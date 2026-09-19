"""The front door: the sign-in page, the two steps behind it, and /check.

Same shape as kernel/control.py -- one Starlette app, one self-contained page,
JSON endpoints under it, run with `python -m auth.server`. It holds no browser,
no state file and no database: the codes live in memory for five minutes and the
session lives in a signed cookie, so the only thing this process has that
matters is AUTH_SECRET, and it will not start without one.

create_app() takes every collaborator as an argument. That is not ceremony: it
is how the tests drive the whole flow with their own clock, their own users and
a sender that goes nowhere near a network.

Caddy asks /check about every request to a workspace (forward_auth). It answers
204 with the username in a header, or 401. Nothing else in this file is on that
path, because that one is on every request the product serves.
"""
import argparse
import html
import logging
import os
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from auth import core, send as send_mod, users as users_mod
from auth.core import AuthConfigError

log = logging.getLogger("auth.server")

COOKIE = "anywr_session"
USER_HEADER = "X-Auth-User"
DEFAULT_PORT = 8788
DEFAULT_WORKSPACE = "/{user}/"
# Where the 401 body points. It is the path the *gateway* serves this app at,
# which this app cannot know, so it is an env var with a sane default.
DEFAULT_LOGIN_PATH = "/auth/"

# Said the same way whatever happened. Whether an address is registered, whether
# a code existed, whether it had expired and whether the limiter refused are all
# answered with these two sentences -- the whole point of step one is that the
# response carries no information about who has an account here.
SENT = "If that address can sign in, a code is on its way. It expires in 5 minutes."
BAD_CODE = "That code is not valid. Ask for a new one and try again."

NO_STORE = {"cache-control": "no-store"}

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Anywhere Live — sign in</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0d0f12; --panel: #161a20; --line: #262c36;
    --ink: #e7ecf3; --dim: #8b95a5; --live: #ffb020; --ok: #2bb673; --bad: #ff6b6b;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--ink); display: flex;
    align-items: center; justify-content: center; padding: 16px;
    font: 15px/1.45 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  main {
    width: 100%; max-width: 380px; background: var(--panel);
    border: 1px solid var(--line); border-radius: 14px; padding: 22px;
  }
  h1 { font-size: 15px; font-weight: 600; margin: 0 0 2px; letter-spacing: .02em; }
  p.sub { margin: 0 0 18px; font-size: 13px; color: var(--dim); }
  label { display: block; font-size: 11px; letter-spacing: .09em;
          text-transform: uppercase; color: var(--live); margin-bottom: 7px; }
  input {
    width: 100%; padding: 13px; border: 1px solid var(--line); border-radius: 10px;
    background: #0d1014; color: var(--ink); font: inherit; font-size: 16px;
  }
  input:focus { outline: 2px solid var(--live); outline-offset: -1px; }
  #code { letter-spacing: .5em; text-align: center; font-size: 22px; }
  button {
    width: 100%; margin-top: 12px; padding: 14px; border: 0; border-radius: 10px;
    background: var(--ok); color: #04150d; font: inherit; font-size: 16px;
    font-weight: 700; cursor: pointer;
  }
  button:disabled { opacity: .5; cursor: default; }
  #back { background: none; color: var(--dim); font-weight: 400; font-size: 13px;
          padding: 10px; margin-top: 4px; }
  #note { margin: 14px 0 0; font-size: 13px; color: var(--dim); min-height: 1.4em;
          overflow-wrap: anywhere; }
  #note.bad { color: var(--bad); }
  form[hidden] { display: none; }
</style>

<main>
  <h1>Anywhere Live</h1>
  <p class="sub">Your browser, wherever you are.</p>

  <form id="step1">
    <label for="email">Email</label>
    <input id="email" name="email" type="email" autocomplete="email" inputmode="email"
           autocapitalize="off" spellcheck="false" required>
    <button id="ask" type="submit">Send me a code</button>
  </form>

  <form id="step2" hidden>
    <label for="code">6-digit code</label>
    <input id="code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code"
           pattern="[0-9]*" maxlength="6" required>
    <button id="go" type="submit">Sign in</button>
    <button id="back" type="button">Use a different address</button>
  </form>

  <p id="note"></p>
</main>

<script>
(function () {
  var step1 = document.getElementById('step1'), step2 = document.getElementById('step2');
  var email = document.getElementById('email'), code = document.getElementById('code');
  var ask = document.getElementById('ask'), go = document.getElementById('go');
  var back = document.getElementById('back'), note = document.getElementById('note');
  // Works whether the page is served at / or mounted under a prefix, with or
  // without the trailing slash a phone will drop.
  var base = location.pathname.replace(/\\/?$/, '/');

  // textContent only: nothing the server says is ever written as markup here.
  function say(text, bad) { note.textContent = text || ''; note.className = bad ? 'bad' : ''; }

  function post(path, body) {
    return fetch(base + path, {
      method: 'POST',
      // application/json is not a form content type, so a cross-site page
      // cannot post this without a preflight we never answer. That is the CSRF
      // defence for the step that hands out a cookie.
      headers: { 'content-type': 'application/json' },
      cache: 'no-store',
      body: JSON.stringify(body)
    }).then(function (r) { return r.json().then(function (j) { return [r.ok, j]; }); });
  }

  step1.onsubmit = function (e) {
    e.preventDefault();
    ask.disabled = true;
    say('Sending…');
    post('start', { email: email.value }).then(function (res) {
      say(res[1].message);
      step1.hidden = true; step2.hidden = false; code.value = ''; code.focus();
    }).catch(function () { say('Could not reach the server. Try again.', true); })
      .then(function () { ask.disabled = false; });
  };

  step2.onsubmit = function (e) {
    e.preventDefault();
    go.disabled = true;
    say('Checking…');
    post('verify', { email: email.value, code: code.value }).then(function (res) {
      if (res[0] && res[1].redirect) { location.assign(res[1].redirect); return; }
      say(res[1].message, true);
      code.value = ''; code.focus();
    }).catch(function () { say('Could not reach the server. Try again.', true); })
      .then(function () { go.disabled = false; });
  };

  back.onclick = function () {
    step2.hidden = true; step1.hidden = false; say(''); email.focus();
  };
})();
</script>
</html>
"""


def workspace_url(username, template=None):
    """Where a signed-in user lands. Path-only and single-slash, so the template
    cannot be turned into an open redirect by an operator's typo or by anything
    that ever gets interpolated into it."""
    template = template if template is not None else os.environ.get("AUTH_WORKSPACE",
                                                                   DEFAULT_WORKSPACE)
    url = template.replace("{user}", username)
    if not url.startswith("/") or url.startswith("//"):
        log.warning("AUTH_WORKSPACE is not a local path — falling back to %s", DEFAULT_WORKSPACE)
        url = DEFAULT_WORKSPACE.replace("{user}", username)
    return url


async def _json_body(request):
    """(body, status). status is 0 when the body is a usable JSON object.

    application/json is required, not merely accepted: a browser cannot send it
    cross-origin without a preflight this service never answers, so insisting on
    it is what stops another site from posting a code and a cookie into a
    visitor's browser. A form post is refused before it is parsed.
    """
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return None, 415
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - junk body is a bad request, not a traceback
        return None, 400
    return (body, 0) if isinstance(body, dict) else (None, 400)


def _deliver(send, email, code):
    """Run after the response has gone out.

    Off the request path on purpose: a real provider takes a few hundred
    milliseconds and an unknown address takes none, and that difference is a
    user-enumeration oracle that no amount of identical wording would close.
    The exception is logged without its arguments -- a provider's exception text
    can quote the request, and the request has the code in it.
    """
    try:
        send(email, code)
    except Exception as exc:  # noqa: BLE001 - delivery is best effort, always
        log.warning("could not deliver a code to %s (%s)", core.mask(email), type(exc).__name__)


def create_app(*, secret=None, users=None, sender=None, codes=None, limiter=None,
               workspace=None):
    """Wire the service. Raises AuthConfigError rather than start half-configured."""
    secret = core.load_secret() if secret is None else secret
    users = users_mod.load() if users is None else users
    sender = send_mod.chosen() if sender is None else sender
    codes = core.Codes(secret) if codes is None else codes
    limiter = core.Limiter() if limiter is None else limiter

    async def page(request):
        return HTMLResponse(PAGE, headers=NO_STORE)

    async def start(request):
        """Step one. Always the same answer, in the same time, to everyone."""
        body, bad = await _json_body(request)
        if bad:
            return JSONResponse({"ok": False, "message": SENT}, status_code=bad, headers=NO_STORE)
        email = core.normal_email(body.get("email"))
        username = users.username_for(email) if email else ""
        allowed = bool(email) and limiter.allow(email)
        task = None
        if username and allowed:
            # Only known addresses get a challenge, so the challenge table's
            # keys come from users.yaml and not from whoever is posting. The
            # work an unknown address skips is one in-memory HMAC, which is
            # thousands of times below the noise on the wire.
            task = BackgroundTask(_deliver, sender, email, codes.issue(username, email))
        # The username, because it is not a secret and an operator needs to see
        # a flood. Never the address, never the code, and the client is told the
        # same sentence whatever this line says.
        log.info("code requested user=%s sent=%s", username or "-", bool(task))
        return JSONResponse({"ok": True, "message": SENT}, headers=NO_STORE, background=task)

    async def verify(request):
        """Step two. One failure message for every way it can fail."""
        body, bad = await _json_body(request)
        if bad:
            return JSONResponse({"ok": False, "message": BAD_CODE}, status_code=bad,
                                headers=NO_STORE)
        email = core.normal_email(body.get("email"))
        code = body.get("code")
        verdict = codes.verify(email, code) if email else core.Verdict(False, reason="no-address")
        if not verdict.ok:
            log.info("sign-in refused (%s)", verdict.reason)
            return JSONResponse({"ok": False, "message": BAD_CODE}, status_code=401,
                                headers=NO_STORE)
        token = core.sign(verdict.username, secret)
        log.info("signed in user=%s", verdict.username)
        resp = JSONResponse({"ok": True, "redirect": workspace_url(verdict.username, workspace)},
                            headers=NO_STORE)
        resp.set_cookie(COOKIE, token, max_age=core.SESSION_MAX_AGE_S, path="/",
                        httponly=True, secure=True, samesite="lax")
        return resp

    async def check(request):
        """What Caddy's forward_auth calls, on every request to a workspace.

        204 and the username, or 401. The name is re-checked against users.yaml
        rather than trusted from the cookie alone: deleting a line from that
        file is the only way to revoke a session before it expires, and it only
        works if this is where the answer comes from.
        """
        username = core.check(request.cookies.get(COOKIE, ""), secret)
        if not username or not users.known(username):
            # 401 rather than a redirect: Caddy copies a non-2xx response
            # straight to the client, and a redirect here would send XHR and
            # image requests to the login page as if they were navigations.
            # Escaped like control.py's noVNC url: an operator's env var is
            # still a string from outside this file, and it has no business
            # being able to close an attribute.
            where = html.escape(os.environ.get("AUTH_LOGIN_PATH", DEFAULT_LOGIN_PATH), quote=True)
            return HTMLResponse('<!doctype html><meta charset="utf-8"><title>Sign in</title>'
                                f'<p>Not signed in. <a href="{where}">Sign in</a>.',
                                status_code=401, headers=NO_STORE)
        return Response(status_code=204, headers={USER_HEADER: username, **NO_STORE})

    async def logout(request):
        resp = JSONResponse({"ok": True}, headers=NO_STORE)
        resp.delete_cookie(COOKIE, path="/", httponly=True, secure=True, samesite="lax")
        return resp

    async def healthz(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[
        Route("/", page),
        Route("/start", start, methods=["POST"]),
        Route("/verify", verify, methods=["POST"]),
        Route("/check", check),
        Route("/logout", logout, methods=["POST"]),
        Route("/healthz", healthz),
    ])
    app.state.users = users
    app.state.codes = codes
    app.state.limiter = limiter
    return app


def main(argv=None):
    logging.basicConfig(level=os.environ.get("AUTH_LOG", "INFO").upper(), stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Email OTP sign-in for Anywhere Live")
    ap.add_argument("--host", default=os.environ.get("AUTH_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("AUTH_PORT", DEFAULT_PORT)))
    args = ap.parse_args(argv)
    try:
        app = create_app()
    except AuthConfigError as exc:
        # Refusing to start is the feature. One line, no traceback, nothing to
        # copy-paste around: it says what is wrong and how to fix it.
        log.error("not starting: %s", exc)
        return 2
    log.info("auth server on %s:%s — %d user(s), codes expire in %ds",
             args.host, args.port, len(app.state.users), core.CODE_TTL_S)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
