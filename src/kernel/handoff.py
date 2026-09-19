"""The handoff: the agent stops, a human drives, the agent picks the page up.

The Done button is in a browser and the call waiting on it is inside an MCP tool
-- which, on stdio, is a `docker compose exec` pipe with no listener of its own,
so no HTTP request can ever reach it. The two sides therefore share one file on a
volume: this module writes it and polls it, control.py serves the page that
clears it.

Every read is total. The reader is a live tool call and the writer is another
process, so a missing, empty or half-written file has to read as "nothing
pending" -- a traceback there would turn a race into a failed task. Writes go
through a temp file and os.replace for the same reason: a reader must only ever
see a whole record, old or new.

Not in here, deliberately: no playwright (control.py imports this and has no
business starting a browser client), no db, and not src/notify.py -- which does
almost exactly the Telegram call below but imports db.py, and with it the soak
experiment the kernel is kept out of.
"""
import asyncio
import dataclasses
import datetime as dt
import json
import logging
import os
import tempfile
import time
import urllib.request

from kernel import digest

log = logging.getLogger("kernel.handoff")

DEFAULT_PATH = "/state/handoff.json"

# Ten minutes. The floor is human: a Telegram message can sit unseen for several
# minutes before the phone is even picked up, so anything under ~5 min would fire
# on notification latency rather than on the person failing. The ceiling is the
# transport: an MCP client and any proxy in front of it give a tool call a finite
# budget, and a call that blocks past it is severed with nobody left to clear the
# state file -- the handoff would leak and the viewer would prompt forever. Ten
# minutes covers a real SSO with a mislaid phone and still returns a result the
# model can read. It is a checkpoint, not a failure: the message says to call
# handoff() again, which re-sends the notification.
DEFAULT_TIMEOUT_S = 600
# One second. 600 stat+reads over the worst case is nothing, and a human who
# presses Done and looks up cannot tell 1s from instant. Polling at 100ms would
# buy no perceptible latency and ten times the reads.
DEFAULT_POLL_S = 1.0
TELEGRAM_TIMEOUT_S = 10
# A reason is a sentence for a person on a phone, not a report.
MAX_REASON = 400

OUTCOMES = ("done", "timeout", "superseded")


def path(p=None):
    return p or os.environ.get("KERNEL_STATE_FILE", DEFAULT_PATH)


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclasses.dataclass(frozen=True)
class Handoff:
    """One handoff, as both processes see it on disk.

    `id` is what makes Done safe. The page posts back the id it displayed, so a
    stale tab cannot clear a handoff that was recorded after it loaded, and the
    waiting call can tell "mine was cleared" from "mine was replaced".
    """
    id: str = ""
    pending: bool = False
    reason: str = ""
    since: str = ""
    resolved: str = ""        # "", "done" or "timeout"
    resolved_at: str = ""


NONE = Handoff()


@dataclasses.dataclass(frozen=True)
class Result:
    """What wait() hands back: the outcome, and the line written for the model."""
    outcome: str
    message: str
    state: Handoff = NONE
    notified: bool = False


def clean_reason(reason):
    """The reason, flattened and capped.

    digest.clean() because this is the mirror of the problem it was written for.
    There the page's text reaches the model; here the model's text reaches a
    human's browser and a human's phone. Both are text from somewhere that does
    not answer to us, so both get the same treatment -- newlines collapse so the
    reason cannot forge a line of its own, and angle brackets go so nothing in it
    can look like markup wherever it is eventually rendered.
    """
    r = digest.clean(reason)
    return r[:MAX_REASON - 1] + "…" if len(r) > MAX_REASON else r


# --- the state file ---------------------------------------------------------

def read(p=None):
    """The handoff on disk, or NONE. Never raises, for any file or no file."""
    try:
        with open(path(p), encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return NONE       # missing, empty, half-written, or not JSON at all
    if not isinstance(obj, dict):
        return NONE
    ident = _str(obj.get("id"))
    return Handoff(
        id=ident,
        # Pending needs an id: a record we cannot name is a record Done could
        # never clear, so honouring it would block until the timeout every time.
        pending=bool(obj.get("pending")) and bool(ident),
        reason=clean_reason(obj.get("reason")),
        since=_str(obj.get("since")),
        resolved=_str(obj.get("resolved")),
        resolved_at=_str(obj.get("resolved_at")),
    )


def _str(v):
    return v if isinstance(v, str) else ""


def write(state, p=None):
    """Replace the state file with `state`, whole or not at all.

    The temp file is made in the destination's own directory because os.replace
    is only atomic within one filesystem, and /state is a volume.
    """
    dest = path(p)
    folder = os.path.dirname(dest) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".handoff-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(state), f)
            f.flush()
            os.fsync(f.fileno())   # replace a whole file, not a whole filename
        os.replace(tmp, dest)
    except BaseException:
        _unlink(tmp)
        raise
    return state


def _unlink(p):
    try:
        os.unlink(p)
    except OSError:
        pass


def start(reason, p=None):
    """Record a new pending handoff and return it. Raises OSError if it cannot.

    Unconditional: whatever was there is stale by definition, because the only
    call that could still be waiting on it is this one's predecessor and it has
    either returned or been severed.
    """
    return write(Handoff(id=os.urandom(8).hex(), pending=True,
                         reason=clean_reason(reason), since=_now()), p)


def done(handoff_id=None, p=None):
    """Clear the pending handoff. Returns (state, whether this call cleared it).

    Idempotent by construction: nothing pending is not an error, it is the
    answer. `handoff_id`, when given, must be the current one -- a phone that
    has had the page open since the last handoff must not be able to clear the
    next one with a stale tap.
    """
    cur = read(p)
    if not cur.pending or (handoff_id and handoff_id != cur.id):
        return cur, False
    return write(dataclasses.replace(cur, pending=False, resolved="done",
                                     resolved_at=_now()), p), True


# --- telegram ---------------------------------------------------------------
# Six lines rather than an import. src/notify.py sends the same request and
# pulls db.py in behind it.

def telegram(text):
    """Send `text`. False if there is nothing configured to send it with.

    No parse_mode, so Telegram renders the body literally: a reason containing
    markup is a reason, not formatting, and not a way to forge a link.
    """
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — handoff not announced")
        return False
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json.dumps({"chat_id": chat, "text": text}).encode(),
                                 {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=TELEGRAM_TIMEOUT_S):
        pass
    return True


def _announce(state, send, viewer_url):
    """Tell the person. Any failure here is logged and swallowed.

    A dead token must cost the notification and nothing else: the state file is
    already written, the viewer already shows the prompt, and a person who is
    watching the stream can still finish the job. Failing the handoff because
    the messenger failed would turn a degraded path into no path.
    """
    text = (f"Anywhere Live needs you:\n\n{state.reason}\n\n"
            "Open the viewer, do the bit only you can do, then press Done.")
    if viewer_url:
        text += f"\n\n{viewer_url}"
    try:
        return bool(send(text))
    except Exception as exc:  # noqa: BLE001 - notification is best effort, always
        log.warning("handoff notification failed (%s: %s) — waiting anyway",
                    type(exc).__name__, exc)
        return False


# --- the wait ---------------------------------------------------------------

async def wait(reason, *, p=None, timeout=None, poll=None, send=None, viewer_url=None):
    """Record a handoff, announce it, and block until it is cleared.

    Returns a Result whichever way it ends; the only way out through an
    exception is being unable to write the state file at all, which means nobody
    was ever asked and waiting would be theatre.
    """
    timeout = _num(timeout, "KERNEL_HANDOFF_TIMEOUT", DEFAULT_TIMEOUT_S)
    poll = _num(poll, "KERNEL_HANDOFF_POLL", DEFAULT_POLL_S)
    send = telegram if send is None else send   # `or` would drop a falsy callable
    viewer_url = viewer_url if viewer_url is not None else os.environ.get("KERNEL_VIEWER_URL", "")

    mine = start(reason, p)
    log.info("handoff %s pending: %s", mine.id, mine.reason)
    # In a thread: urllib blocks, and the event loop belongs to the MCP server.
    notified = await asyncio.to_thread(_announce, mine, send, viewer_url)

    began = time.monotonic()
    deadline = began + timeout
    while True:
        await asyncio.sleep(poll)
        cur = read(p)
        if cur.id != mine.id:
            return _result("superseded", mine, cur, notified, time.monotonic() - began)
        if not cur.pending:
            log.info("handoff %s cleared after %ds", mine.id, int(time.monotonic() - began))
            return _result("done", mine, cur, notified, time.monotonic() - began)
        if time.monotonic() >= deadline:
            break

    # Claim the timeout in the file, so the viewer stops asking for something
    # nothing is waiting on any more. done() may have landed in the moment
    # between the last poll and here; it wrote first, so it wins.
    cur, changed = _expire(mine, p)
    if not changed:
        if cur.id != mine.id:
            return _result("superseded", mine, cur, notified, time.monotonic() - began)
        if cur.resolved == "done":
            return _result("done", mine, cur, notified, time.monotonic() - began)
    log.info("handoff %s timed out after %ds", mine.id, int(timeout))
    return _result("timeout", mine, cur, notified, time.monotonic() - began)


def _expire(mine, p):
    cur = read(p)
    if not cur.pending or cur.id != mine.id:
        return cur, False
    try:
        return write(dataclasses.replace(cur, pending=False, resolved="timeout",
                                         resolved_at=_now()), p), True
    except OSError as exc:  # the wait is over either way; say so rather than raise
        log.warning("could not mark handoff %s timed out: %s", mine.id, exc)
        return cur, False


def _num(given, env, default):
    if given is not None:
        return float(given)
    try:
        return float(os.environ.get(env) or default)
    except ValueError:
        log.warning("%s is not a number; using %s", env, default)
        return float(default)


def _result(outcome, mine, cur, notified, waited):
    return Result(outcome, _message(outcome, mine, notified, waited), cur, notified)


# The messages are the tool's result, so they are written the way KernelError is:
# what happened, what is true now, and what to do next.

def _message(outcome, mine, notified, waited):
    took = f"{int(waited)}s" if waited < 90 else f"{int(waited) // 60}m"
    asked = f'You asked for: "{mine.reason}".'
    silent = ("" if notified else " The Telegram notification could not be sent, so the "
              "person may never have been told to look.")
    if outcome == "done":
        return (f"handoff complete — the person took the browser and pressed Done after "
                f"{took}. {asked} They may have signed in, navigated, or landed somewhere "
                "else entirely, so every ref you were holding is stale; the digest below is "
                "the page as it now is. Check that what you were blocked on is actually "
                "cleared before carrying on.")
    if outcome == "superseded":
        return (f"handoff dropped — a different handoff was recorded while this one was "
                f"waiting, so this call stopped waiting for it after {took}. {asked} Nobody "
                "answered this request. Look at the digest below and call handoff() again if "
                "you are still stuck.")
    return (f"handoff timed out — nobody pressed Done within {took}. {asked}{silent} They may "
            "not have seen the message, or may have finished the job and not pressed the "
            "button. Nothing was undone: read the digest below and decide whether you are "
            "still blocked. If you are, call handoff() again — it sends a fresh notification.")
