"""Daily digest: one line per site, latest state, days since login. Telegram if
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set, otherwise stdout."""
import datetime as dt
import json
import os
import urllib.request

import db
from snapshot_cookies import load_targets


def digest(con):
    start = db.t0(con)
    since = "no manual login yet" if start is None else \
        f"day {(dt.datetime.now(dt.timezone.utc) - start).days} since login"
    lines = [f"soak: {since}"]
    for t in load_targets():
        row = con.execute("SELECT ts, state FROM probes WHERE site=? ORDER BY ts DESC LIMIT 1", (t["name"],)).fetchone()
        lines.append(f"{t['name']}: {row[1]} ({row[0][:16]})" if row else f"{t['name']}: no probe yet")
    return "\n".join(lines)


if __name__ == "__main__":
    text = digest(db.connect())
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     json.dumps({"chat_id": chat, "text": text}).encode(),
                                     {"content-type": "application/json"})
        urllib.request.urlopen(req, timeout=20)
    print(text)
