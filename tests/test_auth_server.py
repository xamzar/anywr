"""The front door end to end, in process.

Driven through Starlette's test client over ASGI: nothing here opens a socket,
and the sender is a list the test reads afterwards, so no code leaves the
process even in principle. The base url is https so the test client's jar keeps
a Secure cookie -- which is also the assertion that the cookie is Secure.
"""
import logging
import pathlib
import re

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from auth import core, send as send_mod, server, users as users_mod  # noqa: E402
from auth.core import AuthConfigError  # noqa: E402

SECRET_TEXT = "b3f1a9c2d4e6f80123456789abcdef0123456789abcdef0123456789abcdef01"
SECRET = SECRET_TEXT.encode()
OTHER = ("9" + SECRET_TEXT[1:]).encode()
EMAIL = "asan@example.com"
USER = "asanbl4"
STRANGER = "nobody@example.com"
JSON = {"content-type": "application/json"}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, seconds):
        self.t += seconds


class Outbox(list):
    """The sender, injected. Records instead of sending."""

    def __call__(self, email, code):
        self.append((email, code))
        return True

    @property
    def code(self):
        return self[-1][1]


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def users():
    return users_mod.parse({USER: EMAIL})


@pytest.fixture
def app(users, outbox, clock):
    return server.create_app(secret=SECRET, users=users, sender=outbox,
                             codes=core.Codes(SECRET, clock=clock),
                             limiter=core.Limiter(clock=clock), workspace="/{user}/")


@pytest.fixture
def client(app):
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def ask(client, email=EMAIL):
    return client.post("/start", json={"email": email}, headers=JSON)


def enter(client, code, email=EMAIL):
    return client.post("/verify", json={"email": email, "code": code}, headers=JSON)


def with_cookie(client, token):
    return client.get("/check", headers={"cookie": f"{server.COOKIE}={token}"})


# --- the page ---------------------------------------------------------------

def test_the_sign_in_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert 'id="email"' in r.text and 'id="code"' in r.text


def test_the_page_asks_for_no_password(client):
    """There are no passwords anywhere in this service, and the page is where
    that would first quietly stop being true."""
    assert 'type="password"' not in client.get("/").text


def test_the_page_never_writes_untrusted_text_as_html(client):
    assert "innerHTML" not in server.PAGE and "document.write" not in server.PAGE
    assert "textContent" in server.PAGE


def test_the_page_is_self_contained(client):
    body = client.get("/").text
    assert "<style>" in body and "<script>" in body
    for remote in ("http://cdn", "https://cdn", "unpkg", "jsdelivr", "googleapis"):
        assert remote not in body


def test_nothing_on_the_auth_path_is_cached(client):
    """A proxy or a phone holding one of these is a session cached for the next
    person to use the same network."""
    assert client.get("/").headers["cache-control"] == "no-store"
    assert ask(client).headers["cache-control"] == "no-store"
    assert client.get("/check").headers["cache-control"] == "no-store"


# --- the happy path ---------------------------------------------------------

def test_the_whole_flow(client, outbox):
    assert ask(client).status_code == 200
    assert outbox and outbox[0][0] == EMAIL
    r = enter(client, outbox.code)
    assert r.status_code == 200 and r.json()["redirect"] == f"/{USER}/"
    # The cookie the client kept is enough to pass the gateway's check.
    check = client.get("/check")
    assert check.status_code == 204 and check.headers[server.USER_HEADER] == USER


def test_the_cookie_is_signed_httponly_secure_lax_and_lasts_a_day(client, outbox):
    ask(client)
    raw = enter(client, outbox.code).headers["set-cookie"]
    assert raw.startswith(f"{server.COOKIE}=v1.{USER}.")
    assert "HttpOnly" in raw and "Secure" in raw
    assert "SameSite=lax" in raw.replace("SameSite=Lax", "SameSite=lax")
    assert "Max-Age=86400" in raw and "Path=/" in raw


def test_the_redirect_is_a_local_path(client, outbox):
    ask(client)
    assert enter(client, outbox.code).json()["redirect"].startswith(f"/{USER}")


@pytest.mark.parametrize("template", ["https://evil.example/{user}", "//evil.example/{user}",
                                      "javascript:alert(1)"])
def test_a_workspace_template_that_leaves_the_site_is_ignored(template):
    assert server.workspace_url(USER, template) == f"/{USER}/"


def test_the_address_is_normalised_before_it_is_looked_up(client, outbox):
    assert ask(client, "  ASAN@Example.com ").status_code == 200
    assert outbox, "a known address in another spelling still gets a code"
    assert enter(client, outbox.code, "asan@EXAMPLE.com").status_code == 200


# --- no user enumeration ----------------------------------------------------

def test_an_unknown_address_is_indistinguishable_from_a_known_one(client, outbox):
    known = ask(client, EMAIL)
    unknown = ask(client, STRANGER)
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert [k for k in known.headers if k != "date"] == [k for k in unknown.headers if k != "date"]
    assert len(outbox) == 1 and outbox[0][0] == EMAIL


def test_a_malformed_address_gets_the_same_answer_too(client, outbox):
    assert ask(client, "not-an-address").json() == ask(client, EMAIL).json()


def test_a_rate_limited_address_gets_the_same_answer_too(client, outbox):
    """Otherwise the limiter itself becomes the oracle: only a real user could
    ever be told they had asked too often."""
    first = ask(client)
    for _ in range(6):
        last = ask(client)
    assert last.status_code == first.status_code and last.json() == first.json()
    assert len(outbox) == 3, "three codes per window, and the rest are silently dropped"


def test_a_wrong_code_and_an_unknown_address_fail_identically(client, outbox):
    ask(client)
    wrong = enter(client, "000000" if outbox.code != "000000" else "111111")
    nobody = enter(client, "123456", STRANGER)
    assert wrong.status_code == nobody.status_code == 401
    assert wrong.json() == nobody.json()


def test_an_expired_code_fails_with_the_same_words(client, outbox, clock):
    ask(client)
    clock.tick(core.CODE_TTL_S + 1)
    expired = enter(client, outbox.code)
    assert expired.status_code == 401
    assert expired.json()["message"] == server.BAD_CODE
    assert "expire" not in expired.text.lower()   # not even a hint that it existed


# --- the code ---------------------------------------------------------------

def test_a_wrong_code_is_refused(client, outbox):
    ask(client)
    wrong = "000000" if outbox.code != "000000" else "111111"
    r = enter(client, wrong)
    assert r.status_code == 401 and "set-cookie" not in r.headers


def test_a_code_cannot_be_replayed(client, outbox):
    ask(client)
    assert enter(client, outbox.code).status_code == 200
    again = enter(client, outbox.code)
    assert again.status_code == 401 and "set-cookie" not in again.headers


def test_the_guess_limit_burns_the_code(client, outbox):
    ask(client)
    wrong = "000000" if outbox.code != "000000" else "111111"
    for _ in range(core.MAX_ATTEMPTS):
        assert enter(client, wrong).status_code == 401
    assert enter(client, outbox.code).status_code == 401, "the right code died with the wrong ones"


def test_a_second_request_invalidates_the_first_code(client, outbox):
    ask(client)
    first = outbox.code
    ask(client)
    assert first != outbox.code
    assert enter(client, first).status_code == 401
    assert enter(client, outbox.code).status_code == 200


def test_a_form_post_is_refused(client):
    """application/json is required, so a cross-site form cannot drive either
    step — which is the CSRF defence for the one that sets a cookie."""
    r = client.post("/verify", data={"email": EMAIL, "code": "123456"})
    assert r.status_code == 415 and "set-cookie" not in r.headers


def test_a_junk_body_is_a_bad_request_not_a_traceback(client):
    assert client.post("/start", content=b"{not json", headers=JSON).status_code == 400
    assert client.post("/verify", content=b"[]", headers=JSON).status_code == 400


def test_the_steps_are_not_gets(client):
    assert client.get("/start").status_code == 405
    assert client.get("/verify").status_code == 405


# --- the gateway's question -------------------------------------------------

def test_check_is_401_without_a_cookie(client):
    assert client.get("/check").status_code == 401


def test_check_is_204_with_one_and_names_the_user(client, outbox):
    ask(client)
    enter(client, outbox.code)
    r = client.get("/check")
    assert 200 <= r.status_code < 300
    assert r.headers[server.USER_HEADER] == USER


def test_check_refuses_a_tampered_cookie(client, outbox):
    ask(client)
    enter(client, outbox.code)
    good = client.cookies[server.COOKIE]
    assert with_cookie(client, good.replace(USER, "root")).status_code == 401
    body, _, mac = good.rpartition(".")
    assert with_cookie(client, f"{body}.{'A' if mac[0] != 'A' else 'B'}{mac[1:]}").status_code == 401


def test_check_refuses_an_expired_cookie(client):
    stale = core.sign(USER, SECRET, now=core.time.time() - core.SESSION_MAX_AGE_S - 10)
    assert with_cookie(client, stale).status_code == 401


def test_check_refuses_a_cookie_signed_with_a_different_secret(client):
    assert with_cookie(client, core.sign(USER, OTHER)).status_code == 401


def test_check_refuses_a_user_who_is_no_longer_in_the_file(users, outbox, clock):
    """Deleting the line is the only revocation there is before the cookie
    expires, so it has to be the file that decides, not the cookie."""
    app = server.create_app(secret=SECRET, users=users_mod.parse({"someone": "s@example.com"}),
                            sender=outbox, codes=core.Codes(SECRET, clock=clock))
    with TestClient(app, base_url="https://testserver") as c:
        assert with_cookie(c, core.sign(USER, SECRET)).status_code == 401


def test_check_never_sets_a_cookie(client, outbox):
    """It is on every request the product serves; it reads, it does not renew.
    A sliding session here would be a 24h maximum that never actually ends."""
    ask(client)
    enter(client, outbox.code)
    assert "set-cookie" not in client.get("/check").headers


def test_logout_clears_the_cookie(client, outbox):
    ask(client)
    enter(client, outbox.code)
    r = client.post("/logout", headers=JSON)
    assert "Max-Age=0" in r.headers["set-cookie"] or "1970" in r.headers["set-cookie"]
    assert client.get("/check").status_code == 401


def test_healthz_needs_nothing(client):
    assert client.get("/healthz").text == "ok"


# --- refusing to start ------------------------------------------------------

def test_the_service_refuses_to_start_without_a_secret(monkeypatch):
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    with pytest.raises(AuthConfigError):
        server.create_app()


def test_the_service_refuses_to_start_with_a_short_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "short")
    with pytest.raises(AuthConfigError):
        server.create_app()


def test_main_exits_nonzero_rather_than_serve_without_a_secret(monkeypatch, caplog):
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    with caplog.at_level(logging.ERROR):
        assert server.main([]) == 2
    assert "AUTH_SECRET" in caplog.text


def test_there_is_no_default_secret_anywhere_in_the_service():
    """The failure this whole check exists for: a fallback key in the source,
    shared by every deployment that never set the variable."""
    import inspect
    for mod in (core, server, send_mod, users_mod):
        # get("AUTH_SECRET", <anything>) is the whole failure, in one regex.
        assert not re.search(r'AUTH_SECRET["\']\s*,', inspect.getsource(mod))


# --- logs -------------------------------------------------------------------

def test_no_code_secret_or_token_reaches_the_log(client, outbox, caplog):
    """The service's own logging, over a whole successful flow plus a failed
    one. The only thing allowed to write a code down is the log *sender*, which
    is a delivery channel and is not wired in here."""
    with caplog.at_level(logging.DEBUG, logger="auth"):
        ask(client)
        code = outbox.code
        enter(client, "000000" if code != "000000" else "111111")
        enter(client, code)
        token = client.cookies[server.COOKIE]
        client.get("/check")
    text = caplog.text
    assert code not in text
    assert SECRET_TEXT not in text and SECRET_TEXT[:16] not in text
    assert token not in text and token.rpartition(".")[2] not in text
    assert EMAIL not in text                       # not even the address
    assert not re.search(r"\b[0-9]{6}\b", text)    # nor anything code-shaped
    assert "signed in user=asanbl4" in text        # it still says what happened


def test_a_delivery_failure_does_not_log_the_code(client, caplog):
    def explode(email, code):
        raise RuntimeError(f"smtp said no for {email} code {code}")

    app = server.create_app(secret=SECRET, users=users_mod.parse({USER: EMAIL}), sender=explode)
    with TestClient(app, base_url="https://testserver") as c, \
            caplog.at_level(logging.DEBUG, logger="auth"):
        assert ask(c).status_code == 200, "the person is still told nothing went wrong"
    assert "smtp said no" not in caplog.text       # the exception quotes the code
    assert not re.search(r"\b[0-9]{6}\b", caplog.text)
    assert "could not deliver" in caplog.text


def test_the_log_sender_is_the_one_place_a_code_is_written_down(caplog):
    """Honest about the default: it exists so the flow works with nothing
    configured, and it means the container log is a credential."""
    with caplog.at_level(logging.DEBUG, logger="auth"):
        send_mod.log_sender(EMAIL, "424242")
    assert "424242" in caplog.text
    assert EMAIL not in caplog.text and "a***@example.com" in caplog.text


# --- senders ----------------------------------------------------------------

def test_the_default_sender_is_the_log_one_and_says_so(caplog):
    with caplog.at_level(logging.WARNING):
        assert send_mod.chosen(env={}) is send_mod.log_sender
    assert "production" in caplog.text


def test_an_unknown_sender_refuses_to_start():
    with pytest.raises(AuthConfigError):
        send_mod.chosen(env={"AUTH_SENDER": "smtp"})


def test_telegram_without_credentials_refuses_to_start():
    """config/.env has both variables present and empty, which is exactly the
    way this fails silently: codes go nowhere and nobody can log in."""
    with pytest.raises(AuthConfigError):
        send_mod.chosen(env={"AUTH_SENDER": "telegram", "TELEGRAM_BOT_TOKEN": "",
                             "TELEGRAM_CHAT_ID": ""})


def test_telegram_is_selectable_when_configured():
    assert send_mod.chosen(env={"AUTH_SENDER": "telegram", "TELEGRAM_BOT_TOKEN": "t",
                                "TELEGRAM_CHAT_ID": "1"}) is send_mod.telegram_sender


def test_the_message_carries_the_code_and_no_link():
    """A sign-in message that trains you to tap a link is a phishing lesson."""
    body = send_mod.message("424242")
    assert "424242" in body
    assert "http://" not in body and "https://" not in body


# --- users.yaml -------------------------------------------------------------

def test_users_are_indexed_both_ways(users):
    assert users.username_for(EMAIL) == USER and users.known(USER)
    assert users.username_for(STRANGER) == "" and not users.known("root")


def test_users_addresses_are_normalised():
    u = users_mod.parse({USER: "  ASAN@Example.COM "})
    assert u.username_for(EMAIL) == USER


@pytest.mark.parametrize("bad", [
    {}, [], None, "asanbl4: a@b.c",
    {"Asan": EMAIL}, {"a b": EMAIL}, {"a/b": EMAIL},
    {USER: "not-an-address"}, {USER: None}, {USER: ""},
    {USER: EMAIL, "other": EMAIL.upper()},        # one address, two accounts
])
def test_a_users_file_that_could_be_misread_is_refused(bad):
    with pytest.raises(AuthConfigError):
        users_mod.parse(bad)


def test_a_missing_users_file_is_a_startup_error(tmp_path):
    with pytest.raises(AuthConfigError) as exc:
        users_mod.load(str(tmp_path / "nope.yaml"))
    assert "users.example.yaml" in str(exc.value)


def test_the_example_file_is_loadable_and_has_no_secrets_in_it():
    here = pathlib.Path(__file__).resolve().parent.parent / "config" / "users.example.yaml"
    u = users_mod.load(str(here))
    assert len(u) == 1
    # A committed file: a placeholder address, and nothing that could be a
    # credential. There is no password field to leave filled in by accident.
    assert list(u.by_email) == ["you@example.com"]
    assert not re.search(r"(?i)(secret|token|passw)\S*\s*[:=]\s*\S", here.read_text())


def test_a_real_users_file_would_load(tmp_path):
    p = tmp_path / "users.yaml"
    p.write_text(f"{USER}: {EMAIL}\n")
    assert users_mod.load(str(p)).username_for(EMAIL) == USER
