"""The security of the front door, with no HTTP in it.

Issue a code, verify a code, sign a session, check a session, and the two rate
limits that stop a 6-digit secret from being a 6-digit secret in name only.
Everything here is a pure function or a small object you hand a clock, so the
properties that matter -- constant-time comparison, single use, expiry, burn on
guessing -- can be tested without a client, a socket or a browser. server.py is
the only file that knows what a request is.

Two clocks, on purpose. Challenges expire on time.monotonic(): both the issue
and the verify happen inside this one process, and monotonic cannot be moved by
ntp, a daylight-saving step or an operator with a shell, so a clock that jumps
backwards cannot resurrect an expired code. A cookie has to outlive the process
that wrote it, so it carries a wall-clock deadline instead -- and check() also
refuses a deadline further out than one max_age, which is the same defence
pointing forwards: a token minted while the clock was wrong ahead does not get
to live for a month.
"""
import base64
import dataclasses
import hashlib
import hmac
import logging
import os
import re
import secrets
import string
import time

log = logging.getLogger("auth.core")

CODE_DIGITS = 6
# Five minutes. The floor is the human: a message has to arrive, a phone has to
# be picked up, an app has to be switched. The ceiling is arithmetic -- a live
# code is a live guessing target, and the window is the only thing bounding how
# many codes can be in flight at once. Five minutes is comfortably past the p99
# of a Telegram message or an SMTP hop and short enough that a code read over a
# shoulder, or left in a notification shade, is dead before it is useful.
CODE_TTL_S = 300
# Five wrong guesses and the code is destroyed, not just refused. Nobody fat-
# fingers six digits five times; an attacker needs 10**6/2 tries. With the send
# limits below this caps an address at 15 guesses per 15 minutes and 50 per day
# -- about 1 in 20,000 per year of continuous attack, and every one of those
# guesses costs the attacker a message the real owner receives and can act on.
MAX_ATTEMPTS = 5
# (codes, seconds). Two windows because one cannot do both jobs: the short one
# keeps a burst from becoming a mailbox flood and caps the guesses available in
# any one window, the long one stops a patient attacker from simply waiting out
# the short one for a year. Ten codes a day is more than any real person needs
# and 480 fewer guesses a day than the short rule alone would allow.
SEND_RULES = ((3, 900), (10, 86400))
SESSION_MAX_AGE_S = 24 * 60 * 60
# A signing key shorter than a sha256 block's worth of entropy is not a key, it
# is a passphrase somebody will type twice. 32 bytes is also exactly what
# `openssl rand -hex 16` gives, so the error message can name a command.
MIN_SECRET_LEN = 32
TOKEN_VERSION = "v1"
MAX_TOKEN_LEN = 512
MAX_EMAIL_LEN = 254
# Wall clocks disagree. A minute of tolerance costs a minute of session life at
# the end and saves a user whose phone is a little ahead of the server.
CLOCK_SKEW_S = 60

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# Deliberately not RFC 5322. This decides what may be used as a dictionary key
# and put in a log line, not what may be posted to an MTA.
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[a-z]{2,63}$")

# Not a password list -- a tripwire for the four or five strings that actually
# get committed when someone needs the service up before lunch.
WEAK_SECRETS = ("changeme", "change-me", "secret", "password", "please-change",
                "dev", "test", "development", "insecure", "todo",
                "your-secret-here", "replace-me", "xxxxxxxx")


class AuthConfigError(Exception):
    """The service must not start. Raised at wiring time, never per request."""


# --- the secret -------------------------------------------------------------

def load_secret(env=None):
    """AUTH_SECRET as bytes, or refuse.

    A default secret is how this ships insecure: the service runs, the cookies
    verify, the tests pass, and every deployment in the world shares a key that
    is in the repository. So there is no default, and nothing here falls back.
    """
    env = os.environ if env is None else env
    raw = (env.get("AUTH_SECRET") or "").strip()
    if not raw:
        raise AuthConfigError(
            "AUTH_SECRET is not set. Sessions are signed with it and there is no default. "
            "Generate one: python -c \"import secrets; print(secrets.token_hex(32))\"")
    if len(raw) < MIN_SECRET_LEN:
        raise AuthConfigError(
            f"AUTH_SECRET is {len(raw)} characters; at least {MIN_SECRET_LEN} are required.")
    low = raw.lower()
    # Length alone passes "changeme-changeme-changeme-change" and "aaaa…".
    if any(w in low for w in WEAK_SECRETS) or len(set(raw)) < 8:
        raise AuthConfigError(
            "AUTH_SECRET looks like a placeholder rather than a random value. "
            "Generate one: python -c \"import secrets; print(secrets.token_hex(32))\"")
    return raw.encode()


# --- shapes -----------------------------------------------------------------

def normal_email(value):
    """Lowercased, stripped, or "" if it is not an address we will accept.

    One normal form, used for the users.yaml index, the rate-limit key and the
    challenge key alike -- otherwise Asan@x.com and asan@x.com are one account
    for logging in and two for rate limiting, and the limit is the one that
    loses.
    """
    s = value.strip().lower() if isinstance(value, str) else ""
    return s if len(s) <= MAX_EMAIL_LEN and EMAIL_RE.match(s) else ""


def valid_username(value):
    return isinstance(value, str) and bool(USERNAME_RE.match(value))


def new_code(digits=CODE_DIGITS):
    """A fresh code from the OS CSPRNG. secrets.choice, so no modulo bias and
    no leading-zero trimming -- '000123' is a perfectly good code and an int
    would have thrown two digits of the space away."""
    return "".join(secrets.choice(string.digits) for _ in range(digits))


def mask(email):
    """An address fit for a log line: a***@example.com."""
    name, _, host = (email or "").partition("@")
    return f"{name[:1]}***@{host}" if host else "***"


# --- one-time codes ---------------------------------------------------------

@dataclasses.dataclass
class Challenge:
    """One outstanding code. The code itself is not in here and never was."""
    username: str
    salt: str
    digest: str
    expires: float          # monotonic
    attempts: int = 0


@dataclasses.dataclass(frozen=True)
class Verdict:
    """ok plus a reason for the *server log*. The client is told one sentence
    whatever happened: "expired" and "no such challenge" cannot be
    distinguishable to a caller, or the error message becomes the user
    enumeration oracle the identical /start response was written to close."""
    ok: bool
    username: str = ""
    reason: str = ""


class Codes:
    """Outstanding challenges, keyed by normalised address.

    In memory, not on disk. A code lives five minutes and the service is one
    process; writing it anywhere would be a second copy to protect for no gain,
    and a restart invalidating every code in flight is the safe direction.

    Hashed with HMAC-SHA256 under AUTH_SECRET, salted, and bound to the address
    it was issued for. Not because sha256 of six digits is hard to reverse -- it
    takes a millisecond -- but because a heap dump, a core file or a future
    debug print then yields nothing without the key, and a code issued for one
    address cannot be spent against another. The real defence against guessing
    is MAX_ATTEMPTS; no amount of key stretching would help a 10**6 space.

    No lock, and it needs none where it is used: verify() reads, decides and
    deletes with no await anywhere in between, so on one event loop two
    simultaneous requests cannot both find the same live challenge. Put this
    behind a thread pool and single use becomes a race -- that is the condition
    it relies on, written down.
    """

    def __init__(self, secret, *, ttl=CODE_TTL_S, max_attempts=MAX_ATTEMPTS,
                 clock=time.monotonic):
        self._secret = secret
        self._ttl = float(ttl)
        self._max_attempts = int(max_attempts)
        self._clock = clock
        self._live = {}

    def _digest(self, salt, email, code):
        msg = f"{salt}:{email}:{code}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def issue(self, username, email):
        """Replace any outstanding challenge for `email` and return the code.

        Replace, not reuse: only the hash is kept, so the same code cannot be
        sent twice, and two live codes for one address would double the guessing
        surface for the convenience of whichever message arrives first. Asking
        twice is therefore well defined -- the newest code is the only one that
        works, and the older one is dead the instant the newer is issued.
        """
        code = new_code()
        salt = secrets.token_hex(8)
        self._live[email] = Challenge(username=username, salt=salt,
                                      digest=self._digest(salt, email, code),
                                      expires=self._clock() + self._ttl)
        return code

    def verify(self, email, code):
        """Spend the code. Single use, constant time, burns on guessing."""
        ch = self._live.get(email)
        if ch is None:
            return Verdict(False, reason="no-challenge")
        if self._clock() >= ch.expires:
            del self._live[email]           # a correct expired code is still no
            return Verdict(False, reason="expired")
        given = code.strip() if isinstance(code, str) else ""
        # compare_digest on the two hex digests, so the time taken says nothing
        # about how many leading digits were right. Comparing the codes directly
        # would leak the same way and is exactly the == that this exists to
        # replace.
        if not hmac.compare_digest(self._digest(ch.salt, email, given), ch.digest):
            ch.attempts += 1
            if ch.attempts >= self._max_attempts:
                del self._live[email]
                return Verdict(False, reason="burned")
            return Verdict(False, reason="wrong")
        # Gone before the caller is told it was right: single use is a deletion,
        # not a flag, so there is no second path that could forget to check it.
        del self._live[email]
        return Verdict(True, username=ch.username, reason="ok")

    def drop(self, email):
        self._live.pop(email, None)

    def outstanding(self):
        return len(self._live)


# --- rate limiting ----------------------------------------------------------

class Limiter:
    """Sliding-window counter over several rules at once.

    Monotonic, like the challenges: a limit a clock change can lift is not a
    limit. Bounded in size because the key is chosen by whoever is posting --
    when the table is full it prunes, and if it is still full it says no. Fail
    closed: under a flood of made-up addresses the real user waits, which is
    worse than nothing and much better than an allocator driven by strangers.
    """

    def __init__(self, rules=SEND_RULES, *, clock=time.monotonic, max_keys=4096):
        self._rules = tuple(rules)
        self._window = max(w for _, w in self._rules)
        self._clock = clock
        self._max_keys = max_keys
        self._hits = {}

    def _prune(self, now):
        cutoff = now - self._window
        for key in [k for k, ts in self._hits.items() if not ts or ts[-1] <= cutoff]:
            del self._hits[key]

    def allow(self, key):
        """True if `key` may spend one now, recording it if so."""
        now = self._clock()
        ts = [t for t in self._hits.get(key, ()) if t > now - self._window]
        for limit, window in self._rules:
            if sum(1 for t in ts if t > now - window) >= limit:
                self._hits[key] = ts
                return False
        if key not in self._hits and len(self._hits) >= self._max_keys:
            self._prune(now)
            if len(self._hits) >= self._max_keys:
                log.warning("rate-limit table full — refusing new addresses")
                return False
        ts.append(now)
        self._hits[key] = ts
        return True


# --- session cookies --------------------------------------------------------
# v1.<username>.<expiry>.<mac>. Readable on purpose: there is nothing secret in
# a session token here, only something unforgeable, and a field an operator can
# read in a browser's storage inspector is a field they can reason about. The
# username charset forbids the separator, so no name can move the boundaries.

def _mac(secret, payload):
    return base64.urlsafe_b64encode(
        hmac.new(secret, payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def sign(username, secret, *, max_age=SESSION_MAX_AGE_S, now=None):
    if not valid_username(username):
        raise ValueError("username is not in the permitted charset")
    now = time.time() if now is None else now
    payload = f"{TOKEN_VERSION}.{username}.{int(now + max_age)}"
    return f"{payload}.{_mac(secret, payload)}"


def check(token, secret, *, max_age=SESSION_MAX_AGE_S, now=None):
    """The username in a valid token, or "". Never raises, whatever arrives.

    Signature first, then the clock: a token nobody could have minted is not
    worth telling apart from one that is merely old, and doing the cheap checks
    first would let the response time sort forged tokens into shapes.
    """
    if not isinstance(token, str) or not (0 < len(token) <= MAX_TOKEN_LEN):
        return ""
    version, _, rest = token.partition(".")
    username, _, rest = rest.partition(".")
    expiry, _, mac = rest.partition(".")
    if version != TOKEN_VERSION or not mac or not valid_username(username):
        return ""
    if not expiry.isdigit():
        return ""
    if not hmac.compare_digest(_mac(secret, f"{version}.{username}.{expiry}"), mac):
        return ""
    now = time.time() if now is None else now
    expiry = int(expiry)
    if expiry <= now:
        return ""
    # Forwards clock defence: a cookie cannot outlive one max_age however it
    # came to carry a distant deadline. Without this, an hour of a wrong system
    # clock mints sessions that last until someone notices.
    if expiry > now + max_age + CLOCK_SKEW_S:
        log.warning("session for %s carries an expiry beyond one max age — rejected", username)
        return ""
    return username
