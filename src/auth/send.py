"""Getting the code to the person. One function per channel.

Adding a real email provider is one function of the shape send(email, code) and
one line in CHANNELS; nothing above this file knows how a code travels. The
server takes the sender as an argument, so a test passes its own and nothing
leaves the process.

The default writes the code to the log. That is a delivery channel, not
logging: with nothing configured the whole flow still works end to end, which is
the point, but it means anyone who can read the container log can log in as
you. It says so, loudly, at startup, and it is the only place in this service
where a code is ever written down.
"""
import json
import logging
import os
import urllib.request

from auth.core import AuthConfigError, CODE_TTL_S, mask

log = logging.getLogger("auth.send")
# Its own logger so the delivery line can be filtered, forwarded or silenced
# without touching the service's own logging -- and so "does the service log
# codes?" has a one-word answer for everything under auth.* except this.
delivery = logging.getLogger("auth.delivery")

TELEGRAM_TIMEOUT_S = 10
MINUTES = max(1, CODE_TTL_S // 60)


def message(code):
    """What the person reads. No links: a sign-in message that trains you to
    tap a link in a sign-in message is a phishing lesson with our name on it."""
    return (f"Anywhere Live sign-in code: {code}\n\n"
            f"It expires in {MINUTES} minutes and works once. "
            "If you did not ask to sign in, ignore this — nobody has access without it.")


def log_sender(email, code):
    """Development delivery: the code goes to stderr."""
    delivery.warning("DEV DELIVERY — sign-in code for %s is %s (set AUTH_SENDER=telegram "
                     "for real delivery)", mask(email), code)
    return True


def telegram_sender(email, code):
    """Send to the one configured chat. Raises if it did not go.

    A failure has to propagate: the caller's whole job is to know whether the
    person can possibly have received the code, and a swallowed exception here
    turns "your provider is down" into "your code is wrong".
    """
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        raise AuthConfigError("AUTH_SENDER=telegram but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                              "are empty")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json.dumps({"chat_id": chat, "text": message(code)}).encode(),
        {"content-type": "application/json"})
    # No parse_mode, so the body is rendered literally. The URL carries the bot
    # token, which is why no exception from here is ever logged with its args.
    with urllib.request.urlopen(req, timeout=TELEGRAM_TIMEOUT_S):
        pass
    return True


CHANNELS = {"log": log_sender, "telegram": telegram_sender}


def chosen(name=None, env=None):
    """The sender named by AUTH_SENDER, checked at startup rather than on the
    first login attempt at 2am."""
    env = os.environ if env is None else env
    name = (name or env.get("AUTH_SENDER") or "log").strip().lower()
    if name not in CHANNELS:
        raise AuthConfigError(f"AUTH_SENDER={name!r} is not one of {sorted(CHANNELS)}")
    if name == "telegram" and not (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")):
        raise AuthConfigError("AUTH_SENDER=telegram but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                              "are empty — codes would go nowhere and nobody could log in")
    if name == "log":
        log.warning("AUTH_SENDER=log — one-time codes will be written to this log. "
                    "Anyone who can read it can sign in. Do not run this way in production.")
    return CHANNELS[name]
