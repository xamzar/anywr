"""anywr: a cloud Chrome per user that stays logged in, driven by the user's agent over MCP.

One process: accounts (open sign-up up to MAX_USERS), each user's page at
anywr.me/<username>, one Docker container
per user (started on demand, stopped when idle, profile kept in a volume), and
the MCP server at /mcp/<token>.

Tenancy works like the LinkedIn service in mcp-hub: the user is always derived
from a credential (session cookie, or the agent token in the MCP URL), never
named by the caller.

There are no passwords. Proving an email address is delegated to Cloudflare
Access's one-time PIN on AUTH_URL (a Worker in the xmzr.dev zone, see auth/):
it sends the code, and the Worker hands the verified address back here as an
HMAC-signed, 2-minute ticket bound to a state cookie set when the login began. Each browser gets its own Docker network holding only
itself, this api and Caddy, so one user's browser cannot reach another's CDP.
The host firewall (fw.sh) stops the browsers reaching private ranges and the
GCE metadata server.

    python app.py            serve
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import time
import urllib.request
from urllib.parse import parse_qs, urlsplit
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.utilities.types import Image
from playwright.async_api import async_playwright
from pydantic import BaseModel
from starlette.applications import Starlette

log = logging.getLogger("anywr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE = os.environ.get("BASE_URL", "https://anywr.me")
AUTH_URL = os.environ.get("AUTH_URL", "https://anywr-auth.xmzr.dev")
SSO_SECRET = os.environ.get("SSO_SECRET", "").encode()   # shared with the auth Worker
DB_PATH = os.environ.get("DB", "/data/anywr.db")
IMAGE = os.environ.get("CHROME_IMAGE", "anywr-chrome")
SELF = os.environ.get("SELF_CONTAINER", "anywr-api")      # attached to every user network
CADDY = os.environ.get("CADDY_CONTAINER", "anywr-caddy")  # likewise, for the viewer
# DEMO ONLY: any 6-digit code signs anyone in, no email is sent. Unset to revert.
DEMO_OTP = os.environ.get("DEMO_OTP") == "1"
MAX_USERS = int(os.environ.get("MAX_USERS", "45"))         # Access free plan: 50 seats
MAX_RUNNING = int(os.environ.get("MAX_RUNNING", "6"))     # ~1 GB each on an 8 GB box
IDLE = int(os.environ.get("IDLE_MINUTES", "30")) * 60
SESSION_TTL = 30 * 86400
COOKIE = "awsid"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
USERNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,28}[a-z0-9])?")
# Top-level paths the app itself uses, plus names that would confuse people.
RESERVED = {"api", "auth", "mcp", "view", "signup", "login", "logout", "admin", "static",
            "www", "help", "about", "terms", "privacy", "favicon.ico", "robots.txt", "anywr"}
USER_HDR = b"x-anywr-user"

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    -- AUTOINCREMENT: an id is never reused, because containers, volumes and
    -- stray rows are keyed by it and a new user must not inherit any of them.
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS agent_tokens (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL, last_used_at INTEGER);
-- A login in flight: what to do once Access has verified an email.
CREATE TABLE IF NOT EXISTS logins (
    state_hash TEXT PRIMARY KEY, intent TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS macros (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL,
    description TEXT NOT NULL, steps TEXT NOT NULL, updated_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, name));
"""


def conn():
    c = sqlite3.connect(DB_PATH, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def q(sql, args=()):
    # ponytail: blocking sqlite from async handlers; fine at tens of users.
    with conn() as c:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        return rows, cur


def one(sql, args=()):
    rows, _ = q(sql, args)
    return dict(rows[0]) if rows else None


def now():
    return int(time.time())


# --------------------------------------------------------------------------
# Credentials: sessions and agent tokens, stored as SHA-256.
# --------------------------------------------------------------------------


def h(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


def new_session(uid):
    tok = secrets.token_urlsafe(32)
    q("INSERT INTO sessions VALUES (?,?,?)", (h(tok), uid, now() + SESSION_TTL))
    return tok


def session_user(request: Request):
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    return one("SELECT u.id, u.username, u.email FROM sessions s JOIN users u ON u.id=s.user_id "
               "WHERE s.token_hash=? AND s.expires_at>?", (h(tok), now()))


def agent_user(tok):
    u = one("SELECT u.id, u.email FROM agent_tokens a JOIN users u ON u.id=a.user_id "
            "WHERE a.token_hash=?", (h(tok),))
    if u:
        q("UPDATE agent_tokens SET last_used_at=? WHERE token_hash=?", (now(), h(tok)))
    return u


def require_user(request: Request):
    u = session_user(request)
    if not u:
        raise HTTPException(401, "not signed in")
    return u


def full():
    return one("SELECT COUNT(*) n FROM users")["n"] >= MAX_USERS


# --------------------------------------------------------------------------
# Browsers: one container + one network + one profile volume per user, via
# the Docker Engine API on the mounted socket.
# --------------------------------------------------------------------------

docker = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock"),
                           base_url="http://docker", timeout=60)
_last_used: dict[int, float] = {}
_lock = asyncio.Lock()  # ponytail: one lock for all starts/stops; per-user locks if starts queue up


def name(uid):
    return f"anywr-u{uid}"


def subnet(uid):
    # A /28 per user out of 172.31.0.0/16 (4096 users); fw.sh keys off this range.
    n = uid * 16
    return f"172.31.{n // 256 % 256}.{n % 256}/28"


async def dk(method, path, ok=(200, 201, 204, 304), **kw):
    r = await docker.request(method, path, **kw)
    if r.status_code not in ok:
        raise RuntimeError(f"docker {method} {path}: {r.status_code} {r.text[:300]}")
    return r


async def state(uid):
    r = await dk("GET", f"/containers/{name(uid)}/json", ok=(200, 404))
    if r.status_code == 404:
        return "none"
    return "running" if r.json()["State"]["Running"] else "stopped"


async def _create(uid):
    net = name(uid)
    await dk("POST", "/networks/create", ok=(201, 409), json={
        "Name": net, "Labels": {"anywr": "1"},
        "IPAM": {"Config": [{"Subnet": subnet(uid)}]}})
    await dk("POST", f"/containers/create?name={net}", json={
        "Image": IMAGE, "Hostname": net, "Labels": {"anywr": str(uid)}, "StopTimeout": 30,
        "HostConfig": {
            "NetworkMode": net,
            "Mounts": [{"Type": "volume", "Source": f"{net}-profile", "Target": "/profile"}],
            "ShmSize": 1 << 30, "Memory": 1536 << 20, "PidsLimit": 1024,
            # Chrome's own sandbox needs namespaces, which Docker's default seccomp
            # profile forbids. Keeping the sandbox beats --no-sandbox for a browser
            # that visits arbitrary sites.
            "SecurityOpt": ["seccomp=unconfined", "no-new-privileges"],
        }})


def _cdp_ws(uid):
    """Chrome answers /json only for an IP Host header and always reports
    127.0.0.1:9222, so resolve the container and re-point the URL at socat."""
    ip = socket.gethostbyname(name(uid))
    socket.create_connection((ip, 6080), timeout=2).close()  # the viewer, too, before we say "up"
    info = json.load(urllib.request.urlopen(f"http://{ip}:9223/json/version", timeout=3))
    return info["webSocketDebuggerUrl"].replace("127.0.0.1:9222", f"{ip}:9223", 1)


async def running():
    r = await dk("GET", "/containers/json", params={"filters": json.dumps({"label": ["anywr"]})})
    return [int(c["Labels"]["anywr"]) for c in r.json()]


async def ensure_running(uid):
    """Start this user's browser (creating it on first use), evicting the
    least-recently-used one if the box is full. Returns the CDP websocket URL."""
    _last_used[uid] = time.time()
    async with _lock:
        s = await state(uid)
        if s != "running":
            live = [u for u in await running() if u != uid]
            for victim in sorted(live, key=lambda u: _last_used.get(u, 0))[:max(0, len(live) - MAX_RUNNING + 1)]:
                log.info("evicting u%s to make room for u%s", victim, uid)
                await stop(victim)
            if s == "none":
                await _create(uid)
            await dk("POST", f"/containers/{name(uid)}/start")
        # Every time, not only at creation: a redeploy recreates the api and Caddy
        # containers, and the new ones are on none of the users' networks.
        for c in (SELF, CADDY):
            await dk("POST", f"/networks/{name(uid)}/connect", ok=(200, 403, 409), json={"Container": c})
    for _ in range(60):
        try:
            return await asyncio.to_thread(_cdp_ws, uid)
        except OSError:
            await asyncio.sleep(0.5)
    raise RuntimeError("browser did not come up in 30s")


async def stop(uid):
    _browsers.pop(uid, None)
    await dk("POST", f"/containers/{name(uid)}/stop", ok=(204, 304, 404), params={"t": 30})


async def destroy(uid):
    await stop(uid)
    n = name(uid)
    await dk("DELETE", f"/containers/{n}", ok=(204, 404), params={"force": "1"})
    await dk("DELETE", f"/volumes/{n}-profile", ok=(204, 404))
    for c in (SELF, CADDY):
        await dk("POST", f"/networks/{n}/disconnect", ok=(200, 404, 500), json={"Container": c, "Force": True})
    await dk("DELETE", f"/networks/{n}", ok=(204, 404))


async def reaper():
    while True:
        await asyncio.sleep(60)
        try:
            for uid in await running():
                last = _last_used.setdefault(uid, time.time())  # unknown after a restart: give it a full window
                if time.time() - last > IDLE:
                    log.info("stopping idle u%s", uid)
                    await stop(uid)
        except Exception:
            log.exception("reaper")


# --------------------------------------------------------------------------
# MCP: the workspace-hub tool set, scoped to the caller's own browser.
# --------------------------------------------------------------------------

mcp = FastMCP("anywr", instructions=(
    "Drive the user's own cloud Chrome, which stays logged in between sessions. Selectors are "
    "Playwright selectors: `role=button[name=\"Sign in\"]`, `text=Next`, `#id`, or CSS. Read a page "
    "with snapshot() (accessibility tree) before acting. For logins, 2FA or captchas, call handoff() "
    "and ask the user to finish in the viewer, then continue. For any workflow you repeat, "
    "check macros() first and save_macro() new ones, so later runs cost one call."))
_pw = None
_browsers = {}


def _uid():
    # Set by the /mcp/<token> dispatcher from the token; any client-sent copy is stripped there.
    return int(get_http_request().headers[USER_HDR.decode()])


def _username():
    return one("SELECT username FROM users WHERE id=?", (_uid(),))["username"]


async def _ctx():
    global _pw
    uid = _uid()
    b = _browsers.get(uid)
    if b is None or not b.is_connected():
        ws = await ensure_running(uid)
        _pw = _pw or await async_playwright().start()
        b = _browsers[uid] = await _pw.chromium.connect_over_cdp(ws)
    _last_used[uid] = time.time()
    return b.contexts[0]


async def _page(tab):
    pages = (await _ctx()).pages
    if not 0 <= tab < len(pages):
        raise ValueError(f"no tab {tab}; there are {len(pages)} (see tabs())")
    return pages[tab]


async def _state(page, tab):
    await page.wait_for_load_state("domcontentloaded")
    return f"tab {tab}: {await page.title()} — {page.url}"


@mcp.tool
async def tabs() -> str:
    """List open tabs (index, title, url) and the live viewer link. Starts the browser if it was asleep."""
    pages = (await _ctx()).pages
    lines = [f"[{i}] {await p.title()} — {p.url}" for i, p in enumerate(pages)]
    return "\n".join(lines) + f"\n\nviewer: {BASE}/{_username()}#browser"


@mcp.tool(name="open")
async def navigate(url: str, tab: int | None = None) -> str:
    """Navigate. tab=None opens a new tab; otherwise navigates that tab."""
    ctx = await _ctx()
    page = await ctx.new_page() if tab is None else await _page(tab)
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    return await _state(page, ctx.pages.index(page))


@mcp.tool
async def snapshot(tab: int = 0) -> str:
    """Accessibility tree of the page (roles + names) — use it to pick selectors."""
    page = await _page(tab)
    tree = await page.locator("body").aria_snapshot(timeout=15_000)
    return f"{await _state(page, tab)}\n\n{tree[:40_000]}"


@mcp.tool
async def screenshot(tab: int = 0, full_page: bool = False) -> Image:
    """PNG of the tab."""
    page = await _page(tab)
    return Image(data=await page.screenshot(full_page=full_page, timeout=30_000), format="png")


@mcp.tool
async def click(selector: str, tab: int = 0) -> str:
    """Click the first element matching a Playwright selector."""
    page = await _page(tab)
    await page.locator(selector).first.click(timeout=15_000)
    await page.wait_for_timeout(500)
    return await _state(page, tab)


@mcp.tool
async def fill(selector: str, value: str, tab: int = 0) -> str:
    """Replace an input's value."""
    page = await _page(tab)
    await page.locator(selector).first.fill(value, timeout=15_000)
    return await _state(page, tab)


@mcp.tool
async def press(key: str, selector: str | None = None, tab: int = 0) -> str:
    """Press a key (Enter, Tab, Escape, PageDown, Control+a...) on an element or the page."""
    page = await _page(tab)
    await (page.locator(selector).first.press(key) if selector else page.keyboard.press(key))
    await page.wait_for_timeout(500)
    return await _state(page, tab)


@mcp.tool
async def select(selector: str, option: str, tab: int = 0) -> str:
    """Choose a <select> option by value or label."""
    page = await _page(tab)
    await page.locator(selector).first.select_option(option, timeout=15_000)
    return await _state(page, tab)


@mcp.tool
async def evaluate(js: str, tab: int = 0) -> str:
    """Run a JS expression/function in the page and return its JSON result."""
    page = await _page(tab)
    return json.dumps(await page.evaluate(js), default=str)[:40_000]


@mcp.tool
async def close_tab(tab: int) -> str:
    """Close a tab. The last tab is kept so the window never disappears."""
    ctx = await _ctx()
    if len(ctx.pages) <= 1:
        return "refused: that is the last tab"
    await (await _page(tab)).close()
    return f"closed; {len(ctx.pages)} tabs left"


@mcp.tool
async def handoff(reason: str) -> str:
    """Hand control to the human (login, 2FA, captcha). Returns the live viewer
    link to give them; call snapshot() once they say they're done."""
    await _ctx()
    return f"Ask the user to open {BASE}/{_username()}#browser and: {reason}. Wait for them to confirm."


# Macros: the user's own saved step lists, so a repeated workflow costs the agent
# one call and one result instead of a dozen snapshots. Steps only reach the
# tools above (never Python), so a macro can do nothing the agent couldn't.
async def _wait(target: str | int, tab: int = 0) -> str:
    page = await _page(tab)
    await (page.wait_for_timeout(target) if isinstance(target, int)
           else page.locator(target).first.wait_for(timeout=30_000))
    return await _state(page, tab)


STEPS = {"open": navigate, "click": click, "fill": fill, "press": press, "select": select,
         "evaluate": evaluate, "snapshot": snapshot, "wait": _wait}
PARAM_RE = re.compile(r"\{\{(\w+)\}\}")


def _subst(v, args, js):
    """{{name}} -> the argument; inside evaluate's js, as a JSON literal so quotes can't break out."""
    if not isinstance(v, str):
        return v
    def one_(m):
        if m[1] not in args:
            raise ValueError(f"missing argument {m[1]!r}")
        return json.dumps(args[m[1]]) if js else str(args[m[1]])
    return PARAM_RE.sub(one_, v)


@mcp.tool
async def save_macro(name: str, description: str, steps: list[dict]) -> str:
    """Create or replace a macro: a saved workflow you can later run in one call with
    run_macro(), returning only its last step's output. Use it whenever you notice
    yourself repeating the same clicks/extraction. Each step is {"tool": ..., ...args}
    with tool one of open(url), click(selector), fill(selector, value), press(key,
    selector?), select(selector, option), wait(target: selector or ms), snapshot(),
    evaluate(js). {{param}} anywhere in a string is filled from run_macro's args (as a
    JSON literal inside js). Steps run in the macro's current tab; open() moves it to
    the new tab. End with an evaluate() that returns just the data you need."""
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", name):
        raise ValueError("name: 1-40 chars of a-z 0-9 _ -")
    for i, s in enumerate(steps):
        if s.get("tool") not in STEPS:
            raise ValueError(f"step {i}: tool must be one of {sorted(STEPS)}")
    old = one("SELECT 1 FROM macros WHERE user_id=? AND name=?", (_uid(), name))
    q("INSERT OR REPLACE INTO macros VALUES (?,?,?,?,?)",
      (_uid(), name, description, json.dumps(steps), now()))
    return f"{'updated' if old else 'saved'} {name} ({len(steps)} steps)"


@mcp.tool
async def macros(name: str | None = None) -> str:
    """List saved macros with their parameters, or show one macro's steps (to edit it:
    change them and save_macro() under the same name)."""
    if name:
        m = one("SELECT description, steps FROM macros WHERE user_id=? AND name=?", (_uid(), name))
        if not m:
            raise ValueError(f"no macro {name!r}")
        return f"{name}: {m['description']}\n{m['steps']}"
    rows, _ = q("SELECT name, description, steps FROM macros WHERE user_id=? ORDER BY name", (_uid(),))
    return "\n".join(f"{r['name']}({', '.join(sorted(set(PARAM_RE.findall(r['steps']))))}): {r['description']}"
                     for r in rows) or "no macros yet (see save_macro)"


@mcp.tool
async def run_macro(name: str, args: dict | None = None, tab: int = 0) -> str:
    """Run a saved macro in a tab. Returns only the last step's output, or where it failed."""
    m = one("SELECT steps FROM macros WHERE user_id=? AND name=?", (_uid(), name))
    if not m:
        raise ValueError(f"no macro {name!r}; see macros()")
    out = ""
    for i, s in enumerate(json.loads(m["steps"])):
        kw = {k: _subst(v, args or {}, s["tool"] == "evaluate") for k, v in s.items() if k != "tool"}
        try:
            if s["tool"] == "open":
                ctx = await _ctx()
                out = await navigate(kw["url"], kw.get("tab"))
                tab = len(ctx.pages) - 1 if kw.get("tab") is None else kw["tab"]
            else:
                out = await STEPS[s["tool"]](**{"tab": tab, **kw})
        except Exception as e:
            return f"step {i} ({s['tool']}) failed: {e}"
    return out


@mcp.tool
async def delete_macro(name: str) -> str:
    """Delete a saved macro."""
    _, cur = q("DELETE FROM macros WHERE user_id=? AND name=?", (_uid(), name))
    return f"deleted {name}" if cur.rowcount else f"no macro {name!r}"


mcp_app = mcp.http_app(path="/mcp", stateless_http=True)

# --------------------------------------------------------------------------
# REST + dashboard
# --------------------------------------------------------------------------

api = FastAPI(title="anywr", docs_url=None, redoc_url=None, openapi_url=None)
HERE = os.path.dirname(os.path.abspath(__file__))


class Start(BaseModel):
    username: str
    signup: bool = False


STATE_COOKIE = "awst"


def set_cookie(resp: Response, tok):
    resp.set_cookie(COOKIE, tok, max_age=SESSION_TTL, httponly=True, secure=True, samesite="lax", path="/")


def page(file):
    return FileResponse(os.path.join(HERE, "static", file))


@api.get("/", include_in_schema=False)
@api.get("/signup", include_in_schema=False)
@api.get("/login", include_in_schema=False)
async def signup_page():
    return page("app.html")


@api.post("/api/login")
async def login_start(b: Start, resp: Response):
    """Begin a sign-in (or sign-up): remember what to do once the email is
    proven, and send the browser to Cloudflare Access to prove it."""
    username = b.username.strip().lower()
    if not b.signup:
        u = one("SELECT id, email FROM users WHERE username=?", (username,))
        if not u:
            raise HTTPException(404, f"anywr.me/{username} does not exist")
        if now() - _mailed.get(u["id"], 0) < 60:
            raise HTTPException(429, "a code was just emailed; wait a minute to send another")
        intent = {"login": u["id"]}
    else:
        if not USERNAME_RE.fullmatch(username) or username in RESERVED:
            raise HTTPException(400, "usernames are 3-30 characters: a-z, 0-9 and dashes")
        if one("SELECT id FROM users WHERE username=?", (username,)):
            raise HTTPException(400, "that username is taken")
        if full():
            raise HTTPException(403, "sign-ups are full")
        intent = {"signup": username}
    st = secrets.token_urlsafe(24)
    q("DELETE FROM logins WHERE expires_at<?", (now(),))
    q("INSERT INTO logins VALUES (?,?,?)", (h(st), json.dumps(intent), now() + 600))
    # The ticket must come back to the same browser that started the login.
    resp.set_cookie(STATE_COOKIE, st, max_age=600, httponly=True, secure=True, samesite="lax", path="/auth")
    if DEMO_OTP:
        return {"url": "/auth/demo"}
    if "login" in intent:
        url = await prefill(st, u["email"])
        if url:
            _mailed[u["id"]] = now()
            return {"url": url}
    return {"url": f"{AUTH_URL}/?state={st}"}


_mailed: dict[int, int] = {}  # ponytail: in-memory, resets on restart; fine for one api process


async def prefill(st, email):
    """Skip Access's "enter your email" step for a known page: start the Access
    login from here, submit the owner's email, and hand the browser to the auth
    Worker's /start, which sets the matching Access app-session cookie and shows a
    code box that never displays the address. None = fall back to the Access page."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{AUTH_URL}/?state={st}")
            login = r.headers["location"]
            if "/cdn-cgi/access/login/" not in login:
                return None
            r2 = await c.post(login.replace("/cdn-cgi/access/login/", "/cdn-cgi/access/verify-code/", 1),
                              data={"email": email})
            nonce = parse_qs(urlsplit(r2.headers["location"]).query)["nonce"][0]
            blob = {"a": r.cookies["CF_AppSession"], "n": nonce, "x": now() + 600}
    except (httpx.HTTPError, KeyError, IndexError) as e:
        log.warning("Access prefill failed, falling back: %r", e)
        return None
    body = base64.urlsafe_b64encode(json.dumps(blob).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(hmac.digest(SSO_SECRET, body, "sha256")).rstrip(b"=")
    return f"{AUTH_URL}/start?b={body.decode()}.{sig.decode()}"


def read_ticket(t):
    """{e: email, s: state, x: expiry}, signed by the auth Worker. None if forged or stale."""
    try:
        body, sig = t.split(".")
        want = base64.urlsafe_b64encode(hmac.digest(SSO_SECRET, body.encode(), "sha256")).rstrip(b"=")
        if not SSO_SECRET or not hmac.compare_digest(want, sig.encode()):
            return None
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    return d if d.get("x", 0) > now() else None


def oops(msg, status=400, retry=None):
    # Cloudflare Access keeps one login per browser across every Access app, and
    # hands out that identity without asking. When it's the wrong person, the
    # only way to get the code prompt again is to sign this browser out of Access.
    switch = "" if not retry else f"""<p><b>Not you?</b> Cloudflare remembered an earlier sign-in on this browser.
<a href="{AUTH_URL}/cdn-cgi/access/logout" target="_blank" rel="noopener">Sign out of Cloudflare</a>
(it opens a new tab), then <a href="{retry}">try again</a> and enter your own email.
A private window works too.</p>"""
    return HTMLResponse(f"""<!doctype html><meta name=viewport content="width=device-width">
<title>anywr</title><style>:root{{color-scheme:dark}}a{{color:#ededed}}</style>
<body style="font:15px/1.5 system-ui,sans-serif;max-width:480px;margin:15vh auto;padding:0 16px;background:#000;color:#ededed">
<h1 style="font-size:22px">Couldn't sign you in</h1><p>{msg}</p>{switch}<p><a href="/">anywr.me</a></p>""", status)


@api.get("/auth/callback", include_in_schema=False)
async def login_finish(request: Request, t: str = ""):
    d = read_ticket(t)
    st = request.cookies.get(STATE_COOKIE, "")
    if not d or not st or not hmac.compare_digest(d.get("s", ""), st):
        return oops("That sign-in link expired or was opened in a different browser. Start again from your page.")
    row = one("SELECT intent FROM logins WHERE state_hash=? AND expires_at>?", (h(st), now()))
    q("DELETE FROM logins WHERE state_hash=?", (h(st),))
    if not row:
        return oops("That sign-in expired. Start again from your page.")
    return finish(json.loads(row["intent"]), d["e"].strip().lower())


def finish(intent, email):
    """The email is proven (Access ticket, or demo mode): sign in or sign up."""
    if "login" in intent:
        u = one("SELECT id, username, email FROM users WHERE id=?", (intent["login"],))
        if not u or u["email"].lower() != email:
            return oops(f"You verified <b>{email}</b>, but that isn't the email for this page.", 403,
                        retry=f"/{u['username']}" if u else None)
        uid, username = u["id"], u["username"]
    else:
        mine = one("SELECT username FROM users WHERE email=?", (email,))
        if mine:
            return oops(f"<b>{email}</b> already has a browser: <a href=\"/{mine['username']}\">anywr.me/{mine['username']}</a>",
                        retry="/signup")
        username = intent["signup"]
        try:
            with conn() as c:  # the cap check and the insert commit together
                c.execute("BEGIN IMMEDIATE")
                if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= MAX_USERS:
                    c.execute("ROLLBACK")
                    return oops("Sign-ups are full.")
                uid = c.execute("INSERT INTO users (username, email, created_at) VALUES (?,?,?)",
                                (username, email, now())).lastrowid
                c.execute("COMMIT")
        except sqlite3.IntegrityError:
            return oops("That username was taken in the meantime. Pick another.")
        log.info("signed up u%s %s", uid, username)

    resp = RedirectResponse(f"/{username}", 303)
    set_cookie(resp, new_session(uid))
    resp.delete_cookie(STATE_COOKIE, path="/auth")
    return resp


DEMO_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>anywr</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500&display=swap" rel=stylesheet>
<style>:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100dvh;display:grid;place-items:center;padding:16px;background:#000;color:#ededed;
 font:15px/1.5 "Instrument Sans",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
form{width:min(360px,100%)}label{display:block;color:#8a8a8a;font-size:14px}
input{display:block;width:100%;margin:6px 0 24px;padding:6px 0;font:500 28px/1.2 "Instrument Sans",system-ui,sans-serif;
 color:#ededed;background:none;border:0;border-bottom:1px solid #262626;border-radius:0}
input[name=code]{letter-spacing:.2em}input:focus{outline:none;border-bottom-color:#ededed}
button{font:500 14px/1 "Instrument Sans",system-ui,sans-serif;color:#000;background:#ededed;border:1px solid #ededed;
 border-radius:6px;padding:9px 14px;cursor:pointer}p{color:#8a8a8a;font-size:14px}</style>
<form method=post action=/auth/demo>{EMAIL}<label for=code>Code from your email</label>
<input id=code name=code inputmode=numeric pattern="\\d{6}" maxlength=6 autocomplete=one-time-code required {AF}>
<button>Sign in</button><p>Demo mode: any 6 digits work.</p></form>"""
EMAIL_FIELD = """<label for=email>Your email</label>
<input id=email name=email type=email autocomplete=email required autofocus>"""


def _pending(request):
    st = request.cookies.get(STATE_COOKIE, "")
    row = st and one("SELECT intent FROM logins WHERE state_hash=? AND expires_at>?", (h(st), now()))
    return st, (json.loads(row["intent"]) if row else None)


@api.get("/auth/demo", include_in_schema=False)
async def demo_page(request: Request):
    st, intent = _pending(request)
    if not DEMO_OTP or not intent:
        return oops("That sign-in expired. Start again from your page.")
    signup = "signup" in intent
    return HTMLResponse(DEMO_PAGE.replace("{EMAIL}", EMAIL_FIELD if signup else "").replace("{AF}", "" if signup else "autofocus"))


@api.post("/auth/demo", include_in_schema=False)
async def demo_finish(request: Request):
    st, intent = _pending(request)
    if not DEMO_OTP or not intent:
        return oops("That sign-in expired. Start again from your page.")
    f = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    if not re.fullmatch(r"\d{6}", f.get("code", "")):
        return oops("The code is 6 digits.")
    q("DELETE FROM logins WHERE state_hash=?", (h(st),))
    if "login" in intent:
        u = one("SELECT email FROM users WHERE id=?", (intent["login"],))
        email = u["email"] if u else ""
    else:
        email = f.get("email", "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return oops("That doesn't look like an email address.")
    return finish(intent, email)


@api.post("/api/logout")
async def logout(request: Request, resp: Response):
    q("DELETE FROM sessions WHERE token_hash=?", (h(request.cookies.get(COOKIE, "")),))
    resp.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@api.get("/api/session")
async def session(request: Request):
    u = session_user(request)
    if not u:
        return {"user": None}
    info = one("SELECT created_at, last_used_at FROM agent_tokens WHERE user_id=?", (u["id"],))
    return {"user": u,
            "browser": await state(u["id"]), "agent": info}


@api.post("/api/agent")
async def agent_rotate(u=Depends(require_user)):
    """Mint the MCP link, replacing any previous one. Shown once; stored hashed."""
    tok = secrets.token_urlsafe(32)
    q("INSERT OR REPLACE INTO agent_tokens (token_hash, user_id, created_at) VALUES (?,?,?)",
      (h(tok), u["id"], now()))
    return {"url": f"{BASE}/mcp/{tok}"}


@api.delete("/api/agent")
async def agent_revoke(u=Depends(require_user)):
    q("DELETE FROM agent_tokens WHERE user_id=?", (u["id"],))
    return {"ok": True}


@api.post("/api/browser/{action}")
async def browser(action: str, u=Depends(require_user)):
    """start, stop, or touch (the dashboard's heartbeat while the viewer is open)."""
    if action == "start":
        await ensure_running(u["id"])
    elif action == "stop":
        await stop(u["id"])
    elif action == "touch":
        _last_used[u["id"]] = time.time()
    else:
        raise HTTPException(404)
    return {"browser": await state(u["id"])}


@api.get("/auth/view")
async def view_auth(request: Request, response: Response):
    """Caddy forward_auth for /view/*: which noVNC to proxy to, for whoever holds this cookie."""
    u = require_user(request)
    await ensure_running(u["id"])
    response.headers["X-Upstream"] = f"{name(u['id'])}:6080"
    return {"ok": True}


class Confirm(BaseModel):
    username: str


@api.post("/api/account/delete")
async def delete_account(b: Confirm, u=Depends(require_user)):
    if b.username.strip().lower() != u["username"]:
        raise HTTPException(400, "type your username to confirm")
    await destroy(u["id"])
    q("DELETE FROM users WHERE id=?", (u["id"],))
    return {"ok": True}


@api.get("/api/user/{username}")
async def user_exists(username: str):
    return {"exists": bool(one("SELECT 1 FROM users WHERE username=?", (username.lower(),)))}


# Last: everything else at the top level is somebody's page.
@api.get("/{username}", include_in_schema=False)
async def user_page(username: str):
    return page("app.html")


# --------------------------------------------------------------------------
# ASGI entry: /mcp/<token> -> MCP (token resolved to a user here), else REST.
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_):
    with conn() as c:
        c.executescript(SCHEMA)
    async with mcp_app.lifespan(_):
        task = asyncio.create_task(reaper())
        try:
            yield
        finally:
            task.cancel()


_lifespan_host = Starlette(lifespan=lifespan)


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        return await _lifespan_host(scope, receive, send)
    if scope["type"] == "http" and scope["path"].startswith("/mcp/"):
        token = scope["path"][len("/mcp/"):].rstrip("/")
        # 404, not 401, for anything not token-shaped: clients probe
        # /.well-known/... first, and a 401 sends them looking for OAuth.
        if not TOKEN_RE.fullmatch(token):
            return await plain(send, 404, "not found")
        u = await asyncio.to_thread(agent_user, token)
        if not u:
            return await plain(send, 401, "invalid or revoked link - make a new one at " + BASE)
        headers = [(k, v) for k, v in scope["headers"] if k != USER_HDR]
        headers.append((USER_HDR, str(u["id"]).encode()))
        return await mcp_app({**scope, "path": "/mcp", "raw_path": b"/mcp", "headers": headers},
                             receive, send)
    return await api(scope, receive, send)


async def plain(send, status, msg):
    body = json.dumps({"detail": msg}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class RedactTokens(logging.Filter):
    """The agent link is the whole credential; keep it out of the access log."""
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) > 2 and str(record.args[2]).startswith("/mcp/"):
            record.args = (*record.args[:2], "/mcp/<token>", *record.args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(RedactTokens())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*")
