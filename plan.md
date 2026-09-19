# Track 0 — Session Persistence Soak Test

Build this first. Nothing else in the project matters until this returns a result.

> **Deviations in this rig (2026-09-19)**
> - GCP `asia-east2-a` (Hong Kong) e2-medium with a reserved static IP instead of Hetzner CX22. Same control condition (static egress), same region as the existing AIMS automation.
> - Google Chrome stable instead of Chromium (closer to what a real user runs; Google sign-in fingerprints the build).
> - CityU AIMS signs in through **Okta** (auth.cityu.edu.hk), not Shibboleth + Duo. Banner's own session idles out after about 30 minutes, so the AIMS probe measures the Okta session.
> - SSH is key-only and open to 0.0.0.0/0 under the project-wide `allow-ssh-direct` rule, not locked to one source IP. The laptop's IP changes between networks.
> - Host power cycles are automated: `cycle.sh` powers the VM off, and the GCE instance schedule powers it back on.

## 1. Context

We are building an agentic browser workspace. Every user gets a Chromium instance that lives in the cloud, stays logged in to everything, is reachable from any device at short.domain/username, and can be driven by an AI agent on their behalf. When the agent hits something only a human can clear (first login, 2FA, a captcha), it hands control back through a live stream. The user clicks through it, and the agent resumes.

The whole product rests on one unproven assumption:

**A browser profile parked on a datacenter IP stays authenticated to real websites for days or weeks at a time, including across machine stop/start cycles.**

If that is false, there is no product.

## 2. What this experiment measures

For each target site, over 7+ days:

- Does the session survive elapsed time?
- Does it survive a machine stop/start cycle?
- Does it survive an egress IP change?
- When does it die, and why: cookie expiry, server-side revocation, or a device-trust challenge?

The output is a dated table of observations and a single GREEN / AMBER / RED verdict.

## 3. Pre-registered decision rules

Evaluated at day 7, after at least two stop/start cycles:

| Verdict | Condition | What we do |
|---|---|---|
| GREEN | Tier 1 and Tier 2 sites all still authenticated | Premise holds. Proceed to M1 (digest kernel). Stop-on-idle is viable. |
| AMBER | Tier 1 (university portals) survive, Tier 2 (Google, GitHub) partially fail | Product works, but the pitch narrows to education/portal automation. Proceed to M1; drop the "logged into everything" framing. |
| RED | Any Tier 1 site dies inside 72h and pinning the egress IP does not fix it | The persistent-profile thesis fails. Do not build M3/M4 as designed. Pivot to a present-only model. |

Sub-verdict on restarts: if sessions survive elapsed time but not stop/start, the fix is a graceful shutdown path (flush cookies to disk before SIGKILL). Investigate that before calling it RED.

## 4. Out of scope

The MCP digest kernel and any agent integration; the auth Worker, email OTP, or /username routing; multi-user provisioning or micro-VM isolation; any write action against any site; any web UI beyond the bare VNC viewer.

## 5. Architecture

A VM with a static IPv4 runs the Docker container "soak". Inside it: Xvfb :99; ONE long-running headful browser (`--user-data-dir=/profile`, `--remote-debugging-port=9222`); x11vnc → websockify → noVNC; and probe.py, run by host cron every 6 hours. The probe attaches over CDP (`connect_over_cdp`) and never launches its own browser.

## 6. Manual setup

- 6.4 One-time manual login (T0): log in by hand over noVNC and accept every "remember this device" prompt. Record the 2FA details in baseline.md. Run snapshot_cookies.py right after. No password is ever typed into a script, stored in a file, or committed.
- 6.5 IP-change arm (day 8+): repeat the last two days with a changed egress IP.
- 6.6 Notifications: the life-tg Telegram bot, or stdout.

## 7. Target sites

- **Tier 1:** CityU AIMS, Canvas.
- **Tier 2:** Google, GitHub, LinkedIn.
- **Tier 3 (optional; not before day 7):** a low-stakes bank or brokerage account.

## 8. Probe behaviour

Every 6 hours: record the egress IP once. Then, per target in a fresh tab: goto authed_url; wait for network idle (with a hard timeout); classify; capture the final URL, title, full-page screenshot and cookie expiries; append a row; close the tab. Never click, submit or fill.

The states, in priority order, are AUTHED > CHALLENGED > LOGGED_OUT. ERROR (timeout, DNS, 5xx, unclassified) is retried once, then recorded, and never counted as a death.

The probe is read-only by construction: tests/test_readonly.py greps for mutating calls, and a navigation backstop aborts action-verb URLs.

## 9. Run schedule

| Day | Machine state |
|---|---|
| 0 | Manual login, cookie snapshot |
| 1–3 | Continuous |
| 3 | docker compose down / up |
| 4 | Full VM power-off / power-on |
| 5–7 | One stop/start per day |
| 8+ | Optional IP-change arm |

Before every deliberate stop, the browser gets SIGTERM and the rig waits for it to exit.

## 10. Data schema

See src/db.py: `probes`, `cookie_snapshots`, `events` in /data/soak.db. Screenshots are saved to /data/shots/<site>/<ts>.png. All times are UTC.

## 11. Deliverables

report.py → findings.md, baseline.md, and a daily Telegram digest.

## 14. Definition of done

- The server is provisioned and reachable only over SSH.
- One long-lived browser runs with a persistent profile.
- The Tier 1 and Tier 2 manual logins are done and baseline.md is written.
- The T0 snapshot has been captured.
- The probe has run for 7 days with no gap longer than one cycle.
- The events table records at least one container restart and one host reboot.
- findings.md states GREEN, AMBER or RED.
- There are zero credentials in the repo.

## 15. What happens next

- GREEN → M1 (digest kernel over CDP).
- AMBER → M1, with the product framing narrowed to portals.
- RED → stop and redesign.
