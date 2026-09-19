"""The adapter runtime: a fixed interpreter that walks a validated spec.

Nothing here reads the spec as anything but data. The verbs are a closed match
in _act(), the only DOM access is through the Kernel methods the base tools
already use, and the one string an adapter contributes to the page -- the
extraction selector -- goes in as an argument to a fixed CSS query. There is no
branch in this file that a spec can reach that ends in code running.

Replay drives view() before each step and then calls the ordinary tool. That
costs a round trip per step and buys the whole of base.py's staleness guarantee:
a recorded (role, name) is turned into a ref against a *fresh* digest, and
_resolve() then verifies that same pair before it acts. Two checks of one fact,
which is the cheapest way to be sure the element replayed is the element
recorded. None of it reaches the model -- that is the milestone: the model calls
one tool and gets rows.
"""
import logging

from kernel.adapters import TYPES, AdapterError

log = logging.getLogger("kernel.replay")

VERDICTS = ("OK", "BROKEN", "LOGGED_OUT", "UNCONFIRMED")
# How many rows verify() quotes back. Enough to see the shape is right, few
# enough that verify stays a cheap thing to run often.
SAMPLE = 3


class StepError(AdapterError):
    """A step that did not happen, named. `step` is 1-based, 0 for extraction."""

    def __init__(self, message, step, verb):
        super().__init__(message)
        self.step = step
        self.verb = verb


def _label(step):
    verb, body = next(iter(step.items()))
    if verb == "wait":
        return verb, f"wait {body['ms']}ms"
    if verb == "read":
        return verb, f"read containing {body['contains']!r}"
    where = f" (#{body['occurrence']})" if body.get("occurrence", 1) > 1 else ""
    return verb, f"{verb} {body['role']} {body['name']!r}{where}"


def _ref(kernel, body):
    """The ref of the recorded element on the page as it is now, or None.

    Matched on (role, name) -- the pair the digest showed and _resolve() checks
    -- and then by occurrence, because a page may offer the same role and name
    twice and a ref is the only thing that told them apart at record time.
    """
    hits = [e for e in kernel.shown() if (e.role, e.name) == (body["role"], body["name"])]
    want = body.get("occurrence", 1)
    return hits[want - 1].ref if len(hits) >= want else None


async def _act(kernel, step, i, values):
    """One step. Every path out that is not success names the step."""
    verb, body = next(iter(step.items()))
    _, said = _label(step)
    where = f"step {i} ({said})"

    if verb == "wait":
        return await kernel.wait(body["ms"])
    if verb == "read":
        got = await kernel.read(contains=body["contains"])
        if got.startswith("no line on this page contains"):
            raise StepError(f"{where} failed: no line on this page contains "
                            f"{body['contains']!r}.", i, verb)
        return None

    await kernel.view()   # refs for this page, this moment; discarded after use
    ref = _ref(kernel, body)
    if ref is None:
        hint = (" The browser may simply not be on the page this adapter starts from: an "
                "adapter replays from wherever the page already is." if i == 1 else "")
        raise StepError(f"{where} failed: this page has no {body['role']} called "
                        f"{body['name']!r}.{hint}", i, verb)
    if verb == "click":
        await kernel.click(ref)
    elif verb == "select":
        await kernel.select(ref, body["option"])
    else:
        # The value is the caller's argument. It was never on disk and it is not
        # logged here either -- only the field it went into.
        await kernel.fill(ref, values[body["value_from"]])
    return None


def _coerce(text, kind, column, i):
    if kind == "str":
        return text
    try:
        return TYPES[kind](text)
    except ValueError:
        raise StepError(f"extract failed: row {i} column {column!r} is {text!r}, which is not "
                        f"a {kind}. Either the table has changed shape or the column is "
                        f"mistyped in the adapter.", 0, "extract") from None


async def extract(kernel, spec):
    """The typed rows, or a StepError naming extraction rather than a step.

    A row too short to carry every field is dropped and counted, never guessed
    at: a "Total" line under a grade table has two cells and is not a grade. The
    count comes back with the rows in the digest's own style -- own up to what
    was left out rather than let it look like a page that simply ends.
    """
    extract, shape = spec["extract"], spec["returns"][0]
    fields = extract["fields"]
    try:
        raw = await kernel.table_rows(extract["selector"], extract["skip"])
    except Exception as exc:  # noqa: BLE001 - a bad selector is the adapter's fault, named
        raise StepError(f"extract failed: {exc}. The page's table is not where the adapter "
                        f"says it is ({extract['selector']!r}) — the site has probably "
                        "changed.", 0, "extract") from exc
    width = max(fields.values()) + 1
    rows, short = [], 0
    for cells in raw:
        if len(cells) < width:
            short += 1
            continue
        rows.append({key: _coerce(cells[col], shape[key], key, len(rows) + 1)
                     for key, col in fields.items()})
    if not rows and raw:
        raise StepError(f"extract failed: {extract['selector']!r} matched a table with "
                        f"{len(raw)} row(s), but none of them has the {width} columns this "
                        "adapter reads. The table's shape has changed.", 0, "extract")
    return rows, short


async def run(kernel, spec, values=None):
    """Replay `spec` and return typed rows. The whole point of the milestone:
    one call, structured data, no digest anywhere in the model's context."""
    values = values or {}
    missing = [p for p in spec.get("params", []) if not str(values.get(p, "")).strip()]
    if missing:
        raise AdapterError(f"{spec['name']} needs {missing} — the adapter types those into "
                           "fields on the way, and they are arguments rather than part of the "
                           "adapter because a typed value is never written to disk.")
    for i, step in enumerate(spec["steps"], 1):
        await _act(kernel, step, i, values)
    rows, short = await extract(kernel, spec)
    log.info("adapter %s returned %d row(s)", spec["name"], len(rows))
    return rows, short


async def run_tool(kernel, spec, values=None):
    """run() shaped as a tool result: rows, or a readable failure, never a raise.

    Zero rows is not an error -- a semester with no grades yet is a real answer
    -- but it is the answer that looks identical to a dead session, so it never
    comes back alone. session_status() is attached to it, which is the one thing
    that can tell a model which of the two it is holding.
    """
    try:
        rows, short = await run(kernel, spec, values)
    except AdapterError as exc:
        out = {"ok": False, "adapter": spec["name"], "rows": [], "count": 0, "error": str(exc)}
        out["session"] = await _session(kernel)
        out["note"] = _triage(out["session"], f"verify('{spec['name']}')")
        return out
    out = {"ok": True, "adapter": spec["name"], "count": len(rows), "rows": rows}
    if short:
        out["skipped_rows"] = short     # stated, as the digest states what it cut
    if not rows:
        out["session"] = await _session(kernel)
        out["note"] = ("the adapter ran and its table held no rows this can read. " +
                       _triage(out["session"], f"verify('{spec['name']}')"))
    return out


async def _session(kernel):
    try:
        return await kernel.session_status()
    except Exception as exc:  # noqa: BLE001 - a failed check must not eat the real failure
        return f"UNKNOWN — the session could not be checked ({type(exc).__name__})"


def _triage(session, retry):
    """Broken, or signed out? The distinction verify() exists to draw.

    An expired portal session serves a page that digests exactly like a live one
    -- that is the M1 finding this whole tool set was reshaped around -- so a
    replay that finds nothing on it is *not* evidence the adapter is wrong.
    Reporting a broken adapter to someone who is merely signed out sends them to
    debug the one thing that is fine.
    """
    if session.startswith("LOGGED_OUT"):
        return (f"the session is signed out, so this says nothing about the adapter: {session} "
                f"Call handoff() to get it signed in, then {retry}.")
    if session.startswith("AUTHED"):
        return (f"the session is live ({session.split(' — ')[0]}), so this is the adapter or "
                "the site, not the sign-in. The site has probably changed.")
    return (f"the session could not be confirmed: {session} Until it is, this cannot be told "
            "apart from a signed-out page — get to a page that shows a sign-out control, ask "
            f"session_status() again, then {retry}.")


def verdict(session):
    if session.startswith("LOGGED_OUT"):
        return "LOGGED_OUT"
    return "BROKEN" if session.startswith("AUTHED") else "UNCONFIRMED"


async def verify(kernel, spec):
    """Re-run the adapter and say whether it still works, and if not, why.

    Sites change and an adapter that silently returns nothing is worse than one
    that fails loudly -- but the failure has to name the right culprit. Every
    way this ends badly asks session_status() *on the page the failure happened
    on*, because that is the page that lied.
    """
    name = spec["name"]
    try:
        rows, short = await run(kernel, spec)
    except StepError as exc:
        session = await _session(kernel)
        return {"adapter": name, "verdict": verdict(session), "failed_at": exc.step,
                "verb": exc.verb, "error": str(exc), "session": session,
                "detail": _triage(session, f"verify('{name}')")}
    except AdapterError as exc:
        # Missing arguments: nothing was replayed, so nothing was learnt about
        # the site. Not a verdict on the adapter and not dressed up as one.
        return {"adapter": name, "verdict": "UNCONFIRMED", "failed_at": 0, "verb": "arguments",
                "error": str(exc), "session": "",
                "detail": "this adapter takes arguments, so it cannot be verified without "
                          "them. Call it directly with real values instead."}
    if not rows:
        session = await _session(kernel)
        return {"adapter": name, "verdict": verdict(session), "failed_at": 0, "verb": "extract",
                "error": f"every step replayed, but {spec['extract']['selector']!r} yielded no "
                         "rows.", "session": session,
                "detail": _triage(session, f"verify('{name}')")}
    return {"adapter": name, "verdict": "OK", "count": len(rows),
            "columns": list(spec["extract"]["fields"]),
            "sample": rows[:SAMPLE], "skipped_rows": short,
            "detail": f"{len(rows)} row(s) came back with the columns the adapter promises. "
                      "The page this left the browser on is wherever the last step landed."}
