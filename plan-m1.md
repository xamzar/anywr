# Anywhere Live — build timeline (M1 → M5)

**Date:** 2026-09-19 · **Target browser: `work` only.**
`plan.md` is Track 0 (xmzr's soak experiment). Nothing in this document touches it.

---

## Scope rule — read this first

Everything here runs against the **`work`** browser. `work` has its own Chrome, its own
profile volume, its own `work-data` volume, and its own CDP port (`work:9223`). It is already
logged into AIMS.

The `soak` browser, `soak.db`, `src/probe.py`, `src/report.py`, `src/classify.py`,
`src/snapshot_cookies.py`, `config/targets.yaml`, `scripts/` and `plan.md` are **out of
scope**. Do not read from them, write to them, or import them.

Consequence for the kernel: it must **not** log `AGENT_ACTION` events and must **not** import
`db.py`. That logging exists only to mark contamination of the soak experiment, and the
kernel is not part of that experiment.

Deploy rule: `docker compose up -d --build --no-deps <service>`. A bare `docker compose up`
recreates `soak` and destroys the running experiment.

---

## M1 — Thin vertical slice

One browser, one task, no product around it. No auth, no isolation, hardcoded to one person.

**Kernel v0 is three tools**: `view()`, `click(ref)`, `fill(ref, value)`. That is the
timeline's scope and it is deliberately smaller than feels comfortable. `back()`,
`read(region)`, `session_status()` and `submit`-preview are real needs but they are not what
M1 is testing, and each one added now delays the number that M1 exists to produce.

`open(url)` is **not** a tool. Banner kills a session on detected URL break-in. The kernel
starts from whatever page the browser is already on.

`evaluate(js)` is **not** a tool. Arbitrary JavaScript in the page would make the immutable
base meaningless once M5 lands.

The existing `src/mcp_server.py` stays exactly as it is — it is the **control condition** for
the measurement. Do not refactor or delete it.

### The digest

`view()` returns addressable refs and nothing the model cannot act on.

```
url: https://banweb.cityu.edu.hk/pls/PROD/twbkwbis.P_GenMenu?name=bmenu.P_MainMnu
title: Main Menu

[1]  link    Student Services
[2]  link    Personal Information
[3]  button  Sign Out
(28 more elements — not shown)
```

- Interactive elements only: links, buttons, inputs, selects.
- Static text summarised, never dumped. No raw HTML, ever.
- Refs are 1-based, stable for one page load, invalidated on navigation.
- Hard token cap. On overflow, truncate and *say so* — never silently drop.

**Target: under 1,000 tokens on Banner's main menu.** The stock server currently allows
40,000 characters on that same page.

### Done when

Claude pulls AIMS grades through the kernel, and `findings-m1.md` records token counts for
the same task through the kernel and through stock Playwright MCP, with a screenshot.

Report the result honestly, including if the kernel loses. A 3% saving is a finding that
changes M5, not something to bury.

### Steps

1. **`src/kernel/digest.py`** — digest builder over a Playwright page, plus
   `tests/test_digest.py` against local fixture HTML. Pure logic, no CDP, no MCP.
2. **`src/kernel/base.py`** — `view` / `click` / `fill` over CDP against `work`.
3. **`src/kernel/server.py`** — FastMCP wiring. HTTP for the deployed path; a stdio entry
   point so Claude Desktop can attach over an SSH tunnel to `work:9223`.
4. **Compose service `kernel`** — own service, default network, reaches `work:9223` by name.
   Not `network_mode: service:soak`. `./src` stays `:ro`.
5. **The measurement** → `findings-m1.md`.

Steps 1–4 are the build. Step 5 is the deliverable; without it M1 has not happened.

---

## M2 — The handoff ← this is the demo

- `handoff(reason)` — unlocks the viewer, pings Telegram, **blocks until the user presses
  Done**. Today's version returns a string and blocks on nothing; that is a stub, not the
  tool.
- Viewer defaults to view-only with an "Agent working" overlay and a Done button.

**Done when:** you log out of AIMS in `work`. From your phone you ask for your grades. The
agent pings you. You complete SSO on the phone through the stream. It continues and returns
the grades.

---

## M3 — Yours, from anywhere

- Auth Worker: email OTP → signed cookie (≤24h) → proxy `/username`.
- Encrypted persistent volume; machine stops on idle, starts on request.

**Done when:** you shut your laptop, open `anywr.live/asanbl4` on your phone, enter an OTP,
and land in the same session.

---

## M4 — Multi-user

One MCP server per user, scoped to **one** browser — drop the `workspace` argument from every
tool signature. Per-user key on the gateway; no shared `X-WS-Key`. This inverts today's
topology, where one server reaches every browser by name.

**Isolation substrate is deliberately undecided.** Fly Machines give each user a real virtual
machine but move between hosts, so the egress IP changes. Hetzner + gVisor keeps a fixed IP
and lets you buy one per user, at a speed cost. Which is right depends on whether device
trust survives an IP change — which is exactly what Track 0 measures and has not yet
answered. Until then: plain Docker, one container per user, and the README says in plain
words that this is not real isolation and no second real person goes on the box.

**Done when:** two accounts exist and neither can reach the other's profile.

---

## M5 — Promotion (self-editing tools)

`record(on|off)` → `export_adapter(name)` → adapter runtime → `verify`.

The agent explores once with the kernel, the server records the path, and promotion turns it
into a typed tool that runs with no inference at all. Inference is then spent only on first
discovery and on repair after a site changes.

### Adapters are declarative

An adapter is YAML in `/data/adapters/<name>.yaml` — **data a fixed interpreter walks, never
code that runs.** That is what makes the guardrail structural instead of a boundary to
defend.

```yaml
name: aims_grades
description: Current semester grades as typed rows
steps:
  - click: "Student Services"
  - click: "Final Grades"
extract:
  kind: table
  selector: "table.datadisplaytable"
  fields: {course: 0, title: 1, grade: 3}
returns: [{course: str, title: str, grade: str}]
```

Step verbs are a **closed set**: `click`, `fill`, `select`, `read`, `back`, `wait`. There is
no verb for running code, reading a file, or making a network request, so there is nothing
for a bad adapter to reach for.

### Guardrails — base tools cannot be edited

1. **Reserved names.** `BASE_TOOL_NAMES` is frozen in code. Any adapter named after a base
   tool is refused. No shadowing of `view`, `click`, `fill`, `export_adapter`, or any other.
2. **Name validation.** `^[a-z][a-z0-9_]{2,40}$`.
3. **Path confinement.** Resolve with `os.path.realpath` and compare against the adapters
   directory. Outside, or a symlink — refused.
4. **Schema validation before write.** Unknown keys are an **error**, not ignored. Silently
   dropping a key the agent thought mattered is how a tool ends up doing something other than
   what it says.
5. **Closed verb set**, enforced at validation time, not run time.
6. **Read-only code mount.** `./src:/app/src:ro` is already in `docker-compose.yml`. Even a
   bug that escapes every check above cannot reach the base tools, because the filesystem
   refuses. This is the wall; rules 1–5 are the fence.
7. **Audit log.** Every write appends to `/data/adapters/.audit.jsonl` — timestamp, name,
   spec hash.

A file that fails validation at startup is skipped with a loud log line. One bad adapter must
never stop the server.

**Done when:** `aims_grades` returns typed rows with zero tokens spent.

---

## Testing

`pytest`, alongside the existing suite. Local fixture HTML only — **no test hits CityU.**
The single real end-to-end run is the M1 measurement.

- **Digest:** a 200-element fixture stays under the token cap; refs dense and 1-based; a
  stale ref gives a clear error, not a crash; no raw HTML in any output.
- **Guardrails (M5):** every name in `BASE_TOOL_NAMES` refused, parametrised; `../` and
  absolute paths refused; symlink escape refused; unknown key refused; `eval`/`exec`/`fetch`
  verbs refused; `src/` byte-identical after a full define/edit/delete cycle; one audit line
  per mutation.
- **Adapter runtime (M5):** valid adapter round-trips to typed rows; a broken selector fails
  naming the step rather than falling through to a wrong row.
