"""FastMCP wiring for the kernel: ten base tools over one browser, plus one tool
per promoted adapter.

One browser, one page, and no `workspace` argument anywhere: the CDP target is
configuration (KERNEL_CDP), not something the model picks. The surface stays
small on purpose -- no open(), no evaluate(), no screenshot -- so what M1
measured is the kernel and not a smaller Playwright server.

An adapter is registered as its own tool rather than reached through a
run_adapter(name) dispatcher. Three reasons, in order of weight: the model sees
`aims_grades()` in its tool list and never has to be told an adapter exists; an
adapter that types into a field needs its own typed arguments and a dispatcher
can only offer a bag of strings; and a dispatcher's first argument is a name the
model chooses, which is precisely the shape that lets a wrong name reach the
wrong thing. Here a name is resolved once, at registration, against a set that
already refused every base tool's name.

Not in here, deliberately: no db, no AGENT_ACTION logging. The kernel is not
part of the soak experiment.
"""
import argparse
import asyncio
import inspect
import logging
import os
import sys
import traceback

from fastmcp import FastMCP
from playwright.async_api import async_playwright

from kernel import adapters, base, digest, replay

log = logging.getLogger("kernel.server")

DEFAULT_CDP = "work:9223"

mcp = FastMCP("kernel", instructions=(
    "One browser, one page. view() returns the page as a numbered list of the "
    "elements you can act on; click(ref), fill(ref, value) and select(ref, option) take one of "
    "those numbers and return the page's new digest. A ref only means anything against the "
    "digest it came from — any change to the page renumbers them, so act on the most recent "
    "digest and call view() again whenever a tool tells you a ref is stale. There is no way to "
    "navigate by URL: the browser starts on whatever page it is already showing, and you move "
    "by clicking. session_status() answers the one question a digest cannot: a timed-out "
    "session can serve a page that looks entirely normal, so ask it before reporting that "
    "something is missing or empty. This browser belongs to a person who can see it and take "
    "it over: when you meet a password, a 2FA prompt or a captcha, do not guess and do not "
    "give up — call handoff(reason), which asks them to do that step and blocks until they "
    "have. Exploring a path is expensive and repeating it should be free: when you have found "
    "a route worth keeping, walk it once with record(true), then export_adapter() turns it "
    "into a tool of its own that replays it and returns typed rows with no digests in "
    "between. Any tool here that is not one of the ten named above is such an adapter — "
    "prefer it to walking the path by hand, and if it comes back wrong call verify(name), "
    "which says whether the site changed or you are merely signed out."))

# One page can only do one thing at a time, so the lock covers whole tool calls
# rather than just the connect: a click landing between another call's view()
# and its ref resolution is exactly the race base.py refuses to guess about.
_lock = asyncio.Lock()
_pw = None
_kernel = None


async def _attach():
    """The Kernel, connected on first use and re-connected after a drop.

    Lazily rather than at startup: `work` is a separate container that may not
    be up yet, and a server that refuses to start is a crash loop nobody reads.
    The same laziness is the recovery path -- a dead CDP connection costs one
    failed tool call, not a restart, because _run() clears the cache and the
    next call lands back here.
    """
    global _pw, _kernel
    if _kernel is not None:
        return _kernel
    cdp = os.environ.get("KERNEL_CDP", DEFAULT_CDP)
    try:
        _pw = _pw or await async_playwright().start()
        _kernel = await base.attach(
            _pw, cdp,
            tab=int(os.environ.get("KERNEL_TAB", "0")),
            max_tokens=int(os.environ.get("KERNEL_MAX_TOKENS", digest.DEFAULT_MAX_TOKENS)))
    except base.KernelError:
        raise
    except Exception as exc:  # noqa: BLE001 - unreachable browser is the model's to retry
        raise base.KernelError(
            f"cannot reach the browser at {cdp}: {type(exc).__name__} — it may still be "
            "starting; try the same call again in a moment") from exc
    return _kernel


def _scrub(text, secret):
    """fill()'s value must not survive into a log line, not even inside a
    traceback that quoted it."""
    return text.replace(secret, "***") if secret else text


async def _run(name, op, *, secret=None):
    """Every tool body goes through here.

    A KernelError is written for the model, so it is the result. Anything else
    is a bug or a dropped connection: swallowing it silently would hide the bug,
    and returning the traceback would spend the model's context on something it
    cannot act on -- so the traceback goes to the log, the model gets one line,
    and the cached connection is dropped because it is the prime suspect.

    The lock is held for the whole call, handoff() included. That is minutes,
    deliberately: while the person is typing their password into this browser,
    no other call has any business clicking in it.
    """
    global _kernel
    async with _lock:
        try:
            return await op(await _attach())
        except (base.KernelError, adapters.AdapterError) as exc:
            # Both are written for the model to read and act on. AdapterError is
            # not a subclass of KernelError only because adapters.py is kept
            # free of playwright so the guardrails stay testable without a
            # browser; they are the same contract and get the same handling.
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - the model gets a result, never a stack trace
            _kernel = None
            log.error("%s failed:\n%s", name, _scrub(traceback.format_exc(), secret))
            return (f"{name} failed unexpectedly ({type(exc).__name__}); the server logged the "
                    "details. Call view() to see where the page actually is.")


# --- the tools --------------------------------------------------------------
# The docstrings are the model's instructions, so every one of them says where
# refs come from and when they stop being true.

@mcp.tool
async def view() -> str:
    """Show the current page as the numbered things you can act on.

    Returns the url, the title, and one `[ref] role name` line per interactive
    element, followed by a count of anything left out. Refs are 1-based and
    belong to this digest only: when the page changes they are renumbered.
    Call this first — click() and fill() take refs, and only refs from the most
    recent digest are valid.
    """
    return await _run("view", lambda k: k.view())


@mcp.tool
async def click(ref: int) -> str:
    """Click the element at `ref`, then return the page as it now is.

    `ref` is a number from the most recent digest (from view(), click() or
    fill()). If the page moved under that ref the click is refused instead of
    guessed at — call view() again and use the numbers it returns. The return
    value is a fresh digest whose refs replace the ones you were using.
    """
    return await _run("click", lambda k: k.click(ref))


@mcp.tool
async def fill(ref: int, value: str) -> str:
    """Type `value` into the field at `ref`, replacing what is there, then
    return the page as it now is.

    `ref` comes from the most recent digest, under the same rule as click():
    stale refs are refused, not guessed. Only fields take a value (textbox,
    searchbox, combobox and the like); use click() for links and buttons.
    `value` is never logged and never echoed back in a result.
    """
    return await _run("fill", lambda k: k.fill(ref, value), secret=value)


@mcp.tool
async def select(ref: int, option: str) -> str:
    """Choose `option` in the dropdown at `ref`, then return the page as it now
    is.

    `ref` comes from the most recent digest, under the same rule as click():
    stale refs are refused, not guessed. Only a dropdown (shown as `combobox`)
    can be selected from — fill() cannot set one. `option` is matched against
    the dropdown's visible labels first and its underlying values second; if it
    matches neither, the error lists the options that really exist, so send one
    of those back instead of guessing a second time.
    """
    return await _run("select", lambda k: k.select(ref, option))


@mcp.tool
async def session_status() -> str:
    """Say whether this page is a signed-in page: AUTHED, LOGGED_OUT or UNKNOWN,
    with the reason.

    Takes no ref, changes nothing and leaves your refs valid. A session that has
    idled out can serve a page that digests exactly like a live one — same menu,
    same refs, same everything — so ask this before reporting that a page is
    empty or that data is missing. LOGGED_OUT means what you read is not about
    the account at all; UNKNOWN means this page carries no evidence either way
    and is the honest answer for most pages, so treat it as "not confirmed"
    rather than as "fine".
    """
    return await _run("session_status", lambda k: k.session_status())


@mcp.tool
async def read(contains: str | None = None) -> str:
    """Read the page's visible text — the part view() counts but does not quote.

    view() shows what you can *act on*; what a page is *for* — a grade table, a
    balance, a timetable — is text and has no ref. Call this when you need the
    content itself rather than a way through the page. Takes no ref.

    `contains` keeps only the lines holding that substring, case-insensitively,
    which is the cheap way to pull one row out of a long table. Long pages are
    truncated with the number of omitted lines stated.
    """
    return await _run("read", lambda k: k.read(contains))


@mcp.tool
async def handoff(reason: str) -> str:
    """Ask the person to take this browser over, and wait until they say they
    are done.

    Use this the moment you hit something only they can clear: a password, a
    one-time code, a 2FA push, a captcha, a consent screen, a session that
    session_status() calls LOGGED_OUT. Do not try to type a credential — you do
    not have one — and do not report the task as impossible. Call this instead.

    `reason` is shown to them on their phone and is the only thing telling them
    what to do, so write it for a person: what is blocking you, and what you
    need them to finish. It is not a place for a stack trace.

    This call blocks — for minutes, until they press Done or it times out — and
    then returns what happened followed by a fresh view() of wherever the page
    has ended up. Takes no ref, and every ref you were holding is dead
    afterwards: use the digest this returns. If it times out, read that digest
    before deciding you are still stuck; they may have done the work and not
    pressed the button.
    """
    return await _run("handoff", lambda k: k.handoff(reason))


# --- promotion ----------------------------------------------------------------

async def _sync(value):
    """record() is the one tool with nothing to await. It still goes through
    _run() so it takes the same lock as everything else: the recorder is state
    on the Kernel, and turning it on halfway through another call's click is the
    same race base.py refuses to guess about."""
    return value


@mcp.tool
async def record(on: bool) -> str:
    """Start (true) or stop (false) recording the path you are walking.

    While it is on, every click(), fill() and select() that *succeeds* is kept
    as a step. What is kept is the element's role and name — the same pair
    view() showed you and click() checks before it acts — and never the ref,
    which is only a position and means nothing on the next page load. A fill's
    value is never kept: it becomes an argument of the tool this turns into.

    record(true) starts a fresh recording and discards any earlier one. Walk the
    path once, from a page you could get back to, then record(false) and
    export_adapter(). A fill into a password field is deliberately not recorded
    and blocks the export — an adapter must not type a credential, so use
    handoff() for the sign-in and record only what comes after it.
    """
    return await _run("record", lambda k: _sync(k.record(on)))


@mcp.tool
async def export_adapter(name: str, description: str, fields: dict[str, int],
                         selector: str | None = None,
                         types: dict[str, str] | None = None) -> dict:
    """Promote what you just recorded into a tool of its own that returns rows.

    Call this while the browser is still on the page the path ends at — the
    table is read from the page in front of you to prove the adapter works
    before anything is written.

    `name` becomes both a filename and a tool name: 3–41 characters,
    `^[a-z][a-z0-9_]{2,40}$`, and never the name of one of the base tools.
    `description` is what the new tool's own docstring will say.
    `fields` maps each column you want to its position in the table row, counting
    from 0 — `{"course": 0, "title": 1, "grade": 3}`. read() shows you the rows
    as text, which is how you count them.
    `selector` is the CSS selector of the table; leave it out and the page's
    largest data table is used.
    `types` optionally makes a column an `int` or a `float` instead of `str`.

    Returns the path written, the tool's arguments, and the first rows it
    extracted, so you can see immediately whether the columns are the ones you
    meant. From here on, call that tool instead of walking the path.
    """
    res = await _run("export_adapter",
                     lambda k: _export(k, name, description, fields, selector, types))
    return _as_dict(res, "export_adapter")


async def _export(kernel, name, description, fields, selector, types):
    # The name first, before anything about the recording: a refused name is the
    # refusal that matters most, and telling the model its recording is empty
    # when its real problem is that it tried to call an adapter `view` sends it
    # to fix the wrong thing.
    adapters.validate_name(name)
    steps, dropped = kernel.recording()
    if dropped:
        raise adapters.AdapterError(
            f"{dropped} fill(s) into a password field were left out of this recording, so the "
            "path has a hole in it and exporting would promote a route that cannot work. An "
            "adapter may not type a credential. Use handoff() for the sign-in and record only "
            "the part after it.")
    if not steps:
        raise adapters.AdapterError(
            "nothing has been recorded. Call record(true), walk the path with click(), fill() "
            "and select(), then call this again — an adapter with no steps would extract from "
            "whatever page happened to be open.")
    if not selector:
        best = await kernel.best_table()
        if not best:
            raise adapters.AdapterError(
                "this page has no table to extract from, and no selector was given. Get to the "
                "page the data is on before exporting, or pass the selector yourself.")
        selector, found = best
        log.info("export_adapter %s: chose %s (%d rows)", name, selector, found)
    spec = adapters.from_recording(
        name, description, steps,
        {"kind": "table", "selector": selector, "fields": fields or {}}, types)

    # Prove it on the page it was built from, before it is written. An adapter
    # that extracts nothing on day one is not worth the file, and finding that
    # out now costs one evaluate instead of a debugging session next week.
    try:
        rows, short = await replay.extract(kernel, spec)
    except adapters.AdapterError as exc:
        raise adapters.AdapterError(
            f"{exc} Nothing was written. Check `fields` against what read() shows you on this "
            "page, or pass `selector` for the table you actually mean.") from exc
    if not rows:
        raise adapters.AdapterError(
            f"{selector!r} gave no rows this adapter could read on the page it was just built "
            "from, so it would be broken the moment it was written. Nothing was written.")

    path = adapters.save(spec)
    register(spec)
    log.info("export_adapter wrote %s (%d steps)", path, len(spec["steps"]))
    return {"ok": True, "adapter": name, "path": path, "steps": len(spec["steps"]),
            "selector": selector, "arguments": spec.get("params", []),
            "columns": list(spec["extract"]["fields"]), "sample": rows[:replay.SAMPLE],
            "skipped_rows": short,
            "note": f"{name}() is registered now and returns these rows without a digest. "
                    "The recording is still held, so you can export a second view of the same "
                    "path under another name."}


@mcp.tool
async def verify(name: str) -> dict:
    """Re-run a promoted adapter and say whether it still works.

    Sites change, and an adapter that quietly returns nothing is worse than one
    that fails loudly. Run this from the page the adapter starts on — it replays
    from wherever the browser already is.

    `verdict` is one of:
      OK          — rows came back, with a sample of them.
      BROKEN      — it failed on a page that proves it is signed in, so the site
                    has changed. `failed_at` says which step.
      LOGGED_OUT  — it failed, but the session is signed out, so this says
                    nothing about the adapter. Call handoff(), then verify again.
      UNCONFIRMED — it failed and the page carries no evidence either way. A
                    timed-out portal serves a page that looks entirely normal, so
                    this is not the same as BROKEN and must not be debugged as if
                    it were.
    """
    try:
        spec = adapters.load(name)
    except adapters.AdapterError as exc:
        return {"adapter": name, "verdict": "UNCONFIRMED", "error": str(exc),
                "detail": "nothing was replayed, so this is about the adapter file rather "
                          "than the site."}
    return _as_dict(await _run("verify", lambda k: replay.verify(k, spec)), "verify")


def _as_dict(res, name):
    """_run() answers a KernelError with the line written for the model. These
    tools answer in objects, so a line becomes one rather than a type surprise."""
    return res if isinstance(res, dict) else {"ok": False, "adapter": name, "error": res}


# --- adapters as tools ---------------------------------------------------------

def _doc(spec):
    """The promoted tool's own instructions. Written for the model, like the
    base tools' -- what it returns, and what to do when it stops working."""
    columns = ", ".join(f"{k} ({t})" for k, t in spec["returns"][0].items())
    args = (f" Takes {', '.join(spec['params'])}, which it types into the fields on the way."
            if spec.get("params") else "")
    return (f"{spec['description']}\n\n"
            f"Replays a path that was recorded once through this browser and returns typed "
            f"rows: {columns}.{args} It costs you no digests — call it instead of walking the "
            f"pages by hand. It replays from wherever the browser already is, so get to the "
            f"page it starts from first.\n\n"
            f"If it comes back with `ok: false`, or with no rows, read `note` before assuming "
            f"the adapter is wrong: a signed-out session serves pages that look entirely "
            f"normal. verify('{spec['name']}') draws that line properly.")


def _adapter_tool(spec):
    """A callable with this adapter's own name, signature and docstring."""
    name = spec["name"]
    params = spec.get("params", [])

    async def fn(**values):
        return _as_dict(await _run(name, lambda k: replay.run_tool(k, spec, values)), name)

    fn.__name__ = name
    fn.__doc__ = _doc(spec)
    fn.__signature__ = inspect.Signature(
        [inspect.Parameter(p, inspect.Parameter.KEYWORD_ONLY, annotation=str) for p in params],
        return_annotation=dict)
    fn.__annotations__ = {p: str for p in params} | {"return": dict}
    return fn


def register(spec):
    """Give this adapter its own tool, replacing an earlier version of itself.

    The reserved-name check is made again here even though validate() already
    made it. This is the one function that can add a name to the live tool
    registry, and a guarantee about the base tools should not rest on every
    caller having gone through the right door.
    """
    name = spec["name"]
    if name in adapters.RESERVED:
        raise adapters.AdapterError(f"{name!r} is a base tool and cannot be registered")
    try:
        mcp.local_provider.remove_tool(name)   # a re-export replaces, never shadows
    except Exception:  # noqa: BLE001 - not registered yet is the normal case
        pass
    mcp.tool(_adapter_tool(spec))
    return name


def register_adapters():
    """Every adapter on the volume, at startup. Returns (registered, problems).

    A file that fails validation is skipped with a loud line and nothing else.
    One bad adapter must not cost the other nine and the ten base tools, and a
    server that refuses to start is a crash loop nobody reads.
    """
    specs, problems = adapters.load_all()
    for entry, why in problems:
        log.error("ADAPTER SKIPPED %s — %s", entry, why)
    done = []
    for spec in specs:
        try:
            done.append(register(spec))
        except Exception as exc:  # noqa: BLE001 - startup survives anything in a spec
            problems.append((spec.get("name", "?"), str(exc)))
            log.error("ADAPTER SKIPPED %s — could not be registered: %s", spec.get("name"), exc)
    log.info("adapters: %d promoted (%s), %d skipped", len(done), ", ".join(done) or "none",
             len(problems))
    return done, problems


try:
    register_adapters()
except Exception as exc:  # noqa: BLE001 - the base tools come up whatever is on the volume
    log.error("adapters could not be loaded at all (%s: %s) — the base tools are unaffected",
              type(exc).__name__, exc)


# --- transports -------------------------------------------------------------
# One server definition, two ways in: http is the deployed path, stdio is what
# Claude Desktop attaches to over an SSH tunnel.

def main(argv=None):
    logging.basicConfig(level=os.environ.get("KERNEL_LOG", "INFO").upper(), stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="MCP kernel: view / click / fill / select / read / session_status / "
                    "handoff / record / export_adapter / verify over one browser, plus one "
                    "tool per promoted adapter")
    ap.add_argument("--transport", choices=("http", "stdio"),
                    default=os.environ.get("KERNEL_TRANSPORT", "http"))
    args = ap.parse_args(argv)
    # Again, now that logging is configured: the import-time pass keeps
    # list_tools() honest for anything that mounts this module without calling
    # main(), but its "ADAPTER SKIPPED" lines would have gone to a handler that
    # did not exist yet, and a loud line nobody can see is not a loud line.
    register_adapters()
    if args.transport == "stdio":
        # stdout is the protocol here: logs went to stderr above, banner off.
        asyncio.run(mcp.run_async(transport="stdio", show_banner=False))
    else:
        asyncio.run(mcp.run_async(transport="http", show_banner=False,
                                  host=os.environ.get("KERNEL_HOST", "0.0.0.0"),
                                  port=int(os.environ.get("KERNEL_PORT", "8000"))))


if __name__ == "__main__":
    main()
