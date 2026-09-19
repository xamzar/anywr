"""view / click / fill / select / session_status against local fixture HTML.
No test leaves the machine.

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

# The shape of Banner's term page: a labelled dropdown whose labels and values
# are different strings, plus a placeholder that cannot be chosen.
FORM = """<!doctype html><title>Term Select</title>
<label for="term">Term</label>
<select id="term">
  <option value="" disabled selected>None</option>
  <option value="202630">Fall Semester 2026/27</option>
  <option value="202710">Spring Semester 2026/27</option>
</select>
<label for="collide">Collision</label>
<select id="collide">
  <option value="A">B</option>
  <option value="B">C</option>
</select>
<a href="#d" id="download">Download</a>
"""

# What the real portal served after 15 idle minutes: the menu still renders and
# the sign-out link is still there. Only the title and one sentence differ.
TIMEOUT = """<!doctype html><title>Session Timeout</title>
<p>Your AIMS session has been timeout (15 minutes inactivity). Please re-enter
your credentials to continue.</p>
<a href="/pls/PROD/twbkwbis.P_GenMenu?name=bmenu.P_MainMnu">Student Services</a>
<a href="/pls/PROD/twbkwbis.P_GenMenu?name=bmenu.P_AdminMnu">Personal Information</a>
<a href="/pls/PROD/twbkwbis.P_Logout">Exit</a>
"""

SIGNIN = """<!doctype html><title>Sign In</title>
<label for="u">Username</label><input id="u" type="text">
<label for="p">Password</label><input id="p" type="password">
<button>Sign In</button>
"""

# Signed in, and talking about logging in the whole way down. Also carries a
# collapsed sign-in panel, which is in the DOM of plenty of signed-in pages.
PROSE = """<!doctype html><title>Help — Accounts</title>
<p>If you have forgotten your login, use the password reset link. Your login is
your EID. A login attempt from a new device may ask you to verify it, and the
session timeout for this service is documented in the IT handbook.</p>
<a href="#reset">Password Reset</a>
<div style="display:none"><label for="hp">Password</label><input id="hp" type="password"></div>
<button onclick="window.__hit='Sign Out'">Sign Out</button>
"""


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    d = tmp_path_factory.mktemp("kernel")
    for name, html in (("menu.html", MENU), ("next.html", NEXT), ("form.html", FORM),
                       ("timeout.html", TIMEOUT), ("signin.html", SIGNIN),
                       ("prose.html", PROSE)):
        (d / name).write_text(html)
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


@pytest.mark.parametrize("act", ["click", "fill", "select"])
def test_acting_before_any_view_is_refused(run, act):
    async def go(page):
        k = Kernel(page)
        calls = {"click": lambda: k.click(1), "fill": lambda: k.fill(1, "x"),
                 "select": lambda: k.select(1, "x")}
        with pytest.raises(KernelError) as err:
            await calls[act]()
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


def test_digest_is_the_only_module_that_holds_javascript():
    """The generalised form of the rule above, which M5's table read made worth
    stating: one file to read to know everything the kernel runs inside a page
    it does not own. session.py's password probe and base.py's two one-liners
    over an already-resolved element are the named exceptions."""
    from pathlib import Path
    # session.py's password probe is the one other page query; control.py's
    # JavaScript is in a page it serves itself, not injected into someone's.
    allowed = {"digest.py", "session.py", "control.py"}
    for f in sorted(Path("src/kernel").glob("*.py")):
        if f.name in allowed:
            continue
        src = f.read_text()
        assert "querySelector" not in src, f
        assert "document." not in src, f


# --- select -----------------------------------------------------------------
# A native <select> is what Banner's Final Grades page picks a semester with,
# and fill() correctly refuses it, so without this the task has no way forward.


def value_of(page, sel):
    return page.evaluate(f"() => document.getElementById('{sel}').value")


def test_the_digest_already_addresses_a_select(run):
    """digest.INTERACTIVE carries `combobox` and JS_DESCRIBE maps SELECT to it,
    so select() needed no new addressing -- checked, not assumed."""
    d = run(lambda page: Kernel(page).view(), "form.html")
    assert "combobox" in d and ref_of(d, "Term")


def test_fill_still_refuses_a_dropdown_but_now_points_at_select(run):
    """fill() was right to refuse a combobox and wrong to stop there: that
    refusal is what left Final Grades unreachable."""
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        with pytest.raises(KernelError) as err:
            await k.fill(ref_of(d, "Term"), "Fall Semester 2026/27")
        return str(err.value)

    msg = run(go, "form.html")
    assert "cannot be filled" in msg and "select(ref, option)" in msg


def test_select_by_visible_label(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        after = await k.select(ref_of(d, "Term"), "Fall Semester 2026/27")
        return after, await value_of(page, "term")

    after, value = run(go, "form.html")
    assert value == "202630"
    assert "title: Term Select" in after and "Term" in after   # the new digest, like click()


def test_select_by_underlying_value(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        await k.select(ref_of(d, "Term"), "202710")
        return await value_of(page, "term")

    assert run(go, "form.html") == "202710"


def test_a_label_beats_a_value_when_the_two_collide(run):
    """Documented: label first, value second. Option 1 is <option value="A">B</option>
    and option 2 is <option value="B">C</option>, so "B" is option 1's label and
    option 2's value. Playwright's own select_option() would take whichever came
    first in the document; here the label wins because the label is what the
    digest showed."""
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        await k.select(ref_of(d, "Collision"), "B")
        return await value_of(page, "collide")

    assert run(go, "form.html") == "A"


def test_selecting_on_something_that_is_not_a_dropdown_is_a_clear_error(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        with pytest.raises(KernelError) as err:
            await k.select(ref_of(d, "Download"), "Fall Semester 2026/27")
        return str(err.value)

    msg = run(go, "form.html")
    assert "not a dropdown" in msg
    assert "link" in msg and "Download" in msg     # says what the ref actually is
    assert "click()" in msg and "fill()" in msg    # and what to use instead


def test_an_option_that_does_not_exist_lists_the_options_that_do(run):
    """The single likeliest failure on the real page: the model guesses a term
    string. The error has to hand back the real ones."""
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        with pytest.raises(KernelError) as err:
            await k.select(ref_of(d, "Term"), "Semester A 2026")
        return str(err.value)

    msg = run(go, "form.html")
    assert "Semester A 2026" in msg
    assert "'Fall Semester 2026/27'" in msg and "'Spring Semester 2026/27'" in msg


def test_a_disabled_option_is_not_offered_and_cannot_be_chosen(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        with pytest.raises(KernelError) as err:
            await k.select(ref_of(d, "Term"), "None")
        return str(err.value), await value_of(page, "term")

    msg, value = run(go, "form.html")
    assert "Its options are: 'Fall Semester 2026/27', 'Spring Semester 2026/27'." in msg
    assert "(1 disabled option — not selectable)" in msg
    assert value == ""


def test_select_refuses_a_stale_ref_and_selects_nothing(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        stale = ref_of(d, "Term")
        # A late-loading link above everything shifts every ref by one.
        await page.evaluate("""() => {
          const a = document.createElement('a');
          a.href = '#c'; a.textContent = 'Cancel';
          document.body.prepend(a);
        }""")
        with pytest.raises(KernelError) as err:
            await k.select(stale, "Fall Semester 2026/27")
        return str(err.value), await value_of(page, "term")

    msg, value = run(go, "form.html")
    assert value == "", "a refused select still changed the dropdown"
    assert "changed" in msg and "view()" in msg and "Term" in msg


# --- session_status ---------------------------------------------------------
# The digest of an idled-out Banner session is indistinguishable from a live
# one, so this is the only tool that can tell the model to stop.


def status_of(run, page_name):
    return run(lambda page: Kernel(page).session_status(), page_name)


def test_the_banner_timeout_page_reads_logged_out(run):
    out = status_of(run, "timeout.html")
    assert out.startswith("LOGGED_OUT")
    assert "session has been timeout" in out


def test_a_page_that_offers_a_sign_out_reads_authed(run):
    assert status_of(run, "menu.html").startswith("AUTHED")


def test_a_sign_in_page_reads_logged_out(run):
    out = status_of(run, "signin.html")
    assert out.startswith("LOGGED_OUT") and "password" in out


def test_a_page_with_no_evidence_either_way_reads_unknown(run):
    assert status_of(run, "next.html").startswith("UNKNOWN")


def test_prose_about_logging_in_does_not_read_logged_out(run):
    """Also covers the hidden sign-in panel on that page: a password field
    nobody can see is not an invitation to sign in."""
    assert status_of(run, "prose.html").startswith("AUTHED")


def test_session_status_acts_on_nothing_and_leaves_refs_standing(run):
    async def go(page):
        k = Kernel(page)
        d = await k.view()
        await k.session_status()
        await k.click(ref_of(d, "Download"))   # the ref from before is still good
        return await page.evaluate("() => window.__hit")

    assert run(go) == "Download"


def test_session_status_needs_no_view_first(run):
    assert status_of(run, "menu.html").split(" — ")[0] in ("AUTHED", "LOGGED_OUT", "UNKNOWN")


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


# --- handoff ------------------------------------------------------------------
# The wait itself is tests/test_handoff.py's; these are about what the Kernel
# adds to it -- that the human's work is visible to the model afterwards.


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("KERNEL_STATE_FILE", str(tmp_path / "handoff.json"))
    return tmp_path / "handoff.json"


def test_handoff_returns_the_page_as_the_human_left_it(run, state):
    """The human navigates while the agent waits, so the digest that comes back
    has to be the page they ended on, not the one it was called from."""
    from kernel import handoff as hs

    async def go(page):
        k = Kernel(page)
        await k.view()

        async def human():
            for _ in range(500):
                await asyncio.sleep(0.01)
                if hs.read().pending:
                    await page.goto((page.url.rsplit("/", 1)[0]) + "/next.html")
                    return hs.done()

        acted = asyncio.ensure_future(human())
        out = await k.handoff("sign in", send=lambda t: True, timeout=2, poll=0.01)
        await acted
        return out

    out = run(go)
    assert "handoff complete" in out
    assert "next.html" in out and "Back To Menu" in out   # the page they left
    assert "Download" not in out                          # not the one it started on


def test_a_timed_out_handoff_still_returns_a_digest_to_judge(run, state):
    out = run(lambda page: Kernel(page).handoff("sign in", send=lambda t: True,
                                                timeout=0.05, poll=0.01))
    assert "timed out" in out and "menu.html" in out


def test_handoff_refuses_an_empty_reason(run, state):
    async def go(page):
        with pytest.raises(KernelError) as err:
            await Kernel(page).handoff("   ")
        return str(err.value)

    msg = run(go)
    assert "reason" in msg and "phone" in msg


def test_handoff_that_cannot_be_recorded_says_so_instead_of_waiting(run, monkeypatch, tmp_path):
    """No /state volume means nobody is ever asked, so blocking for ten minutes
    would be theatre. It is a deployment fault and the message says so."""
    monkeypatch.setenv("KERNEL_STATE_FILE", str(tmp_path / "wall" / "handoff.json"))
    (tmp_path / "wall").write_text("not a directory")

    async def go(page):
        with pytest.raises(KernelError) as err:
            await Kernel(page).handoff("sign in", send=lambda t: True)
        return str(err.value)

    msg = run(go)
    assert "could not be recorded" in msg and "handoff.json" in msg
