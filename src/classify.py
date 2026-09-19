"""Matcher engine: (target config, observed page) -> state. Pure, no browser."""

STATES = ("AUTHED", "CHALLENGED", "LOGGED_OUT", "ERROR")


def _list(sig):
    return [] if sig is None else sig if isinstance(sig, list) else [sig]


def selectors(target):
    """Every CSS selector the target's matchers ask about, so the probe can check them."""
    return [m["value"] for key in ("authed_signal", "challenge_signal", "loggedout_signal")
            for m in _list(target.get(key)) if m["kind"] == "selector"]


def _hit(m, url, text, present):
    v = m["value"]
    if m["kind"] == "selector":
        return v in present
    if m["kind"] == "url_contains":
        return v in url
    if m["kind"] == "url_prefix":
        return url.startswith(v)
    if m["kind"] == "text":
        return v.lower() in text.lower()
    raise ValueError(f"unknown matcher kind {m['kind']!r}")


def classify(target, url, text, present=()):
    """present = the subset of selectors(target) found on the page."""
    for state, key in (("AUTHED", "authed_signal"), ("CHALLENGED", "challenge_signal"),
                       ("LOGGED_OUT", "loggedout_signal")):
        if any(_hit(m, url, text, present) for m in _list(target.get(key))):
            return state
    return "ERROR"   # unclassified: a human reads the screenshot; never counted as a death
