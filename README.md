# anywr

**A cloud Chrome that stays logged in, driven by your AI agent over MCP.**

Sign up at [anywr.me](https://anywr.me), paste your personal MCP link into Claude (or any MCP client),
and your agent gets its own real browser: your logins persist between sessions, you can watch
and take over live, and repeated workflows collapse into one call.

> Demo: from a chat, "add these products to my Amazon cart". The agent opens Amazon in *my*
> signed-in browser, searches, picks the right items and adds them, while I watch it happen live
> in the viewer. No Amazon API, no credentials handed to the model.

## Why

Most of the web has no API, and the parts you care about sit behind a login. Headless
browsers in agent sandboxes start fresh every time, so the agent gets stuck at the login page.
anywr gives each user a single long-lived Chrome profile in the cloud:

- **Stays logged in.** Cookies and sessions live in a persistent profile volume, so you sign in once.
- **Human handoff.** For logins, 2FA or captchas, the agent calls `handoff()` and you finish
  the step in the live viewer (works from your phone), then the agent carries on.
- **Macros.** The agent can save a workflow it repeats (`save_macro`) and later run it with
  `run_macro`: one call, one result, instead of a dozen snapshot/click round trips.
- **Works from anywhere.** It's a remote MCP server over HTTPS, so Claude on desktop, web or
  mobile can use it.

## MCP tools

| Tool | What it does |
|---|---|
| `tabs` | List open tabs and the live viewer link (wakes the browser if asleep) |
| `open` | Navigate a tab, or open a new one |
| `snapshot` | Accessibility tree of the page, for picking selectors |
| `screenshot` | PNG of a tab |
| `click` / `fill` / `press` / `select` | Interact using Playwright selectors (`role=button[name="Sign in"]`, `text=Next`, CSS) |
| `evaluate` | Run JS in the page and return JSON |
| `close_tab` | Close a tab (the last one is kept) |
| `handoff` | Ask the human to finish something in the viewer |
| `save_macro` / `macros` / `run_macro` / `delete_macro` | Per-user saved workflows with `{{param}}` placeholders |

## How it works

```
you (browser) --> anywr.me --> Caddy --> api (app.py)
                                    \--> /view/*  -> your container's KasmVNC viewer
agent (MCP)   --> anywr.me/mcp/<token> -> app.py -> Playwright over CDP -> your container's Chrome
sign-in       --> Cloudflare Access one-time PIN -> auth Worker -> signed ticket -> session cookie
```

- **One container per user** (`anywr-u<id>`): Google Chrome on KasmVNC, with CDP exposed only
  on that user's private Docker network. Created on first use, stopped after 30 idle minutes,
  restarted on demand in about 6 s. The least-recently-used browser is evicted when the host is full.
- **No passwords.** Email is verified with Cloudflare Access's one-time PIN; a small Worker
  (`auth/`) checks the Access JWT itself and returns the email as a 2-minute HMAC ticket bound
  to the browser that started the login.
- **The MCP link is the credential.** Each user mints one on their dashboard; it's stored
  hashed, can be rotated or revoked, and is redacted from access logs. The user is always
  derived from the token, never from anything the caller sends.

| Path | |
|---|---|
| `anywr/app.py` | Everything server-side: users, sessions, agent tokens, MCP tools, and container lifecycle via the Docker Engine API |
| `anywr/static/app.html` | Sign-up, sign-in and dashboard |
| `anywr/chrome/` | The per-user browser image: Chrome + KasmVNC + fluxbox, CDP re-exported on :9223 |
| `anywr/auth/` | Cloudflare Worker behind the Access app |
| `anywr/fw.sh`, `anywr-fw.service` | Egress firewall: browsers can't reach private ranges or the cloud metadata server |
| `anywr/test_app.py` | Self-check: sign-up cap, tickets, tenancy pinning, macros (no Docker or Cloudflare needed) |

## Security model

- Every browser sits on its own `/28` Docker network with only itself, the api and Caddy,
  so one user's browser can't reach another's CDP or viewer.
- The viewer is gated by Caddy `forward_auth` against the session cookie.
- `fw.sh` drops traffic from browsers to RFC 1918, link-local (metadata) and CGNAT ranges.
- The host VM runs with **no cloud service account**, so a compromised browser has no credentials to steal.
- Chrome's own sandbox stays on (`seccomp=unconfined` + `no-new-privileges`) instead of `--no-sandbox`.
- Macros can only call the browser tools above, never arbitrary Python; `{{params}}` inside
  `evaluate` are injected as JSON literals.

## Self-hosting

Everything below runs from `anywr/`. You need a Linux VM with Docker, a domain pointed at it, and a Cloudflare Zero Trust team
(the free plan gives 50 Access seats) for email sign-in.

```sh
docker build -t anywr-chrome chrome/
cp .env.example .env            # BASE_URL, SSO_SECRET, optional MAX_USERS
# edit the domain in Caddyfile
docker compose up -d --build
sudo cp anywr-fw.service /etc/systemd/system/ && sudo systemctl enable --now anywr-fw

cd auth                          # set account_id, TEAM and routes in wrangler.jsonc
npx wrangler secret put SSO_SECRET   # same value as in .env
npx wrangler secret put ACCESS_AUD   # the Access app's audience tag
npx wrangler deploy
```

`fw.sh` and `anywr-fw.service` expect the code at `/opt/anywr`. Set `DEMO_OTP=1` to skip
email entirely (any 6-digit code works). Only use it for demos.

Run the self-check:

```sh
pip install "fastmcp==4.0.5" fastapi uvicorn httpx playwright==1.55.0
DB=/tmp/anywr-test.db python test_app.py
```

## Known limits

Deliberate shortcuts, fine at tens of users:

- One global lock around container start/stop.
- sqlite called synchronously from async handlers.
- The api holds the Docker socket, which is root-equivalent on the host.
- Sign-up is capped at `MAX_USERS` (default 45) to stay under the Access free-plan seats; past
  that, swap the Access hop for SMTP OTP.

## How we got here

anywr is the product end of a research repo, and the research is still here:

- **Can a cloud browser stay logged in?** The whole idea rests on a browser profile parked on
  a datacenter IP staying signed in to real sites for days, across restarts. `plan.md` is the
  experiment design; `soak.md` is the rig (a GCP VM probing AIMS, Canvas, Google, GitHub and
  LinkedIn every 6 hours), with the code in `src/` (`probe.py`, `classify.py`, `report.py`).
- **Digest kernel** (`src/kernel/`, `findings-m1.md`): instead of dumping the whole
  accessibility tree, the agent sees a compact digest of what it can act on. On a real
  university-portal task (fetch this semester's grades) it used **7.6x less context** than
  stock Playwright snapshots for the same completed result. anywr's MCP still uses the raw
  Playwright tools; the kernel is the next swap.
- **Handoff** (`src/kernel/handoff.py`): the agent asks, the human clears the login, 2FA or
  captcha in the live viewer, the agent resumes. anywr's `handoff()` comes from here.
- **Promotion** (M5): record a workflow once, export it as an adapter, replay and verify it.

Tests for the research side: `pytest` (see `tests/`).

## Credits

Built by [@xamzar](https://github.com/xamzar) and [@asanbl4](https://github.com/asanbl4)
(the digest kernel, handoff and promotion).
