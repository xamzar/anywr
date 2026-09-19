"""The state file and the wait. Two processes, one file, and no network.

The property under nearly all of these: a read is total. The reader is a live
tool call and the writer is another process, so every way a file can be wrong --
absent, empty, truncated mid-write, not JSON, JSON but not a record -- has to
come back as "nothing pending" rather than as a traceback inside the model's
result.
"""
import asyncio
import json
import os
import re
from pathlib import Path

import pytest

from kernel import handoff

FAST = {"timeout": 2, "poll": 0.01}


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A state file of this test's own, and no Telegram credentials anywhere."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("KERNEL_HANDOFF_TIMEOUT", raising=False)
    monkeypatch.delenv("KERNEL_HANDOFF_POLL", raising=False)
    p = tmp_path / "state" / "handoff.json"
    monkeypatch.setenv("KERNEL_STATE_FILE", str(p))
    return p


class Sent(list):
    """A sender that records instead of sending. No test reaches the network."""

    def __call__(self, text):
        self.append(text)
        return True


# --- the file ---------------------------------------------------------------

def test_a_record_round_trips(state):
    h = handoff.start("sign in to AIMS")
    assert h.pending and h.id and h.reason == "sign in to AIMS"
    back = handoff.read()
    assert back == h
    assert json.loads(state.read_text())["pending"] is True


def test_write_creates_the_directory_it_was_pointed_at(state):
    assert not state.parent.exists()
    handoff.start("anything")
    assert state.exists()


def test_a_missing_file_is_no_handoff_pending(state):
    assert not state.exists()
    assert handoff.read() == handoff.NONE
    assert handoff.read().pending is False


@pytest.mark.parametrize("junk", [
    "",                                  # created but never written
    "   \n",
    '{"id": "a", "pending": tru',        # caught mid-write, without atomicity
    "not json at all",
    "[]",                                # JSON, but not a record
    '"pending"',
    "null",
    b"\xff\xfe\x00bad".decode("latin-1"),
])
def test_a_broken_file_reads_as_nothing_pending(state, junk):
    state.parent.mkdir(parents=True)
    state.write_text(junk, encoding="latin-1")
    assert handoff.read().pending is False


def test_a_record_with_no_id_is_not_pending(state):
    """An unnameable handoff is one Done could never clear, so honouring it
    would block every call for the whole timeout."""
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"pending": True, "reason": "hi"}))
    assert handoff.read().pending is False


def test_unexpected_field_types_do_not_reach_the_caller(state):
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"id": 7, "pending": 1, "reason": {"a": 1}, "since": []}))
    got = handoff.read()
    assert got.pending is False and got.id == "" and isinstance(got.reason, str)


def test_a_failed_write_leaves_the_old_record_and_no_debris(state, monkeypatch):
    """Whole record or none: os.replace is the point of the temp file."""
    first = handoff.start("the real one")
    monkeypatch.setattr(handoff.json, "dump", lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
    with pytest.raises(RuntimeError):
        handoff.start("never lands")
    assert handoff.read() == first
    assert [f for f in os.listdir(state.parent) if f != state.name] == []


# --- done -------------------------------------------------------------------

def test_done_clears_the_handoff(state):
    handoff.start("sign in")
    got, cleared = handoff.done()
    assert cleared and not got.pending and got.resolved == "done" and got.resolved_at


def test_done_twice_is_not_an_error(state):
    handoff.start("sign in")
    handoff.done()
    got, cleared = handoff.done()
    assert cleared is False          # the second press changed nothing
    assert got.resolved == "done"    # and un-did nothing either


def test_done_on_a_file_that_was_never_written_is_not_an_error(state):
    got, cleared = handoff.done()
    assert (got, cleared) == (handoff.NONE, False)


def test_done_for_a_previous_handoff_does_not_clear_the_current_one(state):
    old = handoff.start("the first one")
    handoff.done(old.id)
    new = handoff.start("the second one")
    got, cleared = handoff.done(old.id)   # a phone with the old page still open
    assert cleared is False and got.id == new.id and got.pending


# --- the reason -------------------------------------------------------------

def test_a_reason_is_flattened_and_capped():
    assert handoff.clean_reason("  two\nlines  ") == "two lines"
    assert len(handoff.clean_reason("x" * 900)) == handoff.MAX_REASON


def test_markup_in_a_reason_stops_being_markup_at_the_door():
    """The reason is model-authored text landing in a human's browser. The
    digest treats page text as hostile for the mirror-image reason; this is the
    first of the two places it is defused, before the file is even written."""
    out = handoff.clean_reason('<img src=x onerror="alert(1)">')
    assert "<" not in out and ">" not in out


def test_the_stored_reason_is_clean_even_if_the_file_was_written_by_hand(state):
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"id": "a", "pending": True, "reason": "<script>x</script>"}))
    assert "<" not in handoff.read().reason


# --- the wait ---------------------------------------------------------------

def test_wait_returns_once_the_other_side_clears_the_file(state):
    sent = Sent()

    async def go():
        async def control_server():
            for _ in range(500):
                await asyncio.sleep(0.01)
                if handoff.read().pending:
                    return handoff.done()           # exactly what post_done does
            raise AssertionError("the handoff was never written")

        pressed = asyncio.ensure_future(control_server())
        res = await handoff.wait("sign in to AIMS", send=sent, **FAST)
        await pressed
        return res

    res = asyncio.run(go())
    assert res.outcome == "done" and res.notified
    assert "handoff complete" in res.message and "sign in to AIMS" in res.message
    assert "stale" in res.message          # it tells the model its refs are dead
    assert handoff.read().resolved == "done"


def test_wait_times_out_and_says_so_readably(state):
    res = asyncio.run(handoff.wait("finish the captcha", send=Sent(), timeout=0.05, poll=0.01))
    assert res.outcome == "timeout"
    assert "timed out" in res.message and "finish the captcha" in res.message
    assert "handoff()" in res.message            # what to do next, in the message
    assert "Traceback" not in res.message


def test_a_timeout_stops_the_viewer_asking_for_something_nobody_awaits(state):
    asyncio.run(handoff.wait("hello", send=Sent(), timeout=0.05, poll=0.01))
    after = handoff.read()
    assert after.pending is False and after.resolved == "timeout"


def test_done_landing_in_the_last_instant_beats_the_timeout(state):
    """The one real race between the two writers: both end the handoff, and the
    one that wrote first is the truth."""
    async def go():
        async def press():
            for _ in range(500):
                await asyncio.sleep(0.005)
                if handoff.read().pending:
                    return handoff.done()
        pressed = asyncio.ensure_future(press())
        # Long enough that done() lands, short enough that the deadline is
        # already behind us when wait() next looks.
        res = await handoff.wait("hello", send=Sent(), timeout=0.02, poll=0.2)
        await pressed
        return res

    res = asyncio.run(go())
    assert res.outcome == "done" and handoff.read().resolved == "done"


def test_a_handoff_replaced_under_the_waiter_is_dropped_not_reported_done(state):
    async def go():
        async def replace():
            for _ in range(500):
                await asyncio.sleep(0.005)
                if handoff.read().pending:
                    return handoff.start("a different request")
        other = asyncio.ensure_future(replace())
        res = await handoff.wait("the original", send=Sent(), **FAST)
        await other
        return res

    res = asyncio.run(go())
    assert res.outcome == "superseded"
    assert "Nobody answered" in res.message and "the original" in res.message


def test_the_person_is_told_the_reason_and_where_to_look(state):
    sent = Sent()
    asyncio.run(handoff.wait("sign in to AIMS", send=sent, viewer_url="https://example/live/",
                             timeout=0.05, poll=0.01))
    assert len(sent) == 1
    assert "sign in to AIMS" in sent[0] and "https://example/live/" in sent[0]
    assert "Done" in sent[0]


# --- the notification is never the handoff ----------------------------------

def test_a_sender_that_raises_does_not_fail_the_handoff(state):
    def broken(text):
        raise ConnectionError("telegram is down")

    async def go():
        async def press():
            for _ in range(500):
                await asyncio.sleep(0.01)
                if handoff.read().pending:
                    return handoff.done()
        pressed = asyncio.ensure_future(press())
        res = await handoff.wait("sign in", send=broken, **FAST)
        await pressed
        return res

    res = asyncio.run(go())
    assert res.outcome == "done" and res.notified is False


def test_no_telegram_credentials_is_a_log_line_not_a_failure(state, caplog):
    res = asyncio.run(handoff.wait("sign in", timeout=0.05, poll=0.01))
    assert res.outcome == "timeout" and res.notified is False
    assert "TELEGRAM" in caplog.text
    # And the model is told, because "nobody pressed Done" reads very
    # differently when nobody was ever asked.
    assert "could not be sent" in res.message


def test_a_sender_that_reports_failure_is_not_claimed_as_sent(state):
    res = asyncio.run(handoff.wait("sign in", send=lambda text: False, timeout=0.05, poll=0.01))
    assert res.notified is False


# --- configuration ----------------------------------------------------------

def test_the_state_path_and_the_clock_come_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("KERNEL_STATE_FILE", raising=False)
    assert handoff.path() == handoff.DEFAULT_PATH
    monkeypatch.setenv("KERNEL_STATE_FILE", str(tmp_path / "x.json"))
    assert handoff.path() == str(tmp_path / "x.json")
    assert handoff.path("/override") == "/override"


def test_a_nonsense_timeout_in_the_environment_falls_back_rather_than_crashing(state, monkeypatch):
    monkeypatch.setenv("KERNEL_HANDOFF_TIMEOUT", "soon")
    monkeypatch.setenv("KERNEL_HANDOFF_POLL", "0.01")
    assert handoff._num(None, "KERNEL_HANDOFF_TIMEOUT", 600) == 600.0


def test_the_defaults_are_the_ones_that_were_argued_for():
    """Both numbers are load-bearing and reasoned about in the module docstring;
    a silent edit to either changes what the demo feels like."""
    assert handoff.DEFAULT_TIMEOUT_S == 600
    assert handoff.DEFAULT_POLL_S == 1.0
    assert handoff.DEFAULT_PATH == "/state/handoff.json"


# --- the guardrail ----------------------------------------------------------

def test_no_kernel_module_reaches_the_other_experiment():
    """/data, db.py and notify.py belong to the soak experiment. The kernel
    container cannot reach them and keeping that true is the point, so the
    import is refused here as well as by the filesystem."""
    bad = re.compile(r"^\s*(import|from)\s+(db|notify|src\.db|src\.notify)\b", re.M)
    for f in sorted(Path("src/kernel").glob("*.py")):
        src = f.read_text()
        assert not bad.search(src), f"{f} imports the soak experiment"
        assert "/data" not in src, f"{f} names /data"
