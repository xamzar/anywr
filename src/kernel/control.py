"""The control server: the page the human takes the browser back in.

Its own process, and it has to be. handoff() blocks inside an MCP tool call, and
the MCP server usually runs on stdio -- a `docker compose exec` pipe with no
listener anywhere in it -- so the Done button cannot talk to the waiting call.
It talks to this server instead, and this server and that call share
handoff.json on a volume.

Always on, tiny, and it never touches the browser: it serves one page, reads one
file and writes one field. Starlette and uvicorn come in with fastmcp, so this
costs no new dependency.
"""
import argparse
import html
import logging
import os
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from kernel import handoff

log = logging.getLogger("kernel.control")

# Where noVNC is, as the *browser* must reach it. In the container that is a
# port on work; through the gateway it is a path on the same origin as this
# page, which is why it cannot be baked in.
DEFAULT_NOVNC = "/view/work/vnc.html"
DEFAULT_PORT = 8787

# The reason never appears here. It arrives as JSON and is written with
# textContent, so model-authored text cannot become markup in a human's browser
# even if it survives handoff.clean_reason() looking like tags.
PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Anywhere Live</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0d0f12; --panel: #161a20; --line: #262c36;
    --ink: #e7ecf3; --dim: #8b95a5; --live: #ffb020; --ok: #2bb673;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--ink); display: flex; flex-direction: column;
    font: 15px/1.45 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  header {
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    border-bottom: 1px solid var(--line); background: var(--panel);
    padding-top: max(10px, env(safe-area-inset-top));
  }
  #dot { width: 9px; height: 9px; border-radius: 50%; background: var(--dim); flex: none; }
  body.live #dot { background: var(--live); animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .25; } }
  h1 { font-size: 14px; font-weight: 600; margin: 0; letter-spacing: .02em; }
  #state { margin-left: auto; font-size: 12px; color: var(--dim); }
  #stage { position: relative; flex: 1; min-height: 0; background: #000; }
  #vnc { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }
  #overlay {
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 8px; text-align: center;
    background: rgba(13, 15, 18, .72); backdrop-filter: blur(1.5px);
    font-size: 15px; color: var(--ink); padding: 16px;
  }
  /* display:flex above beats the browser's own [hidden] rule, so it has to be
     said again -- otherwise "Agent working…" covers the stream the person has
     just been asked to click in. */
  #overlay[hidden] { display: none; }
  #overlay small { color: var(--dim); max-width: 34ch; }
  #ask { border-top: 1px solid var(--line); background: var(--panel); padding: 14px; }
  #ask[hidden] { display: none; }
  #label { font-size: 11px; letter-spacing: .09em; text-transform: uppercase; color: var(--live); }
  #reason { margin: 6px 0 12px; font-size: 17px; font-weight: 500; overflow-wrap: anywhere; }
  #done {
    width: 100%; padding: 15px; border: 0; border-radius: 10px; background: var(--ok);
    color: #04150d; font: inherit; font-size: 16px; font-weight: 700; cursor: pointer;
  }
  #done:disabled { opacity: .5; cursor: default; }
  #hint { margin: 9px 0 0; font-size: 12px; color: var(--dim); text-align: center; }
  @media (min-width: 720px) { #ask { padding: 16px 24px; } #done { width: auto; padding: 14px 28px; } }
</style>

<header>
  <span id="dot"></span>
  <h1>Anywhere Live</h1>
  <span id="state">watching</span>
</header>

<div id="stage">
  <!-- view_only is in the served markup, not added later: if the script never
       runs, the stream is still watch-only. -->
  <iframe id="vnc" title="Cloud browser" data-base="__NOVNC__" src="__SRC__"></iframe>
  <div id="overlay">
    <div>Agent working…</div>
    <small>You are watching the browser. Clicks do nothing until the agent asks for you.</small>
  </div>
</div>

<div id="ask" hidden>
  <div id="label">The agent needs you</div>
  <p id="reason"></p>
  <button id="done">Done — carry on</button>
  <p id="hint">The browser above is live. Finish the step, then press Done.</p>
</div>

<script>
(function () {
  var vnc = document.getElementById('vnc');
  var overlay = document.getElementById('overlay');
  var ask = document.getElementById('ask');
  var reason = document.getElementById('reason');
  var state = document.getElementById('state');
  var done = document.getElementById('done');
  var NOVNC = vnc.dataset.base;
  // Works whether the page is served at / or mounted under a prefix, with or
  // without the trailing slash a phone will drop.
  var base = location.pathname.replace(/\\/?$/, '/');
  var POLL_MS = 2000;

  // Matches the src the server rendered, so the first render never reloads it.
  var interactive = false, current = null, since = null, busy = false;

  function setMode(on) {
    if (on === interactive) return;   // reloading the iframe restarts the stream
    interactive = on;
    vnc.src = NOVNC + '?autoconnect=1&resize=scale' + (on ? '' : '&view_only=1');
    document.body.classList.toggle('live', on);
  }

  function ago() {
    var t = since ? Date.parse(since) : NaN;
    if (isNaN(t)) return 'your turn';
    var s = Math.max(0, Math.round((Date.now() - t) / 1000));
    return 'waiting ' + (s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + (s % 60) + 's');
  }

  function render(s) {
    var pending = !!(s && s.pending);
    current = pending ? s.id : null;
    since = pending ? s.since : null;
    if (pending) reason.textContent = s.reason || 'Something only you can do.';
    ask.hidden = !pending;
    overlay.hidden = pending;
    done.disabled = busy;
    state.textContent = pending ? ago() : 'watching';
    setMode(pending);
  }

  function poll() {
    fetch(base + 'handoff', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(render)
      // A restarting control server is no reason to tear down the stream.
      .catch(function () { state.textContent = 'reconnecting…'; });
  }

  done.onclick = function () {
    busy = true;
    done.disabled = true;
    fetch(base + 'handoff/done', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ id: current })
    }).then(function (r) { return r.json(); })
      .catch(function () { return null; })
      .then(function () { busy = false; poll(); });
  };

  setInterval(poll, POLL_MS);
  setInterval(function () { if (current) state.textContent = ago(); }, 1000);
  poll();
})();
</script>
</html>
"""


def novnc_url():
    return os.environ.get("KERNEL_NOVNC_URL", DEFAULT_NOVNC)


def page(base=None):
    """The viewer, with the noVNC url in it and nothing else substituted.

    Escaped for an attribute rather than interpolated into the script: it is an
    operator's env var, but it is still a string arriving from outside the file
    and there is no reason for it to be able to close a tag.
    """
    base = novnc_url() if base is None else base
    src = f"{base}{'&' if '?' in base else '?'}autoconnect=1&resize=scale&view_only=1"
    return (PAGE.replace("__NOVNC__", html.escape(base, quote=True))
                .replace("__SRC__", html.escape(src, quote=True)))


# --- endpoints --------------------------------------------------------------

def _state(h):
    return {"pending": h.pending, "id": h.id, "reason": h.reason, "since": h.since,
            "resolved": h.resolved, "resolved_at": h.resolved_at}


async def viewer(request):
    return HTMLResponse(page())


async def get_handoff(request):
    # No cache header anywhere in front of this: a proxy holding this response
    # for a second is a person staring at an overlay that will not lift.
    return JSONResponse(_state(handoff.read()), headers={"cache-control": "no-store"})


async def post_done(request):
    """Mark the handoff finished. Always 200 — pressing Done twice is not an
    error, and neither is pressing it for a handoff that already timed out."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an empty or junk body just means "no id"
        body = {}
    wanted = body.get("id") if isinstance(body, dict) else None
    state, cleared = handoff.done(wanted if isinstance(wanted, str) else None)
    log.info("done id=%s cleared=%s", wanted, cleared)
    return JSONResponse({"ok": True, "cleared": cleared, **_state(state)},
                        headers={"cache-control": "no-store"})


async def healthz(request):
    return PlainTextResponse("ok")


app = Starlette(routes=[
    Route("/", viewer),
    Route("/handoff", get_handoff),
    Route("/handoff/done", post_done, methods=["POST"]),
    Route("/healthz", healthz),
])


def main(argv=None):
    logging.basicConfig(level=os.environ.get("KERNEL_LOG", "INFO").upper(), stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Viewer + Done button for the kernel's handoff")
    ap.add_argument("--host", default=os.environ.get("KERNEL_CONTROL_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("KERNEL_CONTROL_PORT",
                                                                  DEFAULT_PORT)))
    args = ap.parse_args(argv)
    log.info("control server on %s:%s — state %s, novnc %s",
             args.host, args.port, handoff.path(), novnc_url())
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
