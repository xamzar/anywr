"""Is this page a signed-in page? -- the one question a digest cannot answer.

An idled-out Banner session renders its whole menu: same refs, same links, same
everything, with "Your AIMS session has been timeout" where the data should be.
A model reading only the digest keeps clicking and reports an empty result as
fact. status() exists to make that state sayable.

Pure: url, title, text and the digest's own elements in, one line out. The
tables below are the only thing that has to be right, and none of them names a
site -- a site is recognised by what it says and what it offers, not by who it
is. src/classify.py solves the neighbouring problem for the soak probe with
per-target YAML, which is the right shape there and the wrong one here: the
kernel meets pages it has no config for, so it has to guess honestly or say
UNKNOWN.
"""
import re
from urllib.parse import urlsplit

from kernel import digest

STATES = ("AUTHED", "LOGGED_OUT", "UNKNOWN")

# --- the tables -------------------------------------------------------------
# Data, walked the same way for every page. A row that is too loose is the only
# way this gets a page wrong, so every row is a whole statement or a whole
# name -- never a bare word. "login" appears in navigation, in help text and in
# prose on pages that are perfectly signed in, and reading that as LOGGED_OUT
# would make this tool worse than not having it.

# The site saying, in its own words, that the session is over. Decisive: it
# outranks every other signal, because the page that makes this tool necessary
# keeps its sign-out control and its entire menu while saying exactly this.
SESSION_ENDED = (
    "session has been timeout",      # CityU AIMS/Banner, verified live 2026-09-19
    "session has timed out",
    "session has expired",
    "session timed out",
    "session expired",
    "session is no longer valid",
    "session has ended",
    "you have been logged out",
    "you have been signed out",
    "you are no longer signed in",
    "please log in again",
    "please sign in again",
    "log in again to continue",
)
# Same idea against the title only. A title is short and is the site's label for
# the whole page, so two words there carry weight two words in the body do not:
# a body mentioning "session timeout" may be documentation, a page *titled* it
# is the timeout page.
SESSION_ENDED_TITLE = ("session timeout", "session expired", "session ended",
                       "logged out", "signed out", "timed out")

# Known sign-in endpoints. Hosts match by suffix, so a tenant's own subdomain of
# one of these counts.
SIGNIN_HOSTS = ("accounts.google.com", "login.microsoftonline.com", "login.live.com",
                "okta.com", "auth0.com", "onelogin.com", "pingidentity.com",
                "duosecurity.com", "id.atlassian.com")
# The generic naming convention for an SSO front door, which is what catches a
# site nobody has listed -- auth.cityu.edu.hk, login.example.edu, sso.corp.
SIGNIN_HOST_PREFIXES = ("auth.", "login.", "signin.", "sso.", "idp.", "accounts.", "adfs.")
# Matched as a whole path segment, never as a substring: /articles/login-help is
# a help article about signing in, not a sign-in page.
SIGNIN_PATHS = frozenset({"login", "signin", "sign_in", "sign-in", "logon", "sso",
                          "saml2", "oauth2", "authorize", "adfs", "session_new"})

# A control that ends a session. Two ways in, because half the portals that
# matter do not call it "Sign Out": Banner's is labelled "Exit" and is only
# recognisable by where it points.
SIGNOUT_NAME = re.compile(r"\b(sign|log)\s*(out|off)\b", re.I)
SIGNOUT_HREF = ("logout", "signout", "sign_out", "sign-out", "logoff")

# Whether a *visible* password field is on the page. Hidden ones are ignored:
# a collapsed sign-in panel in a header sits in the DOM of plenty of pages that
# are signed in, and it is the visible one that means "sign in here".
JS_PASSWORD_FIELD = """() =>
  [...document.querySelectorAll('input[type=password]')]
    .some(el => el.getClientRects().length)"""


def _low(s):
    return str(s or "").lower()


def _first(phrases, haystack):
    return next((p for p in phrases if p in haystack), "")


def signout_control(elements):
    """The name of the first control that ends the session, or ""."""
    for el in elements:
        name = digest.clean(getattr(el, "name", ""))
        href = _low(getattr(el, "href", ""))
        if SIGNOUT_NAME.search(name) or any(m in href for m in SIGNOUT_HREF):
            return name or "unnamed"
    return ""


def signin_url(url):
    """The part of `url` that makes it a sign-in endpoint, or ""."""
    p = urlsplit(str(url or ""))
    host = (p.hostname or "").lower()
    if host.startswith(SIGNIN_HOST_PREFIXES) or any(
            host == h or host.endswith("." + h) for h in SIGNIN_HOSTS):
        return host
    return next((f"/{seg}" for seg in _low(p.path).split("/") if seg in SIGNIN_PATHS), "")


def status(url, title, text, elements=(), *, password_field=False):
    """AUTHED / LOGGED_OUT / UNKNOWN, a dash, and a reason to act on.

    `elements` are digest Elements -- the same list view() numbered, so a
    sign-out control counts only if it is something the model could click.

    UNKNOWN is the answer whenever the page carries no evidence either way, and
    that is most pages: a table of grades says nothing about the session. It is
    also the answer when the evidence conflicts. Answering AUTHED on a hunch
    would defeat the point, since the state worth catching is precisely the one
    that looks fine.
    """
    # Whole statements first wherever they are, loose title markers last: all
    # three mean LOGGED_OUT, but the reason quotes whichever matched, and
    # "session has been timeout" tells the model more than "session timeout".
    ended = (_first(SESSION_ENDED, _low(title)) or _first(SESSION_ENDED, _low(text))
             or _first(SESSION_ENDED_TITLE, _low(title)))
    out = signout_control(elements)
    signin = signin_url(url)

    if ended:
        extra = (" The menu it still shows is part of the signed-out page." if out else "")
        return (f'LOGGED_OUT — the page itself says "{ended}".{extra} Nothing it lists is '
                "your data, so do not report what you read here as a result; the session "
                "has to be signed in again before this task can continue.")
    if password_field or signin:
        why = ("a sign-in form (a password field) is on the page" if password_field
               else f"the url is a known sign-in endpoint ({signin})")
        if out:
            return (f"UNKNOWN — {why}, but so is a sign-out control ({out!r}), which fits a "
                    "signed-in page changing a password as well as it fits a sign-in page. "
                    "Do not treat anything this page shows as confirmed.")
        return (f"LOGGED_OUT — {why} and there is no sign-out control. You are being asked "
                "to sign in; the data behind it is not readable until someone does.")
    if out:
        return (f"AUTHED — a sign-out control ({out!r}) is on this page, which a page asking "
                "you to sign in does not offer. This is as far as the page can be trusted: "
                "it is not proof the next request will succeed.")
    return ("UNKNOWN — no sign-out control, no sign-in form, no sign-in url and no session "
            "notice, so this page cannot be told apart from a signed-out one. An empty or "
            "missing result here is not evidence about the account; get to a page that shows "
            "a sign-out control and ask again.")
