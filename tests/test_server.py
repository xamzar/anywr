"""The MCP surface: three tools, real CDP, and nothing leaking out of fill().

The CDP half runs against a Chromium this file launches on localhost with
--remote-debugging-port, so attach() is exercised for real -- /json/version,
websocket and all -- without any test leaving the machine.
"""
import asyncio
import logging
import re
import time

import pytest

pytest.importorskip("playwright")
pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402

from kernel import base, server  # noqa: E402

REF = re.compile(r"^\[(\d+)\]\s+(\S+)\s+(.*)$", re.M)
SECRET = "hunter2-nEvEr-l0gged"

MENU = """<!doctype html><title>Menu</title>
<h1>Files</h1>
<div id="list"><a href="#d" id="download">Download</a></div>
<label for="q">Search term</label><input id="q" type="text">
<a href="next.html" id="go">Next Page</a>
"""

NEXT = """<!doctype html><title>Second Page</title>
<h1>Arrived</h1><a href="#b">Back To Menu</a>
"""


def ref_of(d, name):
    for num, _role, got in REF.findall(d):
        if got.strip() == name:
            return int(num)
    raise AssertionError(f"{name!r} not in digest:\n{d}")


async def call(name, **args):
    """One tool call over the in-memory MCP transport."""
    async with Client(server.mcp) as c:
        return await c.call_tool(name, args)


# --- the tool surface -------------------------------------------------------
# No browser needed: this is what the model is handed, and it is the part that
# must not quietly grow.

def tool_names():
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_the_tool_set_is_exactly_view_click_fill():
    assert tool_names() == {"view", "click", "fill"}


def test_the_tools_m1_cut_are_absent():
    assert not tool_names() & {"open", "evaluate", "screenshot", "snapshot", "press",
                               "select", "close_tab", "workspaces", "handoff"}


def test_no_tool_takes_a_workspace():
    for t in asyncio.run(server.mcp.list_tools()):
        assert "workspace" not in t.parameters.get("properties", {}), t.name


def test_the_instructions_and_docstrings_teach_the_ref_model():
    assert "view()" in server.mcp.instructions and "ref" in server.mcp.instructions
    for t in asyncio.run(server.mcp.list_tools()):
        assert "ref" in t.description, t.name


# --- a real browser on a real CDP port --------------------------------------

def _cdp_port(profile):
    """Chrome writes the port it actually took here. --remote-debugging-port=0
    plus this file is the one way to pick a port that cannot collide."""
    f = profile / "DevToolsActivePort"
    for _ in range(100):
        if f.exists() and (line := f.read_text().split("\n")[0].strip()):
            return int(line)
        time.sleep(0.05)
    raise AssertionError("chrome never reported a CDP port")


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    """A Chromium on localhost CDP showing menu.html, and the loop it lives on."""
    from playwright.async_api import async_playwright

    d = tmp_path_factory.mktemp("kernel-server")
    (d / "menu.html").write_text(MENU)
    (d / "next.html").write_text(NEXT)
    loop = asyncio.new_event_loop()
    live = {"dir": d, "loop": loop}

    async def start():
        live["pw"] = await async_playwright().start()
        live["ctx"] = await live["pw"].chromium.launch_persistent_context(
            str(d / "profile"), args=["--remote-debugging-port=0"])
        live["page"] = live["ctx"].pages[0] if live["ctx"].pages else await live["ctx"].new_page()

    try:
        loop.run_until_complete(start())
    except Exception as exc:  # noqa: BLE001 - no browser binary is a skip, not a failure
        loop.close()
        pytest.skip(f"no usable browser: {exc.__class__.__name__}: {exc}")

    live["cdp"] = f"127.0.0.1:{_cdp_port(d / 'profile')}"
    yield live

    loop.run_until_complete(live["ctx"].close())
    loop.run_until_complete(live["pw"].stop())
    loop.close()


@pytest.fixture
def run(browser, monkeypatch):
    """`run(fn)` drives `fn(page)` with the server pointed at the local Chrome.

    Each test starts from menu.html with no cached connection, so one test's
    navigation cannot become another's starting state.
    """
    monkeypatch.setenv("KERNEL_CDP", browser["cdp"])
    monkeypatch.setattr(server, "_kernel", None)

    def _run(fn):
        async def go():
            await browser["page"].goto((browser["dir"] / "menu.html").as_uri())
            return await fn(browser["page"])
        return browser["loop"].run_until_complete(go())

    return _run


# --- the round trip ---------------------------------------------------------

def test_view_then_click_returns_the_new_pages_digest(run):
    async def go(page):
        d = (await call("view")).data
        return d, (await call("click", ref=ref_of(d, "Next Page"))).data

    d, after = run(go)
    assert "menu.html" in d and "Next Page" in d
    assert "next.html" in after and "Back To Menu" in after   # the page click landed on
    assert "Next Page" not in after                           # refs are the new page's


def test_a_ref_from_before_a_change_is_refused_readably(run):
    async def go(page):
        d = (await call("view")).data
        stale = ref_of(d, "Next Page")
        # A late-loading item above the list shifts everything after it by one.
        await page.evaluate("""() => {
          const a = document.createElement('a');
          a.href = '#c'; a.textContent = 'Cancel';
          document.getElementById('list').prepend(a);
        }""")
        return (await call("click", ref=stale)), page.url

    res, url = run(go)
    assert not res.is_error and "Traceback" not in res.data
    assert "view()" in res.data and "Next Page" in res.data
    assert "menu.html" in url, "a refused click still navigated"


def test_an_out_of_range_ref_is_a_result_not_an_exception(run):
    async def go(page):
        await call("view")
        return await call("click", ref=999)

    res = run(go)
    assert not res.is_error
    assert "no ref [999]" in res.data and "Traceback" not in res.data


def test_acting_before_any_view_says_so(run):
    res = run(lambda page: call("click", ref=1))
    assert not res.is_error and "view()" in res.data


# --- the value ---------------------------------------------------------------

def test_fill_reaches_the_field_and_the_value_reaches_nothing_else(run, caplog):
    caplog.set_level(logging.DEBUG)

    async def go(page):
        d = (await call("view")).data
        out = (await call("fill", ref=ref_of(d, "Search term"), value=SECRET)).data
        return out, await page.input_value("#q")

    out, typed = run(go)
    assert typed == SECRET, "fill did not actually type the value"
    assert SECRET not in out and SECRET not in caplog.text


def test_an_unexpected_failure_is_logged_scrubbed_not_returned(run, monkeypatch, caplog):
    async def boom(self, ref, value):
        raise RuntimeError(f"internal detail {value}")  # a bug that quoted the secret

    monkeypatch.setattr(base.Kernel, "fill", boom)
    caplog.set_level(logging.DEBUG)

    async def go(page):
        d = (await call("view")).data
        return await call("fill", ref=ref_of(d, "Search term"), value=SECRET)

    res = run(go)
    assert not res.is_error
    assert "RuntimeError" in res.data and "Traceback" not in res.data  # a line, not a dump
    assert "Traceback" in caplog.text and "RuntimeError" in caplog.text  # not swallowed either
    assert SECRET not in caplog.text and "***" in caplog.text


def test_a_dropped_connection_is_re_attached_on_the_next_call(run):
    """The recovery path: a broken kernel must cost one call, not a restart."""
    async def go(page):
        await call("view")
        server._kernel = object()          # stand-in for a connection that died
        first = (await call("view")).data  # fails on the dead one, drops it
        return first, (await call("view")).data

    first, second = run(go)
    assert "failed unexpectedly" in first
    assert "menu.html" in second and server._kernel is not None
