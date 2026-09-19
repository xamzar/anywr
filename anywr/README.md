# anywr

A cloud Chrome per user that stays logged in, driven by the user's agent over MCP.
The productised (M3/M4) side of this repo, on its own VM. It never touches the
soak VM. Its MCP still uses raw Playwright tools; `src/kernel/` (the digest
kernel) is the obvious next swap.

- **Site:** https://anywr.me — sign-in; `/signup` (username); `/<username>` (sign-in + dashboard)
- **MCP:** `https://anywr.me/mcp/<token>`, one per user, minted on their page
- **Viewer:** `https://anywr.me/<username>#browser` (KasmVNC at `/view/*`, authorised by the session cookie)
- **Macros:** `save_macro` / `macros` / `run_macro` / `delete_macro` let the agent keep its own
  per-user workflows (step lists over the same tools, `{{param}}` placeholders, `macros` table),
  so a repeated job is one call returning only the last step's output

```
browser --> anywr.me (Namecheap DNS, A -> VM 34.92.189.68) --> Caddy --> api (app.py)
                                                                  \--> /view/* -> anywr-u<id>:6080
sign-in:  anywr.me/<user> -> api starts the Access login and submits the owner's email
          -> anywr-auth.xmzr.dev/start (Access bypass app `anywr-start`: adopts that login's
             CF_AppSession, asks only for the code) -> Access callback -> Worker
          (falls back to the plain Access page, where you type the email, if that fails)
          -> anywr.me/auth/callback?t=<HMAC ticket>  -> session cookie
agent --> anywr.me/mcp/<token> -> app.py -> Playwright over CDP -> anywr-u<id>:9223
```

## Pieces

| | |
|---|---|
| `app.py` | Everything server-side: users, sessions, agent tokens, the MCP tools, and the per-user containers (Docker Engine API over the socket). |
| `static/` | `app.html` sign-up, sign-in and dashboard (routes by path). |
| `chrome/` | The per-user browser image (`anywr-chrome`): Chrome on KasmVNC (X server + web viewer on :6080), CDP re-exported on :9223. |
| `auth/` | Cloudflare Worker behind the Access app. Verifies the Access JWT and returns the email as a 2-minute HMAC ticket. |
| `fw.sh` + `anywr-fw.service` | Browsers (172.31.0.0/16) can't reach private ranges or the metadata server. |

## Accounts

No passwords. Sign-up and sign-in both go through Cloudflare Access's one-time PIN,
which verifies the email. The ticket is bound to a state cookie set when the login
began, so it only works in the browser that started it, and only once. Sign-in to
`/<username>` succeeds only if the verified email is that page's email.

Sign-up is open until `MAX_USERS` (45) accounts exist. Zero Trust free plan = 50 Access
seats; past that, swap the Access hop for SMTP OTP.

## Browsers

`anywr-u<id>`: one container, one `/28` network (only it, the api, and Caddy), one
profile volume. Created on first use, stopped after `IDLE_MINUTES` (30) without
MCP calls or viewer heartbeats, and started again on demand (~6 s). At `MAX_RUNNING`
(6, sized for the e2-standard-2), the least-recently-used browser is stopped to
make room. Deleting an account removes all three.

## Deploy

VM `anywr` (asia-east2-a, e2-standard-2, `anywr-ip`, **no service account**).
`ssh anywr`. Code lives in `/opt/anywr`.

```sh
cd anywr
rsync -a --exclude __pycache__ --exclude .env --exclude .sso_secret --exclude auth ./ anywr:/opt/anywr/
ssh anywr 'cd /opt/anywr && docker compose up -d --build'
# Caddyfile changes: `docker compose restart caddy`. rsync swaps the file, and a
# single-file bind mount keeps the old copy, so `caddy reload` alone reads stale config.
# chrome image changes: docker build -t anywr-chrome chrome/  (running browsers pick it up when recreated)
cd auth && npx wrangler deploy
```

`.env` on the VM: `BASE_URL`, `SSO_SECRET`, optional `MAX_USERS`. Worker secrets:
`SSO_SECRET` (same value, local copy in `.sso_secret`) and `ACCESS_AUD`.

Check: `python test_app.py` (no Docker or Cloudflare needed).

## Known ceilings

- One global lock around container start/stop.
- sqlite called synchronously from async handlers.
- The api holds the Docker socket, which is root on the VM.
- Browsers run with `seccomp=unconfined` so Chrome's own sandbox works.
