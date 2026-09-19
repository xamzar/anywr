"""Record every target cookie's expiry. Run once right after the manual login:

    docker compose exec soak python src/snapshot_cookies.py "logged in to all Tier 1+2"

That also writes the MANUAL_LOGIN event that starts the clock (T0). The probe
calls snapshot() on every run so expiries can be compared over time.
"""
import datetime as dt
import sys

import yaml
from playwright.sync_api import sync_playwright

import db

CDP = "http://127.0.0.1:9222"


def load_targets(path="/app/config/targets.yaml"):
    return yaml.safe_load(open(path))


def _mine(cookie_domain, domains):
    d = cookie_domain.lstrip(".")
    return any(d == x or d.endswith("." + x) for x in domains)


def snapshot(con, context, targets, ts):
    cookies = context.cookies()
    for t in targets:
        for c in cookies:
            if not _mine(c["domain"], t["cookie_domains"]):
                continue
            session = c["expires"] in (-1, 0, None)
            exp = None if session else dt.datetime.fromtimestamp(c["expires"], dt.timezone.utc).isoformat()
            con.execute("INSERT INTO cookie_snapshots(ts, site, cookie_name, domain, expires_utc, is_session)"
                        " VALUES (?,?,?,?,?,?)", (ts, t["name"], c["name"], c["domain"], exp, int(session)))
    con.commit()


if __name__ == "__main__":
    con = db.connect()
    targets = load_targets()
    with sync_playwright() as p:
        ctx = p.chromium.connect_over_cdp(CDP).contexts[0]
        ts = db.now()
        snapshot(con, ctx, targets, ts)
    db.event(con, "MANUAL_LOGIN", " ".join(sys.argv[1:]) or "T0")
    for site, n, s in con.execute("SELECT site, count(*), sum(is_session) FROM cookie_snapshots"
                                  " WHERE ts=? GROUP BY site", (ts,)):
        print(f"{site:10} {n:3} cookies ({s} session-only)")
