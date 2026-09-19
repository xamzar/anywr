"""anywr: a cloud Chrome per user that stays logged in, driven by the user's agent over MCP.

One process: accounts (invite-only sign-up), the landing page, each user's page at
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
    python app.py invite     mint an invite code
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
import sys
import time
import urllib.request
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
ADMINS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
IMAGE = os.environ.get("CHROME_IMAGE", "anywr-chrome")
SELF = os.environ.get("SELF_CONTAINER", "anywr-api")      # attached to every user network
CADDY = os.environ.get("CADDY_CONTAINER", "anywr-caddy")  # likewise, for the viewer
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
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY, created_at INTEGER NOT NULL,
    used_by INTEGER REFERENCES users(id) ON DELETE SET NULL, used_at INTEGER);
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


def require_admin(u=Depends(require_user)):
    if u["email"].lower() not in ADMINS:
        raise HTTPException(403, "admins only")
    return u


def mint_invite():
    code = secrets.token_urlsafe(9)
    q("INSERT INTO invites (code, created_at) VALUES (?,?)", (code, now()))
    return code


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
    for c in (SELF, CADDY):
        await dk("POST", f"/networks/{net}/connect", ok=(200, 403, 409), json={"Container": c})
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
    "and ask the user to finish in the viewer, then continue."))
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


mcp_app = mcp.http_app(path="/mcp", stateless_http=True)

# --------------------------------------------------------------------------
# REST + dashboard
# --------------------------------------------------------------------------

api = FastAPI(title="anywr", docs_url=None, redoc_url=None, openapi_url=None)
HERE = os.path.dirname(os.path.abspath(__file__))


class Start(BaseModel):
    username: str
    invite: str | None = None   # present = sign-up


STATE_COOKIE = "awst"


def set_cookie(resp: Response, tok):
    resp.set_cookie(COOKIE, tok, max_age=SESSION_TTL, httponly=True, secure=True, samesite="lax", path="/")


def page(file):
    return FileResponse(os.path.join(HERE, "static", file))


@api.get("/", include_in_schema=False)
async def landing():
    return page("index.html")


@api.get("/signup", include_in_schema=False)
@api.get("/login", include_in_schema=False)
async def signup_page():
    return page("app.html")


@api.post("/api/login")
async def login_start(b: Start, resp: Response):
    """Begin a sign-in (or sign-up): remember what to do once the email is
    proven, and send the browser to Cloudflare Access to prove it."""
    username = b.username.strip().lower()
    if b.invite is None:
        u = one("SELECT id FROM users WHERE username=?", (username,))
        if not u:
            raise HTTPException(404, f"anywr.me/{username} does not exist")
        intent = {"login": u["id"]}
    else:
        if not USERNAME_RE.fullmatch(username) or username in RESERVED:
            raise HTTPException(400, "usernames are 3-30 characters: a-z, 0-9 and dashes")
        if one("SELECT id FROM users WHERE username=?", (username,)):
            raise HTTPException(400, "that username is taken")
        if not one("SELECT code FROM invites WHERE code=? AND used_at IS NULL", (b.invite.strip(),)):
            raise HTTPException(400, "that invite code is invalid or already used")
        intent = {"signup": username, "invite": b.invite.strip()}
    st = secrets.token_urlsafe(24)
    q("DELETE FROM logins WHERE expires_at<?", (now(),))
    q("INSERT INTO logins VALUES (?,?,?)", (h(st), json.dumps(intent), now() + 600))
    # The ticket must come back to the same browser that started the login.
    resp.set_cookie(STATE_COOKIE, st, max_age=600, httponly=True, secure=True, samesite="lax", path="/auth")
    return {"url": f"{AUTH_URL}/?state={st}"}


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


def oops(msg, status=400):
    return HTMLResponse(f"""<!doctype html><meta name=viewport content="width=device-width">
<title>anywr</title><body style="font:16px system-ui;max-width:480px;margin:15vh auto;padding:0 16px">
<h1 style="font-size:22px">Couldn't sign you in</h1><p>{msg}</p><p><a href="/">anywr.me</a></p>""", status)


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
    intent, email = json.loads(row["intent"]), d["e"].strip().lower()

    if "login" in intent:
        u = one("SELECT id, username, email FROM users WHERE id=?", (intent["login"],))
        if not u or u["email"].lower() != email:
            return oops(f"You verified <b>{email}</b>, but that isn't the email for this page.", 403)
        uid, username = u["id"], u["username"]
    else:
        mine = one("SELECT username FROM users WHERE email=?", (email,))
        if mine:
            return oops(f"{email} already has a browser: <a href=\"/{mine['username']}\">anywr.me/{mine['username']}</a>")
        username = intent["signup"]
        try:
            with conn() as c:  # user + invite claim commit together or not at all
                c.execute("BEGIN IMMEDIATE")
                uid = c.execute("INSERT INTO users (username, email, created_at) VALUES (?,?,?)",
                                (username, email, now())).lastrowid
                if c.execute("UPDATE invites SET used_by=?, used_at=? WHERE code=? AND used_at IS NULL",
                             (uid, now(), intent["invite"])).rowcount != 1:
                    c.execute("ROLLBACK")
                    return oops("That invite code was used by someone else in the meantime.")
                c.execute("COMMIT")
        except sqlite3.IntegrityError:
            return oops("That username was taken in the meantime. Pick another.")
        log.info("signed up u%s %s", uid, username)

    resp = RedirectResponse(f"/{username}", 303)
    set_cookie(resp, new_session(uid))
    resp.delete_cookie(STATE_COOKIE, path="/auth")
    return resp


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
    return {"user": {**u, "admin": u["email"].lower() in ADMINS},
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


@api.get("/api/invites")
async def invites(_=Depends(require_admin)):
    rows, _c = q("SELECT i.code, i.created_at, i.used_at, u.username FROM invites i "
                 "LEFT JOIN users u ON u.id=i.used_by ORDER BY i.created_at DESC")
    return [dict(r) for r in rows]


@api.post("/api/invites")
async def invite_create(_=Depends(require_admin)):
    return {"code": mint_invite()}


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


if __name__ == "__main__":
    if sys.argv[1:] == ["invite"]:
        with conn() as c:
            c.executescript(SCHEMA)
        print(mint_invite())
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*")
