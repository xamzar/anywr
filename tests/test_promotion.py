"""record → export_adapter → replay → verify, against local fixture HTML.
No test leaves the machine.

The property under all of it: a path found once replays without the model
looking at anything, and the things that would make that dangerous — a value on
disk, a name that shadows a base tool, a wrong row returned as a right one — are
each impossible rather than merely avoided.
"""
import asyncio
import hashlib
import logging
import pathlib

import pytest

pytest.importorskip("playwright")
pytest.importorskip("yaml")
pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402

from kernel import adapters, replay, server  # noqa: E402
from kernel.base import Kernel  # noqa: E402

SECRET = "hunter2-must-not-be-filed"

MENU = """<!doctype html><title>Main Menu</title>
<a href="record.html">Student Record</a>
<a href="other.html">Personal Information</a>
<a href="/logout">Sign Out</a>
"""

RECORD = """<!doctype html><title>Student Record</title>
<a href="grades.html">Grade Display</a>
<a href="menu.html">Back To Menu</a>
<a href="/logout">Sign Out</a>
"""

# Banner's shape: a header row of <th>, the data, and a summary row too short to
# be a grade. A row that cannot carry every column must be dropped and counted,
# never filled in from its neighbour.
GRADES = """<!doctype html><title>Grade Display</title>
<a href="record.html">Back</a>
<table class="datadisplaytable">
  <tr><th>Course</th><th>Title</th><th>Credits</th><th>Grade</th></tr>
  <tr><td>CS1103</td><td>Data Structures</td><td>3</td><td>A</td></tr>
  <tr><td>CS2402</td><td>Operating Systems</td><td>3</td><td>B+</td></tr>
  <tr><td>MA1200</td><td>Calculus</td><td>3</td><td>A-</td></tr>
  <tr><td>Total</td><td>9</td></tr>
</table>
<a href="/logout">Sign Out</a>
"""

# The same page after the site is redesigned: the table is still there and still
# full of rows, but the class the adapter holds on to is gone.
GRADES_MOVED = GRADES.replace('class="datadisplaytable"', 'class="gradeTable"')

# A path with a value in it: what gets typed is the caller's argument, so the
# adapter file must come out of this with no trace of it.
SEARCH = """<!doctype html><title>Course Search</title>
<label for="q">Course code</label><input id="q" type="text">
<button id="go">Search</button>
<table id="results"><tr><th>Course</th><th>Room</th></tr></table>
<a href="/logout">Sign Out</a>
<script>
document.getElementById('go').onclick = function () {
  var v = document.getElementById('q').value;
  var r = document.getElementById('results').insertRow(-1);
  r.insertCell(0).textContent = v;
  r.insertCell(1).textContent = 'LT-1';
};
</script>
"""

SIGNIN = """<!doctype html><title>Sign In</title>
<label for="u">Username</label><input id="u" type="text">
<label for="p">Password</label><input id="p" type="password">
<button>Sign In</button>
"""

# The finding M1 was reshaped around: an idled-out session serves the whole menu.
TIMEOUT = """<!doctype html><title>Session Timeout</title>
<p>Your AIMS session has been timeout (15 minutes inactivity).</p>
<a href="menu.html">Student Services</a>
<a href="/pls/PROD/twbkwbis.P_Logout">Exit</a>
"""

OTHER = """<!doctype html><title>Personal Information</title>
<a href="menu.html">Back To Menu</a>
<a href="/logout">Sign Out</a>
"""

BLANK = """<!doctype html><title>Nothing Here</title><p>Nothing here.</p>"""

PAGES = {"menu.html": MENU, "record.html": RECORD, "grades.html": GRADES,
         "moved.html": GRADES_MOVED, "search.html": SEARCH, "signin.html": SIGNIN,
         "timeout.html": TIMEOUT, "other.html": OTHER, "blank.html": BLANK}

FIELDS = {"course": 0, "title": 1, "grade": 3}


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    d = tmp_path_factory.mktemp("promotion")
    for name, html in PAGES.items():
        (d / name).write_text(html)
    return d


@pytest.fixture(scope="module")
def browser(pages):
    """One Chromium and one loop for the module, as tests/test_base.py does."""
    from playwright.async_api import async_playwright

    loop = asyncio.new_event_loop()
    live = {"dir": pages, "loop": loop, "lock": server._lock}

    async def start():
        live["pw"] = await async_playwright().start()
        live["browser"] = await live["pw"].chromium.launch()
        # The MCP lock is created at import and may bind to the first loop that
        # awaits it; this module owns that loop, so it gets its own and hands
        # the original back afterwards.
        server._lock = asyncio.Lock()

    try:
        loop.run_until_complete(start())
    except Exception as exc:  # noqa: BLE001 - no browser binary is a skip, not a failure
        loop.close()
        pytest.skip(f"no usable browser: {exc.__class__.__name__}: {exc}")

    yield live

    loop.run_until_complete(live["browser"].close())
    loop.run_until_complete(live["pw"].stop())
    loop.close()
    server._lock = live["lock"]


@pytest.fixture
def run(browser):
    """`run(fn, page)` drives `fn(kernel)` on a fresh tab showing `page`."""
    def _run(fn, page_name="menu.html"):
        async def go():
            page = await browser["browser"].new_page()
            try:
                await page.goto((browser["dir"] / page_name).as_uri())
                return await fn(page)
            finally:
                await page.close()
        return browser["loop"].run_until_complete(go())
    return _run


@pytest.fixture
def adapters_dir(tmp_path, monkeypatch):
    d = tmp_path / "adapters"
    d.mkdir()
    monkeypatch.setenv("KERNEL_ADAPTERS_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def tools_restored():
    """Whatever a test registers, the next module must not inherit."""
    before = {t.name for t in asyncio.run(server.mcp.list_tools())}
    yield
    for name in {t.name for t in asyncio.run(server.mcp.list_tools())} - before:
        server.mcp.local_provider.remove_tool(name)


def ref_of(kernel, name):
    for el in kernel.shown():
        if el.name == name:
            return el.ref
    raise AssertionError(f"{name!r} not on the page: {[e.name for e in kernel.shown()]}")


async def walk_to_grades(k):
    """The path a model would explore once, with the recorder on."""
    k.record(True)
    await k.view()
    await k.click(ref_of(k, "Student Record"))
    await k.click(ref_of(k, "Grade Display"))
    k.record(False)
    return k


# --- recording ----------------------------------------------------------------

def test_recording_captures_the_path_by_role_and_name_not_by_ref(run):
    steps, dropped = run(lambda p: _recorded(Kernel(p)))
    assert dropped == 0
    assert steps == [
        {"click": {"role": "link", "name": "Student Record", "occurrence": 1}},
        {"click": {"role": "link", "name": "Grade Display", "occurrence": 1}},
    ]
    assert not any("ref" in body for s in steps for body in s.values())


async def _recorded(k):
    await walk_to_grades(k)
    return k.recording()


def test_nothing_is_recorded_while_it_is_off(run):
    async def go(page):
        k = Kernel(page)
        await k.view()
        await k.click(ref_of(k, "Student Record"))
        return k.recording()

    assert run(go) == ([], 0)


def test_a_refused_act_is_not_a_step(run):
    """A click that never happened is not part of the path, and the page it
    would have reached is not either."""
    async def go(page):
        k = Kernel(page)
        k.record(True)
        await k.view()
        try:
            await k.click(999)
        except Exception:  # noqa: BLE001
            pass
        return k.recording()

    assert run(go) == ([], 0)


def test_record_true_starts_a_fresh_recording(run):
    async def go(page):
        k = Kernel(page)
        await walk_to_grades(k)
        k.record(True)
        return k.recording()

    assert run(go) == ([], 0)


def test_the_summary_says_what_was_captured(run):
    async def go(page):
        k = Kernel(page)
        await walk_to_grades(k)
        return k.recording_summary()

    out = run(go)
    assert "2 step(s)" in out and "Student Record" in out and "Grade Display" in out


# --- export -------------------------------------------------------------------

def test_export_writes_a_valid_file_and_the_adapter_loads_back(run, adapters_dir):
    res = run(lambda p: _export(Kernel(p)))
    assert res["ok"] and res["adapter"] == "aims_grades"
    assert res["selector"] == "table.datadisplaytable"
    assert res["skipped_rows"] == 1                    # the "Total" row, owned up to
    assert res["sample"][0] == {"course": "CS1103", "title": "Data Structures", "grade": "A"}

    spec = adapters.load("aims_grades")
    assert [list(s)[0] for s in spec["steps"]] == ["click", "click"]
    assert spec["returns"] == [{"course": "str", "title": "str", "grade": "str"}]


async def _export(k, name="aims_grades", fields=None, **kw):
    await walk_to_grades(k)
    return await server._export(k, name, "Current semester grades", fields or FIELDS,
                                kw.pop("selector", None), kw.pop("types", None))


def test_export_refuses_a_recording_that_extracts_nothing(run, adapters_dir):
    async def go(page):
        k = Kernel(page)
        with pytest.raises(adapters.AdapterError) as err:
            await _export(k, selector="table.does-not-exist")
        return str(err.value)

    msg = run(go)
    assert "table.does-not-exist" in msg and "Nothing was written" in msg
    assert list(adapters_dir.iterdir()) == [], "a refused export still wrote something"


def test_export_refuses_when_nothing_was_recorded(run, adapters_dir):
    async def go(page):
        k = Kernel(page)
        with pytest.raises(adapters.AdapterError) as err:
            await server._export(k, "aims_grades", "d", FIELDS, None, None)
        return str(err.value)

    assert "nothing has been recorded" in run(go)


def test_types_come_back_as_numbers_not_strings(run, adapters_dir):
    res = run(lambda p: _export(p and Kernel(p), fields={"course": 0, "credits": 2},
                                types={"credits": "int"}))
    assert res["sample"][0] == {"course": "CS1103", "credits": 3}


# --- the value that is never written ------------------------------------------

def test_no_fill_value_reaches_the_adapter_file_the_audit_log_or_a_log_line(
        run, adapters_dir, caplog):
    async def go(page):
        k = Kernel(page)
        k.record(True)
        await k.view()
        await k.fill(ref_of(k, "Course code"), SECRET)
        await k.click(ref_of(k, "Search"))
        k.record(False)
        return await server._export(k, "course_rooms", "Rooms for a course",
                                    {"course": 0, "room": 1}, "#results", None)

    with caplog.at_level(logging.DEBUG):
        res = run(go, "search.html")

    assert res["arguments"] == ["course_code"]
    for path in sorted(pathlib.Path(adapters_dir).rglob("*")):
        assert SECRET not in path.read_text(), path
    assert SECRET not in caplog.text
    assert "fill ref=" in caplog.text        # the ref is logged, so the absence means something


def test_a_password_field_is_not_recorded_and_blocks_the_export(run, adapters_dir):
    async def go(page):
        k = Kernel(page)
        k.record(True)
        await k.view()
        await k.fill(ref_of(k, "Password"), SECRET)
        steps, dropped = k.recording()
        with pytest.raises(adapters.AdapterError) as err:
            await server._export(k, "sso_login", "signs in", {"a": 0}, "table", None)
        return steps, dropped, str(err.value)

    steps, dropped, msg = run(go, "signin.html")
    assert steps == [] and dropped == 1
    assert "password" in msg and "handoff()" in msg
    assert SECRET not in msg
    assert list(adapters_dir.iterdir()) == []


# --- replay -------------------------------------------------------------------

def test_a_recorded_adapter_replays_and_returns_typed_rows(run, adapters_dir):
    async def go(page):
        k = Kernel(page)
        await _export(k)
        await page.goto((page.url.rsplit("/", 1)[0]) + "/menu.html")   # back to the start
        return await replay.run_tool(k, adapters.load("aims_grades"))

    res = run(go)
    assert res["ok"] and res["count"] == 3 and res["skipped_rows"] == 1
    assert res["rows"] == [
        {"course": "CS1103", "title": "Data Structures", "grade": "A"},
        {"course": "CS2402", "title": "Operating Systems", "grade": "B+"},
        {"course": "MA1200", "title": "Calculus", "grade": "A-"},
    ]
    assert "url:" not in str(res), "a replay returns rows, never a digest"
    assert "[1]" not in str(res)


def test_a_replay_takes_its_fill_value_as_an_argument(run, adapters_dir):
    async def go(page):
        k = Kernel(page)
        k.record(True)
        await k.view()
        await k.fill(ref_of(k, "Course code"), "placeholder")
        await k.click(ref_of(k, "Search"))
        k.record(False)
        await server._export(k, "course_rooms", "Rooms", {"course": 0, "room": 1},
                             "#results", None)
        spec = adapters.load("course_rooms")
        await page.goto((page.url.rsplit("/", 1)[0]) + "/search.html")   # a clean table
        return await replay.run_tool(k, spec, {"course_code": "CS4288"})

    res = run(go, "search.html")
    assert res["ok"] and res["rows"] == [{"course": "CS4288", "room": "LT-1"}]


def test_a_replay_with_a_missing_argument_says_which(run, adapters_dir):
    async def go(page):
        k = Kernel(page)
        k.record(True)
        await k.view()
        await k.fill(ref_of(k, "Course code"), "x")
        await k.click(ref_of(k, "Search"))
        k.record(False)
        await server._export(k, "course_rooms", "Rooms", {"course": 0, "room": 1},
                             "#results", None)
        return await replay.run_tool(k, adapters.load("course_rooms"), {})

    res = run(go, "search.html")
    assert not res["ok"] and "course_code" in res["error"]


def test_a_broken_step_names_the_step_rather_than_returning_wrong_rows(run, adapters_dir):
    """The page still has links and a table; what it does not have is the link
    this adapter's second step clicks."""
    async def go(page):
        k = Kernel(page)
        await _export(k)
        spec = adapters.load("aims_grades")
        spec["steps"][1] = {"click": {"role": "link", "name": "Gone Away", "occurrence": 1}}
        await page.goto((page.url.rsplit("/", 1)[0]) + "/menu.html")
        return await replay.run_tool(k, spec)

    res = run(go)
    assert not res["ok"] and res["rows"] == [] and res["count"] == 0
    assert "step 2" in res["error"] and "Gone Away" in res["error"]


def test_a_broken_selector_names_extraction_rather_than_returning_wrong_rows(run, adapters_dir):
    """The site is redesigned: the grade table is still full of rows, and the
    class the adapter holds is gone. Guessing the next table would return three
    plausible, wrong rows."""
    async def go(page):
        k = Kernel(page)
        await _export(k)
        spec = adapters.load("aims_grades")
        await page.goto((page.url.rsplit("/", 1)[0]) + "/moved.html")
        spec["steps"] = [{"wait": {"ms": 0}}]      # already on the page; do not navigate
        return await replay.run_tool(k, spec)

    res = run(go)
    assert not res["ok"] and res["rows"] == []
    assert "extract failed" in res["error"] and "table.datadisplaytable" in res["error"]


def test_a_read_step_that_finds_nothing_fails_at_that_step(run, adapters_dir):
    async def go(page):
        k = Kernel(page)
        await _export(k)
        spec = adapters.load("aims_grades")
        spec["steps"].append({"read": {"contains": "Tuition Balance"}})
        await page.goto((page.url.rsplit("/", 1)[0]) + "/menu.html")
        return await replay.run_tool(k, spec)

    res = run(go)
    assert not res["ok"] and "step 3" in res["error"] and "Tuition Balance" in res["error"]


def test_a_first_step_that_misses_says_the_page_may_be_the_wrong_one(run, adapters_dir):
    async def go(page):
        k = Kernel(page)
        await _export(k)
        spec = adapters.load("aims_grades")
        await page.goto((page.url.rsplit("/", 1)[0]) + "/blank.html")
        return await replay.run_tool(k, spec)

    res = run(go)
    assert "step 1" in res["error"] and "starts from" in res["error"]


# --- verify: broken, or signed out? -------------------------------------------

def _verify_from(run, page_name, adapters_dir):
    async def go(page):
        k = Kernel(page)
        await _export(k)
        await page.goto((page.url.rsplit("/", 1)[0]) + "/" + page_name)
        return await replay.verify(k, adapters.load("aims_grades"))
    return run(go)


def test_verify_says_ok_when_the_adapter_still_works(run, adapters_dir):
    res = _verify_from(run, "menu.html", adapters_dir)
    assert res["verdict"] == "OK" and res["count"] == 3
    assert res["columns"] == ["course", "title", "grade"]
    assert res["sample"][0]["course"] == "CS1103"


def test_verify_calls_a_signed_out_session_logged_out_and_not_a_broken_adapter(
        run, adapters_dir):
    """The M1 finding, as a failure mode of promotion: the timed-out page serves
    a full menu, so the replay fails on it exactly as a broken adapter would.
    Reporting BROKEN here sends someone to debug the one thing that is fine."""
    res = _verify_from(run, "timeout.html", adapters_dir)
    assert res["verdict"] == "LOGGED_OUT"
    assert "session has been timeout" in res["session"]
    assert "handoff()" in res["detail"]
    assert "BROKEN" not in res["verdict"]


def test_verify_calls_it_broken_only_on_a_page_that_proves_it_is_signed_in(run, adapters_dir):
    res = _verify_from(run, "other.html", adapters_dir)
    assert res["verdict"] == "BROKEN" and res["failed_at"] == 1
    assert res["session"].startswith("AUTHED")
    assert "site has probably changed" in res["detail"]


def test_verify_says_unconfirmed_when_the_page_proves_nothing_either_way(run, adapters_dir):
    """Most pages carry no evidence. UNCONFIRMED has to be a real verdict, or
    every one of them reads as BROKEN."""
    res = _verify_from(run, "blank.html", adapters_dir)
    assert res["verdict"] == "UNCONFIRMED"
    assert res["session"].startswith("UNKNOWN")
    assert "signed-out" in res["detail"]


def test_verify_treats_no_rows_as_a_failure_and_triages_it_the_same_way(run, adapters_dir):
    async def go(page):
        k = Kernel(page)
        await _export(k)
        spec = adapters.load("aims_grades")
        spec["extract"]["skip"] = 99          # every row skipped: replays fine, returns nothing
        await page.goto((page.url.rsplit("/", 1)[0]) + "/menu.html")
        return await replay.verify(k, spec)

    res = run(go)
    assert res["verdict"] in ("BROKEN", "UNCONFIRMED")
    assert res["verb"] == "extract" and "no rows" in res["error"]


def test_a_replay_that_returns_no_rows_never_returns_them_alone(run, adapters_dir):
    """Zero rows is a real answer and is also what a dead session looks like, so
    it always comes back with the session check attached."""
    async def go(page):
        k = Kernel(page)
        await _export(k)
        spec = adapters.load("aims_grades")
        spec["extract"]["skip"] = 99
        await page.goto((page.url.rsplit("/", 1)[0]) + "/menu.html")
        return await replay.run_tool(k, spec)

    res = run(go)
    assert res["count"] == 0 and res["session"] and res["note"]


def test_verify_of_an_adapter_that_is_not_there_is_not_a_verdict_about_a_site(adapters_dir):
    res = asyncio.run(_verify_tool("nothing_here"))
    assert res["verdict"] == "UNCONFIRMED" and "no adapter called" in res["error"]


async def _verify_tool(name):
    async with Client(server.mcp) as c:
        return (await c.call_tool("verify", {"name": name})).data


# --- adapters are their own MCP tools -----------------------------------------

def test_a_promoted_adapter_is_registered_as_its_own_tool(run, adapters_dir):
    run(lambda p: _export(Kernel(p)))
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "aims_grades" in names
    assert "run_adapter" not in names, "the model calls the adapter, not a dispatcher"


def test_a_promoted_tool_carries_its_own_arguments_and_instructions(run, adapters_dir):
    async def go(page):
        k = Kernel(page)
        k.record(True)
        await k.view()
        await k.fill(ref_of(k, "Course code"), "x")
        await k.click(ref_of(k, "Search"))
        k.record(False)
        return await server._export(k, "course_rooms", "Rooms for a course",
                                    {"course": 0, "room": 1}, "#results", None)

    run(go, "search.html")
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "course_rooms")
    assert set(tool.parameters["properties"]) == {"course_code"}
    assert "Rooms for a course" in tool.description
    assert "verify('course_rooms')" in tool.description
    assert "typed rows" in tool.description


def test_calling_the_promoted_tool_returns_rows_with_no_digest_anywhere(run, adapters_dir,
                                                                        browser):
    async def go(page):
        k = Kernel(page)
        await _export(k)
        await page.goto((page.url.rsplit("/", 1)[0]) + "/menu.html")
        server._kernel = k
        try:
            async with Client(server.mcp) as c:
                return (await c.call_tool("aims_grades", {})).data
        finally:
            server._kernel = None

    res = run(go)
    assert res["ok"] and res["count"] == 3
    assert res["rows"][2] == {"course": "MA1200", "title": "Calculus", "grade": "A-"}
    assert "[1] link" not in str(res)


def test_adapters_on_the_volume_are_registered_at_startup(adapters_dir):
    adapters.save({"name": "aims_grades", "description": "grades",
                   "steps": [{"click": {"role": "link", "name": "Grade Display"}}],
                   "extract": {"kind": "table", "selector": "t", "fields": {"course": 0}},
                   "returns": [{"course": "str"}]})
    done, problems = server.register_adapters()
    assert done == ["aims_grades"] and problems == []
    assert "aims_grades" in {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_one_bad_file_is_skipped_loudly_and_the_rest_still_come_up(adapters_dir, caplog):
    adapters.save({"name": "good_one", "description": "fine",
                   "steps": [{"click": {"role": "link", "name": "X"}}],
                   "extract": {"kind": "table", "selector": "t", "fields": {"a": 0}},
                   "returns": [{"a": "str"}]})
    (adapters_dir / "bad_one.yaml").write_text(
        "name: bad_one\ndescription: d\nsteps: [{eval: {code: whatever}}]\n"
        "extract: {kind: table, selector: t, fields: {a: 0}}\nreturns: [{a: str}]\n")

    with caplog.at_level(logging.ERROR):
        done, problems = server.register_adapters()

    assert done == ["good_one"]
    assert [f for f, _ in problems] == ["bad_one.yaml"]
    assert "ADAPTER SKIPPED" in caplog.text and "bad_one.yaml" in caplog.text
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "good_one" in names and "bad_one" not in names
    assert {"view", "click", "fill", "export_adapter"} <= names, "the base tools came up"


def test_a_re_export_replaces_the_tool_rather_than_adding_a_second(run, adapters_dir):
    run(lambda p: _export(Kernel(p)))
    run(lambda p: _export(Kernel(p)))
    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert names.count("aims_grades") == 1


@pytest.mark.parametrize("name", sorted(adapters.RESERVED))
def test_a_reserved_name_never_reaches_the_tool_registry(run, adapters_dir, name):
    async def go(page):
        k = Kernel(page)
        with pytest.raises(adapters.AdapterError):
            await _export(k, name)
        return None

    before = {t.name: t.description for t in asyncio.run(server.mcp.list_tools())}
    run(go)
    after = {t.name: t.description for t in asyncio.run(server.mcp.list_tools())}
    assert before == after, "a base tool was shadowed or replaced"
    assert list(adapters_dir.iterdir()) == []


def test_the_whole_cycle_over_the_mcp_surface(run, adapters_dir):
    """record → explore → export_adapter → the new tool → verify, every call
    through the protocol, because the promotion tools' argument schemas are new
    and `fields` is the first mapping any tool here has taken."""
    async def go(page):
        k = Kernel(page)
        server._kernel = k
        try:
            async with Client(server.mcp) as c:
                async def call(tool, **args):
                    return (await c.call_tool(tool, args)).data

                out = {"record_on": await call("record", on=True)}
                await call("view")
                await call("click", ref=ref_of(k, "Student Record"))
                await call("click", ref=ref_of(k, "Grade Display"))
                out["record_off"] = await call("record", on=False)
                out["export"] = await call(
                    "export_adapter", name="aims_grades",
                    description="Current semester grades", fields=FIELDS)
                await page.goto((page.url.rsplit("/", 1)[0]) + "/menu.html")
                out["rows"] = await call("aims_grades")
                await page.goto((page.url.rsplit("/", 1)[0]) + "/menu.html")
                out["verify"] = await call("verify", name="aims_grades")
                return out
        finally:
            server._kernel = None

    out = run(go)
    assert "recording" in out["record_on"]
    assert "2 step(s)" in out["record_off"]
    assert out["export"]["ok"] and out["export"]["path"].endswith("aims_grades.yaml")
    assert out["rows"]["count"] == 3
    assert out["rows"]["rows"][0]["grade"] == "A"
    assert out["verify"]["verdict"] == "OK"


def test_export_over_the_surface_answers_a_bad_name_readably(run, adapters_dir):
    async def go(page):
        server._kernel = Kernel(page)
        try:
            async with Client(server.mcp) as c:
                return await c.call_tool("export_adapter", {
                    "name": "view", "description": "d", "fields": {"a": 0}})
        finally:
            server._kernel = None

    res = run(go)
    assert not res.is_error and "Traceback" not in str(res.data)
    assert "base tool" in res.data["error"]


# --- the wall -----------------------------------------------------------------

def _tree(root):
    out = {}
    for p in sorted(pathlib.Path(root).rglob("*.py")):
        out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_src_is_byte_identical_after_a_full_record_export_run_cycle(run, adapters_dir):
    before = _tree("src")
    assert before, "nothing was hashed"

    async def go(page):
        k = Kernel(page)
        await _export(k)
        await page.goto((page.url.rsplit("/", 1)[0]) + "/menu.html")
        rows = await replay.run_tool(k, adapters.load("aims_grades"))
        await page.goto((page.url.rsplit("/", 1)[0]) + "/menu.html")
        checked = await replay.verify(k, adapters.load("aims_grades"))
        return rows, checked

    rows, checked = run(go)
    assert rows["ok"] and checked["verdict"] == "OK"     # the cycle really happened
    assert _tree("src") == before
    assert (adapters_dir / "aims_grades.yaml").exists()  # it wrote somewhere, just not there


def test_the_audit_log_gains_one_line_per_export_and_holds_no_values(run, adapters_dir):
    run(lambda p: _export(Kernel(p)))
    run(lambda p: _export(Kernel(p, )))
    lines = open(adapters.audit_path()).read().splitlines()
    assert len(lines) == 2
    assert all('"name": "aims_grades"' in ln for ln in lines)
    assert SECRET not in "".join(lines)
