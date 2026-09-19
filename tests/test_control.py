"""The control server: the page, and the two endpoints the page talks to.

Driven in-process through Starlette's test client, so nothing here opens a
socket to anywhere. The reason under test throughout is a hostile one, because
a reason is model-authored text that ends up rendered in a human's browser and
the digest's rule about page text applies to it in the other direction.
"""
import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from kernel import control, handoff  # noqa: E402

NASTY = '<img src=x onerror="alert(1)"> <script>steal()</script>'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNEL_STATE_FILE", str(tmp_path / "state" / "handoff.json"))
    monkeypatch.delenv("KERNEL_NOVNC_URL", raising=False)
    with TestClient(control.app) as c:
        yield c


# --- the page ---------------------------------------------------------------

def test_the_viewer_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Agent working" in r.text and 'id="done"' in r.text


def test_the_viewer_defaults_to_view_only(client):
    r = client.get("/")
    assert "view_only=1" in r.text
    # In the served markup, not added by script: if the JS never runs, the
    # stream is still watch-only rather than a live browser on a public page.
    assert 'src="/view/work/vnc.html?autoconnect=1&amp;resize=scale&amp;view_only=1"' in r.text


def test_the_page_is_view_only_even_while_a_handoff_is_pending(client):
    handoff.start("sign in")
    assert "view_only=1" in client.get("/").text   # the script drops it, the server never does


def test_the_novnc_url_is_configurable_and_escaped(monkeypatch):
    page = control.page('https://host/v/vnc.html?tok="x')
    assert "https://host/v/vnc.html?tok=" in page
    assert '?tok="x' not in page              # cannot close the src attribute
    assert "&amp;autoconnect=1" in page       # already had a query, so it appends


def test_the_novnc_url_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("KERNEL_NOVNC_URL", "http://work:6080/vnc.html")
    assert "http://work:6080/vnc.html" in control.page()


def test_a_reason_never_reaches_the_page_as_markup(client):
    """The strongest form of this: the reason is not in the served HTML at all.
    It arrives as JSON and is written with textContent, so there is no context
    for it to escape from."""
    handoff.start(NASTY)
    body = client.get("/").text
    assert "onerror" not in body and "steal()" not in body


def test_the_page_never_writes_untrusted_text_as_html(client):
    """Named for the failure it prevents: one innerHTML here and the reason
    becomes script in the browser of the person the agent is asking for help."""
    assert "innerHTML" not in control.PAGE
    assert "outerHTML" not in control.PAGE and "document.write" not in control.PAGE
    assert "textContent" in control.PAGE


def test_every_element_the_script_hides_can_actually_be_hidden(client):
    """A CSS display rule beats the browser's own [hidden], so each one has to
    be restated. Missed for #overlay first time round, which left 'Agent
    working…' covering the stream the person had just been asked to click in."""
    for el in ("#overlay", "#ask"):
        assert f"{el}[hidden] {{ display: none; }}" in control.PAGE, el


def test_the_page_is_self_contained(client):
    """No build step and no CDN: this is served from a container behind a
    tunnel, and a page that needs the public internet to render is a page that
    does not render when the thing you are fixing is the network."""
    body = client.get("/").text
    assert "<style>" in body and "<script>" in body
    for remote in ("http://cdn", "https://cdn", "unpkg", "jsdelivr", "googleapis"):
        assert remote not in body


# --- read -------------------------------------------------------------------

def test_the_state_endpoint_reports_nothing_pending_with_no_file(client):
    got = client.get("/handoff").json()
    assert got["pending"] is False and got["id"] == ""


def test_the_state_endpoint_reports_a_pending_handoff(client):
    h = handoff.start("sign in to AIMS")
    got = client.get("/handoff").json()
    assert got["pending"] is True and got["id"] == h.id
    assert got["reason"] == "sign in to AIMS" and got["since"]


def test_a_hostile_reason_comes_back_as_data(client):
    handoff.start(NASTY)
    r = client.get("/handoff")
    assert "<" not in r.text and ">" not in r.text   # defused before it was stored
    assert isinstance(r.json()["reason"], str)


def test_the_state_endpoint_is_never_cached(client):
    """A proxy holding this for a second is a person watching an overlay that
    will not lift."""
    assert client.get("/handoff").headers["cache-control"] == "no-store"


def test_a_corrupt_state_file_is_served_as_nothing_pending(client, tmp_path):
    p = tmp_path / "state" / "handoff.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"id": "a", "pend')
    assert client.get("/handoff").status_code == 200
    assert client.get("/handoff").json()["pending"] is False


# --- done -------------------------------------------------------------------

def test_done_clears_the_pending_handoff(client):
    h = handoff.start("sign in")
    got = client.post("/handoff/done", json={"id": h.id}).json()
    assert got["ok"] and got["cleared"] and got["pending"] is False
    assert handoff.read().resolved == "done"


def test_done_is_idempotent(client):
    h = handoff.start("sign in")
    first = client.post("/handoff/done", json={"id": h.id})
    second = client.post("/handoff/done", json={"id": h.id})
    assert first.status_code == second.status_code == 200
    assert second.json()["ok"] is True and second.json()["cleared"] is False


def test_done_with_no_body_at_all_still_works(client):
    handoff.start("sign in")
    r = client.post("/handoff/done")
    assert r.status_code == 200 and r.json()["cleared"] is True


def test_done_with_a_junk_body_is_not_an_error(client):
    handoff.start("sign in")
    r = client.post("/handoff/done", content=b"not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 200 and r.json()["cleared"] is True


def test_done_from_a_stale_tab_cannot_clear_a_newer_handoff(client):
    old = handoff.start("the first one")
    client.post("/handoff/done", json={"id": old.id})
    new = handoff.start("the second one")
    got = client.post("/handoff/done", json={"id": old.id}).json()
    assert got["cleared"] is False and got["pending"] is True and got["id"] == new.id


def test_done_when_nothing_is_pending_is_not_an_error(client):
    r = client.post("/handoff/done", json={"id": "whatever"})
    assert r.status_code == 200 and r.json() == {**r.json(), "ok": True, "cleared": False}


def test_done_is_not_a_get(client):
    assert client.get("/handoff/done").status_code == 405


def test_healthz(client):
    assert client.get("/healthz").text == "ok"
