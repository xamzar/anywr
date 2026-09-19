"""Self-check: capped sign-up through the Access ticket, username pages,
agent links, and that the MCP dispatcher pins the user from the token (ignoring
a spoofed header). No Docker or Cloudflare needed: ensure_running is stubbed to
report which user it was asked for, and tickets are signed here the way
auth/index.js signs them.

    DB=/tmp/anywr-test.db python test_app.py
"""
import base64
import logging
import hmac
import json
import os
import time

os.environ.setdefault("DB", "/tmp/anywr-test.db")
os.environ["MAX_USERS"] = "2"
os.environ["SSO_SECRET"] = "test-secret"
if os.path.exists(os.environ["DB"]):
    os.remove(os.environ["DB"])

from starlette.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402


async def fake_ensure(uid):
    raise RuntimeError(f"ensure_running(u{uid})")


async def fake_state(uid):
    return "none"


async def fake_destroy(uid):
    pass


prefilled = []


async def fake_prefill(st, email):  # the real one talks to Cloudflare; None = plain Access page
    return prefilled.pop() if prefilled else None


A.prefill = fake_prefill
A.ensure_running = fake_ensure
A.state = fake_state
A.destroy = fake_destroy


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def ticket(email, state, secret=b"test-secret", ttl=120):
    body = b64(json.dumps({"e": email, "s": state, "x": int(time.time()) + ttl}).encode())
    return f"{body}.{b64(hmac.digest(secret, body.encode(), 'sha256'))}"


def sign_in(c, email, **start):
    """What the browser does: start, get bounced through Access, come back with a ticket."""
    r = c.post("/api/login", json=start)
    if r.status_code != 200:
        return r
    st = r.json()["url"].split("state=")[1]
    return c.get("/auth/callback", params={"t": ticket(email, st)}, follow_redirects=False)


def mcp_call(c, url, tool, spoof=None):
    hdr = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if spoof:
        hdr["x-anywr-user"] = spoof
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": {}}}
    r = c.post(url, headers=hdr, json=body)
    return r.status_code, r.text


def new_client():
    return TestClient(A.app, base_url="https://testserver")


with new_client() as c:
    assert c.get("/").status_code == 200 and "anywr" in c.get("/").text
    assert c.post("/api/login", json={"username": "admin", "signup": True}).status_code == 400  # reserved
    r = sign_in(c, "Admin@Example.com", username="Boss", signup=True)
    assert r.status_code == 303 and r.headers["location"] == "/boss", r.text
    me = c.get("/api/session").json()["user"]
    assert me["username"] == "boss" and me["email"] == "admin@example.com"
    assert c.get("/boss").status_code == 200
    assert c.get("/api/user/boss").json()["exists"] and not c.get("/api/user/nobody").json()["exists"]

    c2 = new_client()
    r = sign_in(c2, "admin@example.com", username="bee", signup=True)                      # email taken
    assert r.status_code == 400 and "/cdn-cgi/access/logout" in r.text
    assert sign_in(c2, "b@x.io", username="boss", signup=True).status_code == 400            # name taken
    r = sign_in(c2, "b@x.io", username="bee", signup=True)
    assert r.status_code == 303 and r.headers["location"] == "/bee"
    b_id = c2.get("/api/session").json()["user"]["id"]
    admin_id = me["id"]
    assert new_client().post("/api/login", json={"username": "cee", "signup": True}).status_code == 403  # full

    # log in to an existing page: only with that page's email
    c3 = new_client()
    assert sign_in(c3, "b@x.io", username="boss").status_code == 403
    assert sign_in(c3, "admin@example.com", username="boss").status_code == 303
    assert c3.post("/api/login", json={"username": "ghost"}).status_code == 404
    prefilled.append("https://auth/start?b=x")  # owner's email already submitted: straight to the code box
    assert c3.post("/api/login", json={"username": "boss"}).json()["url"] == "https://auth/start?b=x"
    assert c3.post("/api/login", json={"username": "boss"}).status_code == 429   # one email a minute

    # tickets: forged, expired, replayed, or from another browser are all refused
    st = c3.post("/api/login", json={"username": "bee"}).json()["url"].split("state=")[1]
    assert c3.get("/auth/callback", params={"t": ticket("b@x.io", st, secret=b"wrong")}).status_code == 400
    assert c3.get("/auth/callback", params={"t": ticket("b@x.io", st, ttl=-1)}).status_code == 400
    assert new_client().get("/auth/callback", params={"t": ticket("b@x.io", st)}).status_code == 400
    t = ticket("b@x.io", st)
    assert c3.get("/auth/callback", params={"t": t}, follow_redirects=False).status_code == 303
    c3.cookies.set("awst", st, domain="testserver", path="/auth")
    assert c3.get("/auth/callback", params={"t": t}, follow_redirects=False).status_code == 400

    # MCP runs as the token's owner, even when the client claims to be someone else.
    url = c2.post("/api/agent").json()["url"]
    path = url[len(A.BASE):]
    st_, text = mcp_call(c2, path, "tabs", spoof=str(admin_id))
    assert st_ == 200 and f"ensure_running(u{b_id})" in text, (st_, text)
    assert mcp_call(c2, "/mcp/" + "A" * 43, "tabs")[0] == 401
    assert mcp_call(c2, "/mcp/.well-known", "tabs")[0] == 404
    url2 = c2.post("/api/agent").json()["url"]
    assert mcp_call(c2, path, "tabs")[0] == 401           # rotated
    c2.delete("/api/agent")
    assert mcp_call(c2, url2[len(A.BASE):], "tabs")[0] == 401  # revoked

    # a deleted user's id is never handed out again, so nothing of theirs is inherited
    c2.post("/api/agent")
    assert c2.post("/api/account/delete", json={"username": "bee"}).status_code == 200
    c4 = new_client()
    assert sign_in(c4, "new@x.io", username="newbie", signup=True).status_code == 303
    s4 = c4.get("/api/session").json()
    assert s4["user"]["id"] > b_id and s4["agent"] is None, s4
    assert A.one("SELECT COUNT(*) n FROM agent_tokens WHERE user_id=?", (b_id,))["n"] == 0

    c2.post("/api/logout")
    assert c2.get("/api/session").json()["user"] is None
    assert c2.post("/api/browser/start").status_code == 401

rec = logging.LogRecord("uvicorn.access", 20, "", 0, '%s - "%s %s HTTP/%s" %d',
                        ("1.2.3.4:0", "POST", "/mcp/" + "A" * 43, "1.1", 200), None)
A.RedactTokens().filter(rec)
assert "A" * 43 not in rec.getMessage() and "/mcp/<token>" in rec.getMessage()

print("ok")
