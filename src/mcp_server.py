"""MCP server: full Playwright control of the workspace browsers over CDP.

Workspaces (config/workspaces.yaml) are long-lived Chromes. This server never
launches a browser; it attaches to each one's CDP port, like probe.py does.

`soak` is the Track 0 experiment's browser. Every tool that changes it is
written to soak.db as an AGENT_ACTION event, so report.py can flag which
sessions were kept alive by use rather than by parking. Values typed into
fields are never logged.
"""
import asyncio
import json
import os
import socket
import urllib.request

import yaml
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from playwright.async_api import async_playwright

import db

WS = yaml.safe_load(open("/app/config/workspaces.yaml"))
VIEW_BASE = os.environ.get("VIEW_BASE", "")
mcp = FastMCP("workspace", instructions=(
    "Drive persistent cloud Chrome workspaces that stay logged in. Selectors are Playwright "
    "selectors: `role=button[name=\"Sign in\"]`, `text=Next`, `#id`, or CSS. Read a page with "
    "snapshot() (accessibility tree) before acting. For logins, 2FA or captchas, call handoff() "
    "and ask the user to finish in the viewer, then continue."))

_pw = None
_browsers = {}
_lock = asyncio.Lock()


def _ws_url(cdp):
    """Chrome answers /json only for an IP or localhost Host header, and its
    webSocketDebuggerUrl always says 127.0.0.1 -- which, from this container,
    may be a different workspace's Chrome. So resolve, fetch, and re-point it."""
    host, port = cdp.split(":")
    ip = socket.gethostbyname(host)
    info = json.load(urllib.request.urlopen(f"http://{ip}:{port}/json/version", timeout=10))
    return info["webSocketDebuggerUrl"].replace("127.0.0.1:9222", f"{ip}:{port}", 1)


async def _ctx(ws):
    global _pw
    if ws not in WS:
        raise ValueError(f"unknown workspace {ws!r}; have {list(WS)}")
    async with _lock:
        b = _browsers.get(ws)
        if b is None or not b.is_connected():
            _pw = _pw or await async_playwright().start()
            b = _browsers[ws] = await _pw.chromium.connect_over_cdp(_ws_url(WS[ws]["cdp"]))
    return b.contexts[0]


async def _page(ws, tab):
    pages = (await _ctx(ws)).pages
    if not 0 <= tab < len(pages):
        raise ValueError(f"no tab {tab} in {ws}; it has {len(pages)} tabs (see workspaces())")
    return pages[tab]


def _log(ws, what):
    if ws == "soak":
        db.event(db.connect(), "AGENT_ACTION", what[:300])


def _viewer(ws):
    return f"{VIEW_BASE}/{ws}/vnc.html?path=ws/view/{ws}/websockify&autoconnect=1&resize=scale"


async def _state(page, tab):
    await page.wait_for_load_state("domcontentloaded")
    return f"tab {tab}: {await page.title()} — {page.url}"


@mcp.tool
async def workspaces() -> str:
    """List workspaces, their open tabs (index, title, url) and live viewer links."""
    out = []
    for ws, cfg in WS.items():
        try:
            pages = (await _ctx(ws)).pages
            tabs = "\n".join([f"  [{i}] {await p.title()} — {p.url}" for i, p in enumerate(pages)])
        except Exception as e:  # noqa: BLE001 - one dead workspace must not hide the others
            tabs = f"  unreachable: {e.__class__.__name__}: {e}"
        out.append(f"{ws}: {cfg['about']}\n  viewer: {_viewer(ws)}\n{tabs}")
    return "\n\n".join(out)


@mcp.tool(name="open")
async def navigate(workspace: str, url: str, tab: int | None = None) -> str:
    """Navigate. tab=None opens a new tab; otherwise navigates that tab."""
    _log(workspace, f"open {url} tab={tab}")
    ctx = await _ctx(workspace)
    page = await ctx.new_page() if tab is None else await _page(workspace, tab)
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    return await _state(page, ctx.pages.index(page))


@mcp.tool
async def snapshot(workspace: str, tab: int = 0) -> str:
    """Accessibility tree of the page (roles + names) — use it to pick selectors."""
    page = await _page(workspace, tab)
    tree = await page.locator("body").aria_snapshot(timeout=15_000)
    return f"{await _state(page, tab)}\n\n{tree[:40_000]}"


@mcp.tool
async def screenshot(workspace: str, tab: int = 0, full_page: bool = False) -> Image:
    """PNG of the tab."""
    page = await _page(workspace, tab)
    return Image(data=await page.screenshot(full_page=full_page, timeout=30_000), format="png")


@mcp.tool
async def click(workspace: str, selector: str, tab: int = 0) -> str:
    """Click the first element matching a Playwright selector."""
    _log(workspace, f"click {selector} tab={tab}")
    page = await _page(workspace, tab)
    await page.locator(selector).first.click(timeout=15_000)
    await page.wait_for_timeout(500)
    return await _state(page, tab)


@mcp.tool
async def fill(workspace: str, selector: str, value: str, tab: int = 0) -> str:
    """Replace an input's value. (The value is never logged.)"""
    _log(workspace, f"fill {selector} tab={tab}")
    page = await _page(workspace, tab)
    await page.locator(selector).first.fill(value, timeout=15_000)
    return await _state(page, tab)


@mcp.tool
async def press(workspace: str, key: str, selector: str | None = None, tab: int = 0) -> str:
    """Press a key (Enter, Tab, Escape, PageDown, Control+a...) on an element or the page."""
    _log(workspace, f"press {key} {selector or ''} tab={tab}")
    page = await _page(workspace, tab)
    await (page.locator(selector).first.press(key) if selector else page.keyboard.press(key))
    await page.wait_for_timeout(500)
    return await _state(page, tab)


@mcp.tool
async def select(workspace: str, selector: str, option: str, tab: int = 0) -> str:
    """Choose an <select> option by value or label."""
    _log(workspace, f"select {selector} tab={tab}")
    page = await _page(workspace, tab)
    await page.locator(selector).first.select_option(option, timeout=15_000)
    return await _state(page, tab)


@mcp.tool
async def evaluate(workspace: str, js: str, tab: int = 0) -> str:
    """Run a JS expression/function in the page and return its JSON result (scroll, read, anything)."""
    _log(workspace, f"evaluate {js[:120]} tab={tab}")
    page = await _page(workspace, tab)
    return json.dumps(await page.evaluate(js), default=str)[:40_000]


@mcp.tool
async def close_tab(workspace: str, tab: int) -> str:
    """Close a tab. The last tab is kept so the browser window never disappears."""
    _log(workspace, f"close_tab {tab}")
    ctx = await _ctx(workspace)
    if len(ctx.pages) <= 1:
        return "refused: that is the last tab"
    await (await _page(workspace, tab)).close()
    return f"closed; {len(ctx.pages)} tabs left"


@mcp.tool
async def handoff(workspace: str, reason: str) -> str:
    """Hand control to the human (login, 2FA, captcha). Returns the live viewer
    link to give them; call snapshot() once they say they're done."""
    _log(workspace, f"handoff: {reason}")
    return f"Ask the user to open {_viewer(workspace)} and: {reason}. Wait for them to confirm."


if __name__ == "__main__":
    asyncio.run(mcp.run_async(transport="http", host="0.0.0.0", port=8000))
