# workspace-hub — Track 0 soak rig

Does a browser profile on a datacenter IP stay logged in for 7+ days, across
restarts? Design and decision rules: `plan.md`. Results: `findings.md` (day 7).

## What is running where

- **VM** `soak` — GCP `asia-east2-a` (Hong Kong, same region as mcp-hub, near
  CityU), e2-medium, Ubuntu 24.04, static IP `34.92.248.157` (`soak-ip`).
  Swapped for the plan's Hetzner box: GCP already hosts everything else, and a
  reserved GCE address is an equally static egress.
- **Container** `soak` — one long-lived Google Chrome on Xvfb, profile on the
  `profile` volume, CDP on 127.0.0.1:9222, noVNC on host loopback :6080.
  Chrome policy (`docker/chrome-policy.json`) turns on restore-last-session (so
  session cookies survive restarts) and turns off the password manager.
- **Host cron** (`scripts/crontab`): probe every 6 h, Telegram digest 23:00 UTC
  (07:00 HKT), `scripts/cycle.sh` 02:30 UTC.
- **GCE instance schedule** `soak-daily-start` starts the VM at 02:45 UTC, which
  powers it back on after `cycle.sh` powers it off.

## Access

    ssh soak            # ~/.ssh/config forwards localhost:6080
    open http://localhost:6080/vnc.html?autoconnect=1&resize=scale

No VNC password: noVNC is bound to the VM's loopback, so the SSH tunnel is the gate.
Firewall: GCP project rule `allow-ssh-direct` (tcp/22, key-only sshd) is the only
way in; nothing else is published.

## MCP: agent control of the browsers

`src/mcp_server.py` (container `mcp`) drives the workspace Chromes over CDP with
Playwright. Tools: `workspaces`, `open`, `snapshot` (accessibility tree), `screenshot`,
`click`, `fill`, `press`, `select`, `evaluate`, `close_tab`, `handoff` (returns the
live-viewer link so a human can clear a login, 2FA or captcha).

- **Workspaces** (`config/workspaces.yaml`): `soak` is the experiment browser, and every
  mutating call on it is logged as an `AGENT_ACTION` event, which `report.py` flags as
  contamination. `work` is a separate Chrome with its own profile, free for anything.
- **Remote:** `https://mcp.xmzr.dev/ws/mcp` (add as a claude.ai connector). The chain is
  Cloudflare Access (the mcp-hub app) → mcp-hub Caddy `/ws/*` → VPC `10.170.0.6:8090` →
  `gateway` Caddy (requires the `X-WS-Key` header from `config/gateway.env`) → mcp.
- **Viewers:** `https://mcp.xmzr.dev/ws/view/<soak|work>/vnc.html?path=ws/view/<ws>/websockify&autoconnect=1&resize=scale`
- **Over SSH** (for people not in the Access policy):
  `ssh -N -L 8090:10.170.0.6:8090 soak`, then
  `claude mcp add --transport http workspace http://localhost:8090/mcp --header "X-WS-Key: <key>"`.
  The key is in `~/workspace-hub/config/gateway.env` on the VM.
- **Deploying mcp changes:** `docker compose restart mcp` (src/ is mounted). Never
  `docker compose up` without `--no-deps` for mcp/work/gateway: `mcp` depends on
  `soak` and would rebuild or recreate it.

## Runbook

1. **Deploy/update** (from the laptop):
   `rsync -a --exclude .git --exclude data ./ soak:workspace-hub/ && ssh soak 'cd workspace-hub && docker compose up -d --build'`
2. **Tests**: `ssh soak 'cd workspace-hub && docker compose exec soak pytest -q'`
3. **T0 manual login.** In noVNC, log in by hand to AIMS, Canvas, Google, GitHub,
   LinkedIn. Accept every "remember this device" / "keep me signed in". Fill in
   `baseline.md` as you go. Then immediately:

       ssh soak 'cd workspace-hub && docker compose exec soak python src/snapshot_cookies.py "T0 all tier 1+2"'
       ssh soak 'cd workspace-hub && docker compose exec soak python src/probe.py'

   Look at the screenshots in `data/shots/`: every site should be AUTHED. If one is
   ERROR (unclassified), fix its matcher in `config/targets.yaml` now. Then start
   the clock: `crontab scripts/crontab`.
4. **Stop/start schedule** runs itself (day counted from T0):
   day 3 `docker compose down/up`, days 4–7 host poweroff at 02:30 and GCE start at 02:45.
   Every start writes CONTAINER_RESTART or HOST_REBOOT into `events` (by kernel boot id).
5. **Day 7**: `ssh soak 'cd workspace-hub && docker compose exec -T soak python src/report.py' > findings.md`,
   then write the M1 recommendation paragraph by hand.
6. **IP-change arm (day 8+, optional):** `gcloud compute instances delete-access-config soak --zone asia-east2-a`
   then `add-access-config` with no address (ephemeral IP). The probe logs IP_CHANGE by itself.
   Reattach `soak-ip` afterwards to go back.

## Teardown (after the experiment, not before)

    gcloud compute instances delete soak --zone asia-east2-a
    gcloud compute addresses delete soak-ip --region asia-east2
    gcloud compute resource-policies delete soak-daily-start --region asia-east2
