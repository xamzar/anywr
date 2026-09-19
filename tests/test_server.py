"""The MCP surface: seven tools, real CDP, and nothing leaking out of fill().

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
<label for="term">Term</label>
<select id="term">
  <option value="202630">Fall Semester 2026/27</option>
  <option value="202710">Spring Semester 2026/27</option>
</select>
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


def test_the_tool_set_is_exactly_the_base_tools():
    """M5 added three — record, export_adapter, verify — and nothing else. Any
    further name in this list is a promoted adapter, which on a machine with no
    /state volume means the list has grown by accident."""
    assert tool_names() == {"view", "click", "fill", "select", "session_status", "read",
                            "handoff", "record", "export_adapter", "verify"}


def test_every_base_tool_name_is_reserved_against_adapters():
    """The frozen set is what an adapter is measured against, so it has to cover
    the surface that actually exists rather than the one it was written for."""
    from kernel import adapters
    assert tool_names() <= adapters.RESERVED


def test_the_tools_m1_cut_are_absent():
    """select() and read() both left this list on the live run: Final Grades is
    unreachable without select(), and the grades themselves are page text, so
    view() could reach them and not report them. handoff() is M2's whole point
    and joined in M2. open() and evaluate() are still cut by design, and the
    rest were never in scope."""
    assert not tool_names() & {"open", "evaluate", "screenshot", "snapshot", "press",
                               "close_tab", "workspaces", "back"}


def test_no_tool_takes_a_workspace():
    for t in asyncio.run(server.mcp.list_tools()):
        assert "workspace" not in t.parameters.get("properties", {}), t.name


def test_the_instructions_and_docstrings_teach_the_ref_model():
    """Every tool that takes a ref has to say where refs come from and when they
    stop being true. M5's three take none — record() explains what it keeps
    *instead* of a ref, and export_adapter() and verify() never see one — so the
    rule is about the parameter, not about every docstring."""
    assert "view()" in server.mcp.instructions and "ref" in server.mcp.instructions
    took_one = 0
    for t in asyncio.run(server.mcp.list_tools()):
        if "ref" in t.parameters.get("properties", {}):
            assert "most recent digest" in t.description, t.name
            took_one += 1
        # The docstrings are the model's instructions here, not API notes.
        assert len(t.description) > 200, t.name
    assert took_one == 3, "click, fill and select take a ref"


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


# --- select and session_status ------------------------------------------------

def test_select_through_the_tool_sets_the_dropdown_and_returns_the_digest(run):
    async def go(page):
        d = (await call("view")).data
        out = (await call("select", ref=ref_of(d, "Term"),
                          option="Spring Semester 2026/27")).data
        return out, await page.input_value("#term")

    out, value = run(go)
    assert value == "202710"
    assert "title: Menu" in out and "Term" in out


def test_a_guessed_option_comes_back_as_a_result_listing_the_real_ones(run):
    async def go(page):
        d = (await call("view")).data
        return await call("select", ref=ref_of(d, "Term"), option="Semester A 2026")

    res = run(go)
    assert not res.is_error and "Traceback" not in res.data
    assert "'Fall Semester 2026/27'" in res.data and "'Spring Semester 2026/27'" in res.data


def test_session_status_answers_unknown_on_a_page_with_no_evidence(run):
    """The fixture has no sign-out control, no sign-in form and no notice —
    which is most pages, and is why UNKNOWN has to be a real answer."""
    res = run(lambda page: call("session_status"))
    assert not res.is_error
    assert res.data.startswith("UNKNOWN") and " — " in res.data


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


# --- handoff ------------------------------------------------------------------
# End to end through the MCP surface, with the control server's side of the file
# written by hand. No network: with no TELEGRAM_* in the environment the sender
# gives up before it builds a request.


def test_handoff_through_the_tool_blocks_until_the_file_is_cleared(run, monkeypatch, tmp_path):
    from kernel import handoff as hs

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("KERNEL_STATE_FILE", str(tmp_path / "handoff.json"))
    monkeypatch.setenv("KERNEL_HANDOFF_TIMEOUT", "5")
    monkeypatch.setenv("KERNEL_HANDOFF_POLL", "0.01")

    async def go(page):
        async def control_server():
            for _ in range(500):
                await asyncio.sleep(0.01)
                if hs.read().pending:
                    return hs.done()
            raise AssertionError("the handoff was never recorded")

        pressed = asyncio.ensure_future(control_server())
        res = await call("handoff", reason="sign in to AIMS")
        await pressed
        return res

    res = run(go)
    assert not res.is_error and "Traceback" not in res.data
    assert "handoff complete" in res.data
    assert "title: Menu" in res.data          # a fresh digest, as every acting tool returns


def test_a_handoff_nobody_answers_is_a_readable_result_not_a_hang(run, monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("KERNEL_STATE_FILE", str(tmp_path / "handoff.json"))
    monkeypatch.setenv("KERNEL_HANDOFF_TIMEOUT", "0.05")
    monkeypatch.setenv("KERNEL_HANDOFF_POLL", "0.01")

    res = run(lambda page: call("handoff", reason="finish the captcha"))
    assert not res.is_error
    assert "timed out" in res.data and "title: Menu" in res.data
