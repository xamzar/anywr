"""One case per state per site: (url, visible text, selectors present)."""
import pytest
import yaml

from classify import classify, selectors

T = {t["name"]: t for t in yaml.safe_load(open("config/targets.yaml"))}

CASES = [
    ("aims", "https://banweb.cityu.edu.hk/pls/PROD/twbkwbis.P_GenMenu?name=bmenu.P_MainMnu",
     "Personal Information Student Record", {'a[href*="P_Logout"]'}, "AUTHED"),
    ("aims", "https://auth.cityu.edu.hk/signin/verify/okta/push", "Get a push notification", set(), "CHALLENGED"),
    ("aims", "https://auth.cityu.edu.hk/oauth2/v1/authorize?x=1", "Sign In Username", set(), "LOGGED_OUT"),
    ("aims", "https://banweb.cityu.edu.hk/pls/PROD/twgkpswd_cityu.P_WWWLogin", "User Login", set(), "LOGGED_OUT"),
    ("canvas", "https://canvas.cityu.edu.hk/profile", "Profile", {"#global_nav_profile_link"}, "AUTHED"),
    ("canvas", "https://auth.cityu.edu.hk/app/sso/saml", "Sign In", set(), "LOGGED_OUT"),
    ("google", "https://myaccount.google.com/", "Home", set(), "AUTHED"),
    ("google", "https://www.google.com/account/about/?hl=en-US", "Google Account", set(), "LOGGED_OUT"),
    ("google", "https://accounts.google.com/v3/signin/challenge/pwd", "Verify it’s you", set(), "CHALLENGED"),
    ("google", "https://accounts.google.com/v3/signin/identifier", "Sign in", set(), "LOGGED_OUT"),
    ("github", "https://github.com/settings/profile", "Public profile",
     {'meta[name="user-login"]:not([content=""])'}, "AUTHED"),
    ("github", "https://github.com/sessions/verified-device", "Device verification", set(), "CHALLENGED"),
    ("github", "https://github.com/login?return_to=%2Fsettings%2Fprofile", "Sign in", set(), "LOGGED_OUT"),
    ("linkedin", "https://www.linkedin.com/feed/", "Start a post", set(), "AUTHED"),
    ("linkedin", "https://www.linkedin.com/checkpoint/challenge/x", "Let's do a quick security check", set(), "CHALLENGED"),
    ("linkedin", "https://www.linkedin.com/authwall?sessionRedirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed%2F",
     "Join now", set(), "LOGGED_OUT"),
    ("github", "https://github.com/settings/profile", "Whoa there! 503", set(), "ERROR"),
]


@pytest.mark.parametrize("site,url,text,present,want", CASES)
def test_states(site, url, text, present, want):
    assert set(present) <= set(selectors(T[site]))
    assert classify(T[site], url, text, present) == want


def test_every_target_covered():
    assert {c[0] for c in CASES} == set(T)
