"""The security properties, with no HTTP anywhere near them.

Every test here drives its own clock, so "expired" and "the window has passed"
are decisions this file makes rather than things it waits for. Nothing in this
file sleeps, opens a socket or reads the real config.
"""
import inspect
import re

import pytest

from auth import core
from auth.core import AuthConfigError

GOOD = "b3f1a9c2d4e6f80123456789abcdef0123456789abcdef0123456789abcdef01"
SECRET = GOOD.encode()
OTHER = ("9" + GOOD[1:]).encode()
EMAIL = "someone@example.com"


class Clock:
    """A clock the test moves. Stands in for time.monotonic, so nothing here
    depends on how long the suite takes to run."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def codes(clock):
    return core.Codes(SECRET, clock=clock)


# --- the secret -------------------------------------------------------------

def test_a_missing_secret_is_refused():
    with pytest.raises(AuthConfigError) as exc:
        core.load_secret({})
    assert "AUTH_SECRET" in str(exc.value)


def test_an_empty_secret_is_refused():
    with pytest.raises(AuthConfigError):
        core.load_secret({"AUTH_SECRET": "   "})


def test_a_short_secret_is_refused():
    with pytest.raises(AuthConfigError):
        core.load_secret({"AUTH_SECRET": "a1b2c3d4e5f6"})


@pytest.mark.parametrize("value", [
    "changeme-changeme-changeme-changeme",     # long enough, still a placeholder
    "please-change-this-before-deploying-it",
    "a" * 64,                                  # long enough, one bit of entropy
    "abababababababababababababababababab",
])
def test_a_long_but_trivial_secret_is_refused(value):
    """Length alone is the check everyone writes, and it passes 'changeme'
    repeated four times."""
    with pytest.raises(AuthConfigError):
        core.load_secret({"AUTH_SECRET": value})


def test_a_real_secret_is_accepted():
    assert core.load_secret({"AUTH_SECRET": GOOD}) == SECRET


def test_the_error_says_how_to_make_one():
    with pytest.raises(AuthConfigError) as exc:
        core.load_secret({})
    assert "token_hex" in str(exc.value)


# --- shapes -----------------------------------------------------------------

def test_a_code_is_six_digits_and_may_start_with_zero():
    seen = {core.new_code() for _ in range(500)}
    assert all(re.fullmatch(r"[0-9]{6}", c) for c in seen)
    # An int would have eaten the leading zeros and a tenth of the space with
    # them; 500 draws from 10**6 should still be 500 distinct codes.
    assert len(seen) > 490


@pytest.mark.parametrize("given,want", [
    ("  Asan@Example.COM ", "asan@example.com"),
    ("a@b.co", "a@b.co"),
    ("", ""),
    ("no-at-sign", ""),
    ("two@@example.com", ""),
    ("space in@example.com", ""),
    ("a@b", ""),
    (None, ""),
    (12345, ""),
    ("a" * 300 + "@example.com", ""),
])
def test_email_normalisation(given, want):
    assert core.normal_email(given) == want


@pytest.mark.parametrize("name,ok", [
    ("asanbl4", True), ("a", True), ("a_b-c9", True), ("4asan", True),
    ("Asan", False),            # would be a second spelling of one account
    ("a b", False), ("a/b", False), ("a.b", False),
    ("a" * 41, False), ("", False), (None, False),
    ("x\r\nX-Auth-User: root", False),   # it becomes a header value
])
def test_username_charset(name, ok):
    assert core.valid_username(name) is ok


def test_mask_keeps_the_domain_and_loses_the_person():
    assert core.mask("someone@example.com") == "s***@example.com"
    assert "omeone" not in core.mask("someone@example.com")


# --- one-time codes ---------------------------------------------------------

def test_the_happy_path(codes):
    code = codes.issue("asanbl4", EMAIL)
    verdict = codes.verify(EMAIL, code)
    assert verdict.ok and verdict.username == "asanbl4"


def test_a_wrong_code_is_refused(codes):
    code = codes.issue("asanbl4", EMAIL)
    wrong = "000000" if code != "000000" else "111111"
    assert codes.verify(EMAIL, wrong).ok is False


def test_a_code_cannot_be_used_twice(codes):
    code = codes.issue("asanbl4", EMAIL)
    assert codes.verify(EMAIL, code).ok is True
    replay = codes.verify(EMAIL, code)
    assert replay.ok is False and replay.reason == "no-challenge"


def test_an_expired_code_is_refused_even_when_correct(codes, clock):
    code = codes.issue("asanbl4", EMAIL)
    clock.tick(core.CODE_TTL_S)
    assert codes.verify(EMAIL, code).reason == "expired"


def test_a_code_is_still_good_a_second_before_it_expires(codes, clock):
    code = codes.issue("asanbl4", EMAIL)
    clock.tick(core.CODE_TTL_S - 1)
    assert codes.verify(EMAIL, code).ok is True


def test_an_expired_challenge_is_dropped_rather_than_left_to_rot(codes, clock):
    codes.issue("asanbl4", EMAIL)
    clock.tick(core.CODE_TTL_S)
    codes.verify(EMAIL, "000000")
    assert codes.outstanding() == 0


def test_the_guess_limit_burns_the_code(codes):
    code = codes.issue("asanbl4", EMAIL)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(core.MAX_ATTEMPTS - 1):
        assert codes.verify(EMAIL, wrong).reason == "wrong"
    assert codes.verify(EMAIL, wrong).reason == "burned"
    # The point of burning: the right code is now worthless too.
    assert codes.verify(EMAIL, code).reason == "no-challenge"
    assert codes.outstanding() == 0


def test_a_correct_guess_before_the_limit_still_works(codes):
    code = codes.issue("asanbl4", EMAIL)
    wrong = "000000" if code != "000000" else "111111"
    codes.verify(EMAIL, wrong)
    codes.verify(EMAIL, wrong)
    assert codes.verify(EMAIL, code).ok is True


def test_asking_twice_leaves_exactly_one_live_code(codes):
    """A second request while the first is live: the newest code is the only
    one that works, and there is never more than one target per address."""
    first = codes.issue("asanbl4", EMAIL)
    second = codes.issue("asanbl4", EMAIL)
    assert codes.outstanding() == 1
    assert codes.verify(EMAIL, first).ok is False
    assert codes.verify(EMAIL, second).ok is True


def test_a_reissue_does_not_hand_back_free_guesses(codes):
    """Named for the bug it prevents: if a new code reset nothing but the
    counter, an attacker would re-request instead of stopping at five. The
    send limiter is what caps this, not the attempt counter."""
    codes.issue("asanbl4", EMAIL)
    for _ in range(core.MAX_ATTEMPTS):
        codes.verify(EMAIL, "000000")
    assert codes.outstanding() == 0


def test_a_code_is_bound_to_the_address_it_was_issued_for(codes):
    code = codes.issue("asanbl4", EMAIL)
    assert codes.verify("someone.else@example.com", code).reason == "no-challenge"


def test_two_addresses_do_not_share_a_challenge(codes):
    a = codes.issue("asanbl4", EMAIL)
    b = codes.issue("other", "other@example.com")
    assert codes.verify(EMAIL, b).ok is False
    assert codes.verify(EMAIL, a).ok is True


def test_the_code_is_never_stored_in_the_clear(codes):
    code = codes.issue("asanbl4", EMAIL)
    stored = repr(codes.__dict__)
    assert code not in stored
    assert core.CODE_DIGITS == 6 and len(code) == 6


def test_the_stored_digest_needs_the_secret(clock):
    """Same code, same address, different key: the digests must not match, or
    a leaked store would be brute-forceable in a millisecond without it."""
    a, b = core.Codes(SECRET, clock=clock), core.Codes(OTHER, clock=clock)
    code = a.issue("asanbl4", EMAIL)
    b._live[EMAIL] = core.Challenge(username="asanbl4", salt=a._live[EMAIL].salt,
                                    digest=a._live[EMAIL].digest,
                                    expires=a._live[EMAIL].expires)
    assert b.verify(EMAIL, code).ok is False


def test_two_identical_codes_hash_differently(clock):
    """Salted per challenge, so the store never shows that two addresses were
    sent the same six digits."""
    codes = core.Codes(SECRET, clock=clock)
    codes.issue("a", "a@example.com")
    codes.issue("b", "b@example.com")
    a, b = codes._live["a@example.com"], codes._live["b@example.com"]
    a.digest = codes._digest(a.salt, "a@example.com", "123456")
    b.digest = codes._digest(b.salt, "b@example.com", "123456")
    assert a.digest != b.digest


@pytest.mark.parametrize("junk", [None, 123456, "", "  ", "1234567", "12345", "abcdef", b"123456"])
def test_junk_where_a_code_should_be_is_refused_not_raised(codes, junk):
    codes.issue("asanbl4", EMAIL)
    assert codes.verify(EMAIL, junk).ok is False


def test_the_comparison_is_constant_time():
    """Not observable from outside, so it is asserted where it lives. A plain
    == on the digest, or worse on the code, is the whole failure."""
    src = inspect.getsource(core.Codes.verify)
    assert "hmac.compare_digest" in src
    assert "== ch.digest" not in src and "code ==" not in src


# --- rate limiting ----------------------------------------------------------

def test_the_send_limit_stops_a_burst(clock):
    lim = core.Limiter(((3, 900),), clock=clock)
    assert [lim.allow(EMAIL) for _ in range(5)] == [True, True, True, False, False]


def test_the_window_slides(clock):
    lim = core.Limiter(((3, 900),), clock=clock)
    for _ in range(3):
        lim.allow(EMAIL)
    clock.tick(899)
    assert lim.allow(EMAIL) is False
    clock.tick(2)                       # the first of the three has aged out
    assert lim.allow(EMAIL) is True


def test_the_daily_rule_outlives_the_short_one(clock):
    """The rule that stops a patient attacker: 3 per 15 minutes alone would
    allow 288 codes a day, which is 1,440 guesses a day."""
    lim = core.Limiter(core.SEND_RULES, clock=clock)
    sent = 0
    for _ in range(12):                 # twelve windows, far past the daily cap
        for _ in range(3):
            sent += lim.allow(EMAIL)
        clock.tick(901)
    assert sent == 10


def test_a_limit_is_per_address(clock):
    lim = core.Limiter(((1, 900),), clock=clock)
    assert lim.allow("a@example.com") is True
    assert lim.allow("b@example.com") is True
    assert lim.allow("a@example.com") is False


def test_a_clock_that_jumps_backwards_does_not_lift_the_limit(clock):
    """Monotonic, so this cannot happen in production either — the test is here
    to fail loudly if someone swaps in time.time() for readability."""
    lim = core.Limiter(((3, 900),), clock=clock)
    for _ in range(3):
        lim.allow(EMAIL)
    clock.tick(-100000)
    assert lim.allow(EMAIL) is False


def test_the_table_is_bounded(clock):
    """The key is chosen by whoever is posting. Full means no, not more RAM."""
    lim = core.Limiter(((1, 900),), clock=clock, max_keys=8)
    for i in range(8):
        assert lim.allow(f"u{i}@example.com") is True
    assert lim.allow("late@example.com") is False
    clock.tick(901)                     # everything ages out, and it recovers
    assert lim.allow("late@example.com") is True


# --- session cookies --------------------------------------------------------

def test_a_signed_token_verifies():
    assert core.check(core.sign("asanbl4", SECRET), SECRET) == "asanbl4"


def test_a_token_carries_the_username_and_an_expiry():
    token = core.sign("asanbl4", SECRET, now=1_000_000)
    assert token.startswith("v1.asanbl4.")
    assert token.split(".")[2] == str(1_000_000 + core.SESSION_MAX_AGE_S)


def test_a_token_expires_at_twentyfour_hours():
    now = 1_000_000
    token = core.sign("asanbl4", SECRET, now=now)
    assert core.check(token, SECRET, now=now + core.SESSION_MAX_AGE_S - 1) == "asanbl4"
    assert core.check(token, SECRET, now=now + core.SESSION_MAX_AGE_S) == ""
    assert core.SESSION_MAX_AGE_S == 24 * 60 * 60


def test_a_token_signed_with_another_secret_is_refused():
    assert core.check(core.sign("asanbl4", OTHER), SECRET) == ""


def test_a_tampered_username_is_refused():
    token = core.sign("asanbl4", SECRET)
    assert core.check(token.replace("asanbl4", "root"), SECRET) == ""


def test_a_tampered_expiry_is_refused():
    now = 1_000_000
    v, user, exp, mac = core.sign("asanbl4", SECRET, now=now).split(".")
    forged = f"{v}.{user}.{int(exp) + 86400}.{mac}"
    assert core.check(forged, SECRET, now=now) == ""


def test_a_tampered_signature_is_refused():
    token = core.sign("asanbl4", SECRET)
    body, _, mac = token.rpartition(".")
    assert core.check(f"{body}.{'A' if mac[0] != 'A' else 'B'}{mac[1:]}", SECRET) == ""


def test_an_unsigned_token_is_refused():
    assert core.check("v1.asanbl4.99999999999.", SECRET) == ""
    assert core.check("v1.asanbl4.99999999999", SECRET) == ""


def test_a_token_from_a_future_clock_cannot_outlive_one_max_age():
    """A wrong system clock for an hour should cost an hour of odd sessions,
    not a month of valid ones."""
    token = core.sign("asanbl4", SECRET, now=2_000_000)
    assert core.check(token, SECRET, now=1_000_000) == ""


def test_a_slightly_fast_phone_is_tolerated():
    token = core.sign("asanbl4", SECRET, now=1_000_030)
    assert core.check(token, SECRET, now=1_000_000) == "asanbl4"


@pytest.mark.parametrize("junk", [
    "", None, 12345, b"v1.a.1.b", "garbage", "v1.asanbl4", "v2.asanbl4.99999999999.x",
    "v1..99999999999.x", "v1.asanbl4.notanumber.x", "v1.asanbl4.-1.x",
    "v1.ASANBL4.99999999999.x", "." * 10, "v1.asanbl4.99999999999.x" + "A" * 600,
])
def test_junk_where_a_token_should_be_is_refused_not_raised(junk):
    assert core.check(junk, SECRET) == ""


def test_the_token_comparison_is_constant_time():
    src = inspect.getsource(core.check)
    assert "hmac.compare_digest" in src


def test_signing_refuses_a_username_that_could_forge_a_field():
    with pytest.raises(ValueError):
        core.sign("asan.bl4", SECRET)
    with pytest.raises(ValueError):
        core.sign("asan\r\nX-Auth-User: root", SECRET)
