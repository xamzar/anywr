"""SQLite at /data/soak.db. CLI: `boot` | `day` | `event KIND DETAIL`."""
import datetime as dt
import os
import sqlite3
import sys

DB = os.environ.get("SOAK_DB", "/data/soak.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS probes(id INTEGER PRIMARY KEY, ts TEXT, site TEXT, state TEXT,
  egress_ip TEXT, final_url TEXT, page_title TEXT, screenshot_path TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS cookie_snapshots(id INTEGER PRIMARY KEY, ts TEXT, site TEXT,
  cookie_name TEXT, domain TEXT, expires_utc TEXT, is_session INTEGER);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, detail TEXT);
"""
EVENT_KINDS = ("MANUAL_LOGIN", "CONTAINER_RESTART", "HOST_REBOOT", "IP_CHANGE")


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def connect(path=DB):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def event(con, kind, detail=""):
    assert kind in EVENT_KINDS, kind
    con.execute("INSERT INTO events(ts, kind, detail) VALUES (?,?,?)", (now(), kind, detail))
    con.commit()


def t0(con):
    row = con.execute("SELECT ts FROM events WHERE kind='MANUAL_LOGIN' ORDER BY ts DESC LIMIT 1").fetchone()
    return row and dt.datetime.fromisoformat(row[0])


def boot(con):
    """Called by the entrypoint: a new kernel boot_id means the host rebooted."""
    boot_id = open("/proc/sys/kernel/random/boot_id").read().strip()
    marker = os.path.join(os.path.dirname(DB), ".boot_id")
    last = open(marker).read().strip() if os.path.exists(marker) else None
    if last is not None:
        event(con, "HOST_REBOOT" if last != boot_id else "CONTAINER_RESTART", f"boot_id {boot_id}")
    open(marker, "w").write(boot_id)


if __name__ == "__main__":
    con = connect()
    cmd = sys.argv[1]
    if cmd == "boot":
        boot(con)
    elif cmd == "day":
        start = t0(con)
        print(-1 if start is None else (dt.datetime.now(dt.timezone.utc) - start).days)
    elif cmd == "event":
        event(con, sys.argv[2], " ".join(sys.argv[3:]))
