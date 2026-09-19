"""SQLite -> findings.md: per-site timeline, time-to-death, correlation with the
events table, and the pre-registered GREEN / AMBER / RED call (plan.md section 3).

    docker compose exec soak python src/report.py > findings.md
"""
import datetime as dt

import db
from snapshot_cookies import load_targets

DEAD = ("CHALLENGED", "LOGGED_OUT")
P = dt.datetime.fromisoformat


def hours(a, b):
    return (P(b) - P(a)).total_seconds() / 3600


def cause(con, site, t0, death_ts, state):
    if state == "CHALLENGED":
        return "device-trust challenge (cookie survived, trust did not)"
    rows = con.execute("SELECT expires_utc FROM cookie_snapshots WHERE site=? AND ts=(SELECT min(ts)"
                       " FROM cookie_snapshots WHERE site=? AND ts>=?) AND is_session=0", (site, site, t0)).fetchall()
    if not rows:
        return "unknown (no T0 cookie snapshot)"
    alive = [e for (e,) in rows if P(e) > P(death_ts)]
    return ("cookie expiry (every T0 persistent cookie had expired)" if not alive
            else f"server-side revocation ({len(alive)}/{len(rows)} T0 persistent cookies still unexpired)")


def main():
    con = db.connect()
    targets = load_targets()
    start = db.t0(con)
    if start is None:
        print("# Findings\n\nNo MANUAL_LOGIN event yet: run snapshot_cookies.py after the manual login.")
        return
    t0 = start.isoformat()
    now = db.now()
    events = con.execute("SELECT ts, kind, detail FROM events WHERE ts>=? ORDER BY ts", (t0,)).fetchall()
    runs = [r for (r,) in con.execute("SELECT DISTINCT ts FROM probes WHERE ts>=? ORDER BY ts", (t0,))]
    gaps = [hours(a, b) for a, b in zip(runs, runs[1:])]
    cycles = sum(k in ("CONTAINER_RESTART", "HOST_REBOOT") for _, k, _ in events)
    days = hours(t0, now) / 24

    out = [f"# Findings — session persistence soak\n",
           f"Generated {now}. T0 (manual login) {t0}. Elapsed {days:.1f} days, "
           f"{len(runs)} probe runs, longest gap {max(gaps, default=0):.1f} h, "
           f"{cycles} stop/start cycles. All times UTC.\n",
           "## Events\n", "| ts | kind | detail |", "|---|---|---|"]
    out += [f"| {ts} | {k} | {d} |" for ts, k, d in events] or ["| — | — | — |"]

    summary, died = [], {}
    for t in targets:
        site = t["name"]
        rows = con.execute("SELECT ts, state, egress_ip, final_url, screenshot_path, notes FROM probes"
                           " WHERE site=? AND ts>=? ORDER BY ts", (site, t0)).fetchall()
        out += [f"\n## {site} (tier {t['tier']})\n", "| ts | state | egress | final url | screenshot |", "|---|---|---|---|---|"]
        last = None
        for ts, state, ip, url, shot, notes in rows:
            if state != last or state == "ERROR":
                out.append(f"| {ts} | {state} | {ip} | {url[:70]} | {shot} {notes} |")
            last = state
        errors = sum(r[1] == "ERROR" for r in rows)
        death = next((r for r in rows if r[1] in DEAD), None)
        if death:
            ttd = hours(t0, death[0])
            prior_ok = [r[0] for r in rows if r[1] == "AUTHED" and r[0] < death[0]]
            window = [e for e in events if (prior_ok[-1] if prior_ok else t0) <= e[0] <= death[0]]
            out.append(f"\n**Died** at {death[0]} as {death[1]}, **{ttd:.1f} h** after T0. "
                       f"Cause: {cause(con, site, t0, death[0], death[1])}.")
            out.append("Events between last AUTHED probe and death: "
                       + (", ".join(f"{k} {ts}" for ts, k, _ in window) or "none (pure elapsed time)") + ".")
            died[site] = (t["tier"], ttd, [k for _, k, _ in window])
            summary.append(f"| {site} | {t['tier']} | {death[1]} | {ttd:.1f} h | {errors} |")
        else:
            state = rows[-1][1] if rows else "no data"
            summary.append(f"| {site} | {t['tier']} | alive ({state}) | — | {errors} |")
        if errors:
            out.append(f"\n{errors} ERROR probe(s): rig problems, not counted as deaths — check the screenshots.")

    t1_dead = [s for s, v in died.items() if v[0] == 1]
    t2_dead = [s for s, v in died.items() if v[0] == 2]
    if t1_dead and any(died[s][1] <= 72 for s in t1_dead):
        verdict = "RED"
    elif t1_dead:
        verdict = "UNRESOLVED — a Tier 1 site died after 72 h, which no pre-registered rule covers"
    elif t2_dead:
        verdict = "AMBER"
    else:
        verdict = "GREEN"
    provisional = days < 7 or cycles < 2
    restart_deaths = [s for s, v in died.items() if {"CONTAINER_RESTART", "HOST_REBOOT"} & set(v[2])]

    out += ["\n## Summary\n", "| site | tier | outcome | time to death | errors |", "|---|---|---|---|---|", *summary,
            f"\n## Verdict: **{verdict}**" + (" (PROVISIONAL: needs ≥7 days and ≥2 stop/start cycles)" if provisional else ""),
            ""]
    if restart_deaths:
        out.append(f"Restart sub-verdict: {', '.join(restart_deaths)} died across a stop/start. Investigate the "
                   "graceful-shutdown path (cookie flush before kill) before accepting a RED.")
    if verdict == "RED":
        out.append("RED only stands if pinning the egress IP does not fix it. This rig already runs on a pinned "
                   "static IP, so a Tier 1 death here does count.")
    out.append("\n_Recommendation for M1: (write by hand after reading the above)._")
    print("\n".join(out))


if __name__ == "__main__":
    main()
