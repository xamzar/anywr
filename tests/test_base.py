"""view / click / fill against local fixture HTML. No test leaves the machine.

Most of these are about one property: a ref reaches the element the digest named
it for, or it reaches nothing at all.
"""
import asyncio
import logging
import re

import pytest

pytest.importorskip("playwright")

from kernel.base import Kernel, KernelError  # noqa: E402

REF = re.compile(r"^\[(\d+)\]\s+(\S+)\s+(.*)$", re.M)

MENU = """<!doctype html><title>Menu</title>
<h1>Files</h1>
<div id="list">
  <a href="#d" id="download" onclick="window.__hit='Download';return false">Download</a>
  <a href="#x" id="delete" onclick="window.__hit='Delete';return false">Delete</a>
</div>
<label for="q">Search term</label><input id="q" type="text">
<button onclick="window.__hit='Sign Out'">Sign Out</button>
<a href="next.html" id="go">Next Page</a>
"""

NEXT = """<!doctype html><title>Second Page</title>
<h1>Arrived</h1><a href="#b">Back To Menu</a>
"""


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    d = tmp_path_factory.mktemp("kernel")
    (d / "menu.html").write_text(MENU)
    (d / "next.html").write_text(NEXT)
    return d


@pytest.fixture(scope="module")
def run(pages):
    """`run(fn)` drives `fn(page)` on a fresh tab of one shared Chromium."""
    from playwright.async_api import async_playwright

    loop = asyncio.new_event_loop()
    live = {}

    async def start():
        live["pw"] = await async_playwright().start()
        live["browser"] = await live["pw"].chromium.launch()

    try:
        loop.run_until_complete(start())
    except Exception as exc:  # noqa: BLE001 - no browser binary is a skip, not a failure
        loop.close()
        pytest.skip(f"no usable browser: {exc.__class__.__name__}: {exc}")

    def _run(fn, page_name="menu.html"):
        async def go():
            page = await live["browser"].new_page()
            try:
                await page.goto((pages / page_name).as_uri())
                return await fn(page)
            finally:
                await page.close()
        return loop.run_until_complete(go())

    yield _run

    loop.run_until_complete(live["browser"].close())
    loop.run_until_complete(live["pw"].stop())
    loop.close()


def ref_of(d, name):
    for num, _role, got in REF.findall(d):
        if got.strip() == name:
            return int(num)
    raise AssertionError(f"{name!r} not in digest:\n{d}")


# --- the round trip ---------------------------------------------------------

def test_view_lists_the_page_and_a_ref_clicks_what_it_named(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        after = await k.click(ref_of(d, "Download"))
        return d, after, await page.evaluate("() => window.__hit")

    d, after, hit = run(go)
    assert "Download" in d and "Delete" in d and "Sign Out" in d
    assert hit == "Download"          # the element the digest named, not its neighbour
    assert "Download" in after        # click returns the page as it now is


def test_every_ref_in_the_digest_reaches_its_own_element(run):
    async def go(page):
        out = {}
        for name in ("Download", "Delete", "Sign Out"):
            k = Kernel(page)  # fresh view each time; the page does not navigate
            await page.evaluate("() => { window.__hit = null; }")
            d = await k.view()
            await k.click(ref_of(d, name))
            out[name] = await page.evaluate("() => window.__hit")
        return out

    assert run(go) == {"Download": "Download", "Delete": "Delete", "Sign Out": "Sign Out"}


# --- staleness: the reason this module exists -------------------------------

def test_a_ref_whose_element_changed_is_refused_and_nothing_is_clicked(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        stale = ref_of(d, "Delete")
        # A late-loading menu item lands above the list: everything after it
        # shifts by one, so `stale` is now Download.
        await page.evaluate("""() => {
          const a = document.createElement('a');
          a.href = '#c'; a.textContent = 'Cancel';
          document.getElementById('list').prepend(a);
        }""")
        with pytest.raises(KernelError) as err:
            await k.click(stale)
        return str(err.value), await page.evaluate("() => window.__hit")

    msg, hit = run(go)
    assert hit is None, "refused click still reached an element"
    assert "changed" in msg and "view()" in msg
    assert "Delete" in msg and "Download" in msg  # says what it was and what it is now


def test_a_ref_past_the_end_after_a_shrink_is_refused(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        last = max(int(n) for n, _, _ in REF.findall(d))
        await page.evaluate("() => document.getElementById('list').remove()")
        with pytest.raises(KernelError) as err:
            await k.click(last)
        return str(err.value)

    assert "changed" in run(go)


def test_a_ref_survives_a_change_that_does_not_touch_it(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        r = ref_of(d, "Download")
        # Appended *after* everything: refs 1..n keep their meaning.
        await page.evaluate("""() => {
          const a = document.createElement('a');
          a.href = '#z'; a.textContent = 'Late Footer Link';
          document.body.append(a);
        }""")
        await k.click(r)
        return await page.evaluate("() => window.__hit")

    assert run(go) == "Download"


# --- bad refs ---------------------------------------------------------------

@pytest.mark.parametrize("ref", [0, -1, 999])
def test_a_ref_that_does_not_exist_is_a_clear_error(run, ref):
    async def go(page):
        k = Kernel(page)
        await k.view()
        with pytest.raises(KernelError) as err:
            await k.click(ref)
        return str(err.value)

    msg = run(go)
    assert f"[{ref}]" in msg and "refs 1–" in msg


def test_a_ref_that_is_not_a_number_is_a_clear_error(run):
    async def go(page):
        k = Kernel(page)
        await k.view()
        with pytest.raises(KernelError) as err:
            await k.click("Download")
        return str(err.value)

    assert "whole number" in run(go)


@pytest.mark.parametrize("act", ["click", "fill"])
def test_acting_before_any_view_is_refused(run, act):
    async def go(page):
        k = Kernel(page)
        with pytest.raises(KernelError) as err:
            await (k.click(1) if act == "click" else k.fill(1, "x"))
        return str(err.value)

    assert "view()" in run(go)


# --- fill -------------------------------------------------------------------

SECRET = "hunter2-correct-horse"


def test_fill_writes_the_value(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        await k.fill(ref_of(d, "Search term"), SECRET)
        return await page.evaluate("() => document.getElementById('q').value")

    assert run(go) == SECRET


def test_fill_returns_and_logs_nothing_of_the_value(run, caplog):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        return await k.fill(ref_of(d, "Search term"), SECRET)

    with caplog.at_level(logging.DEBUG):
        returned = run(go)
    assert SECRET not in returned
    assert SECRET not in caplog.text
    assert "fill ref=" in caplog.text  # the ref is logged, so the absence above means something


def test_filling_something_that_is_not_a_field_is_a_clear_error(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        with pytest.raises(KernelError) as err:
            await k.fill(ref_of(d, "Download"), SECRET)
        return str(err.value)

    msg = run(go)
    assert "cannot be filled" in msg
    assert SECRET not in msg


# --- navigation -------------------------------------------------------------

def test_a_click_that_navigates_returns_the_new_page(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        return await k.click(ref_of(d, "Next Page"))

    after = run(go)
    assert "title: Second Page" in after
    assert "Back To Menu" in after
    assert "Download" not in after
    assert after.startswith("url: file://") and after.splitlines()[0].endswith("next.html")


# --- one definition of the candidate set ------------------------------------

def test_base_owns_no_selector_of_its_own():
    """A second copy of the query is how the extractor and the resolver drift
    apart, and drift is a silent wrong click. base.py must import digest's."""
    from pathlib import Path
    src = Path("src/kernel/base.py").read_text()
    assert "querySelectorAll" not in src
    assert "JS_CANDIDATES" in src and "JS_DESCRIBE" in src


def test_refs_from_before_a_navigation_are_refused(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        r = ref_of(d, "Download")
        await page.goto((page.url.rsplit("/", 1)[0]) + "/next.html")
        with pytest.raises(KernelError) as err:
            await k.click(r)
        return str(err.value)

    msg = run(go)
    assert "navigated" in msg and "view()" in msg
