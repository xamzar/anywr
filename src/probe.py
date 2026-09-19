"""The 6-hourly probe. Attaches to the running Chrome over CDP -- it never
launches a browser -- and opens one tab per target. Strictly read-only:
navigate, look, screenshot, read cookies, close the tab. tests/test_readonly.py
enforces that no mutating Playwright call appears in this file.
"""
import os
import re
import urllib.request

from playwright.sync_api import sync_playwright

import db
from classify import classify, selectors
from snapshot_cookies import CDP, load_targets, snapshot

SHOTS = "/data/shots"
# Backstop: a main-frame navigation whose path/query carries an action verb is aborted.
ACTION = re.compile(r"log-?out|sign-?out|delete|submit|transfer|(?<![a-z])pay(?![a-z])", re.I)


def egress_ip():
    try:
        return urllib.request.urlopen("https://ifconfig.me/ip", timeout=15).read().decode().strip()
    except Exception as e:  # noqa: BLE001 - recorded, not fatal
        return f"unknown ({e.__class__.__name__})"


def _risky(url):
    return bool(ACTION.search(url.split("://", 1)[-1].partition("/")[2]))


# Browser-side prefilter (patterns are case-sensitive); _risky() decides.
# Not page.route: it intercepts every request even with a predicate, and that
# breaks Google's redirects over CDP.
PATTERNS = sorted({f"*{w}*" for v in ("logout", "log-out", "signout", "sign-out", "delete", "submit", "transfer", "pay")
                   for w in (v, v.capitalize(), v.upper())} | {"*LogOut*", "*SignOut*"})


def _guard(ctx, page, blocked):
    cdp = ctx.new_cdp_session(page)

    def paused(ev):
        if _risky(ev["request"]["url"]):
            blocked.append(ev["request"]["url"])
            cdp.send("Fetch.failRequest", {"requestId": ev["requestId"], "errorReason": "BlockedByClient"})
        else:
            cdp.send("Fetch.continueRequest", {"requestId": ev["requestId"]})
    cdp.on("Fetch.requestPaused", paused)
    cdp.send("Fetch.enable", {"patterns": [{"urlPattern": p, "resourceType": "Document"} for p in PATTERNS]})


def probe_once(ctx, t, ts):
    blocked = []
    page = ctx.new_page()
    try:
        _guard(ctx, page, blocked)
        page.goto(t["authed_url"], wait_until="domcontentloaded", timeout=45_000)
        try:   # SSO chains keep redirecting after "load"; Gmail-style apps never idle
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(3_000)
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
        url, title = page.url, page.title()
        text = page.inner_text("body", timeout=10_000)
        present = {s for s in selectors(t) if page.query_selector(s)}
        state = classify(t, url, text, present)
        shot = f"{SHOTS}/{t['name']}/{ts.replace(':', '').replace('+0000', 'Z')}.png"
        os.makedirs(os.path.dirname(shot), exist_ok=True)
        page.screenshot(path=shot, full_page=True, timeout=30_000)
        notes = "unclassified: no signal matched" if state == "ERROR" else ""
        if blocked:
            notes += f" blocked navigation: {blocked}"
        return state, url, title, shot, notes.strip()
    finally:
        page.close()


def main():
    con = db.connect()
    targets = load_targets()
    ip = egress_ip()
    prev = con.execute("SELECT egress_ip FROM probes ORDER BY id DESC LIMIT 1").fetchone()
    if prev and prev[0] != ip and not ip.startswith("unknown") and not prev[0].startswith("unknown"):
        db.event(con, "IP_CHANGE", f"{prev[0]} -> {ip}")
    ts = db.now()
    with sync_playwright() as p:
        ctx = p.chromium.connect_over_cdp(CDP).contexts[0]
        for t in targets:
            for attempt in (1, 2):   # an ERROR is retried once, then recorded as-is
                try:
                    state, url, title, shot, notes = probe_once(ctx, t, ts)
                except Exception as e:  # noqa: BLE001 - timeouts/DNS/5xx are rig errors
                    state, url, title, shot, notes = "ERROR", "", "", "", f"{e.__class__.__name__}: {str(e)[:300]}"
                if state != "ERROR":
                    break
            if attempt == 2:
                notes = f"(retried) {notes}"
            con.execute("INSERT INTO probes(ts, site, state, egress_ip, final_url, page_title, screenshot_path, notes)"
                        " VALUES (?,?,?,?,?,?,?,?)", (ts, t["name"], state, ip, url, title, shot, notes))
            con.commit()
            print(f"{ts} {t['name']:10} {state:11} {url[:90]}")
        snapshot(con, ctx, targets, ts)


if __name__ == "__main__":
    main()
