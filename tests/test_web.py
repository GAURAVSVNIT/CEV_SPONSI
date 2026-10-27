"""web.py: the browser UI is one more way to tap a button, never a way around
the gate. Offline throughout: the fake model, the console notifier, frozen
sources, and an HTTP server on an ephemeral loopback port."""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from sponsy import store, web

from .conftest import SOURCES


@pytest.fixture
def app(cfg, event, voice):
    return web.WebApp(cfg, event, voice, limit=5, frozen={"P01": SOURCES})


@pytest.fixture
def queued(run_job, prospect, db):
    """One draft for P01 waiting for a verdict."""
    run_job()
    rows = store.pending_drafts(db)
    assert len(rows) == 1
    return rows[0]["id"]


def post(app, path, body=None):
    return app.handle("POST", path, body or {})


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

def test_the_queue_shows_what_the_card_shows(app, queued):
    status, out = app.handle("GET", "/api/drafts")
    assert status == 200
    [draft] = out["drafts"]
    assert draft["id"] == queued and draft["company"] == "Acme Analytics"
    assert draft["words"] > 0 and draft["sources"] >= 1
    assert draft["grounded"] <= len(draft["claims"])
    assert isinstance(draft["failed"], list) and "checks" not in draft


def test_overview_and_prospect_detail(app, queued):
    status, out = app.handle("GET", "/api/overview")
    assert status == 200
    assert out["stats"]["pending"] == 1 and out["event"]["name"] == "Example Hackathon"
    assert out["channels"]["telegram"] == "off"          # offline never posts
    status, out = app.handle("GET", "/api/prospects/P01")
    assert status == 200
    assert out["prospect"]["status"] == "queued"
    assert out["drafts"][0]["id"] == queued and out["sources"]
    assert app.handle("GET", "/api/prospects/NOPE")[0] == 404


def test_unknown_endpoint_is_a_json_404(app):
    status, out = app.handle("GET", "/api/nothing")
    assert status == 404 and "error" in out


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------

def test_send_goes_through_the_same_gate(app, queued, db):
    status, out = post(app, "/api/drafts/{}/send".format(queued))
    assert status == 200, out
    assert out["draft"]["sent_at"] and out["draft"]["verdict"] == "approved"
    assert store.get(db, "P01").touches == 1
    # Sending twice is refused by the handler, and by the trigger behind it.
    status, out = post(app, "/api/drafts/{}/send".format(queued))
    assert out["message"] == "already sent"
    assert store.get(db, "P01").touches == 1


def test_edit_keeps_the_correction_and_sends_your_version(app, queued, db):
    status, out = post(app, "/api/drafts/{}/edit".format(queued),
                       {"subject": "  A shorter\nsubject ", "body": "Hi Devi,\n\nShort one."})
    assert status == 200, out
    row = store.get_draft(db, queued)
    assert row["verdict"] == "edited" and row["sent_at"]
    assert row["subject"] == "A shorter subject" and row["body"] == "Hi Devi,\n\nShort one."
    correction = db.one("SELECT * FROM corrections WHERE draft_id=?", (queued,))
    assert correction["after_subject"] == "A shorter subject"


def test_an_empty_edit_changes_nothing(app, queued, db):
    status, out = post(app, "/api/drafts/{}/edit".format(queued), {"body": "   "})
    assert status == 400
    row = store.get_draft(db, queued)
    assert row["verdict"] is None and not row["sent_at"]
    assert db.one("SELECT 1 FROM corrections") is None


def test_redo_retires_the_card_and_queues_a_new_one(app, queued, db):
    assert post(app, "/api/drafts/{}/redo".format(queued))[0] == 400   # no instruction
    status, out = post(app, "/api/drafts/{}/redo".format(queued),
                       {"instruction": "shorter"})
    assert status == 200, out
    assert store.get_draft(db, queued)["verdict"] == "superseded"
    [fresh] = store.pending_drafts(db)
    assert fresh["supersedes"] == queued


def test_skip_and_unknown_verdicts(app, queued, db):
    assert post(app, "/api/drafts/{}/explode".format(queued))[0] == 400
    assert post(app, "/api/drafts/999/skip")[0] == 404
    status, out = post(app, "/api/drafts/{}/skip".format(queued))
    assert status == 200 and out["draft"]["verdict"] == "skipped"
    assert store.get(db, "P01").next_touch


def test_verdicts_wait_while_the_agent_is_busy(app, queued, db):
    app.busy.acquire()
    try:
        status, out = post(app, "/api/drafts/{}/send".format(queued))
    finally:
        app.busy.release()
    assert status == 409, out
    assert not store.get_draft(db, queued)["sent_at"]


# ---------------------------------------------------------------------------
# prospects
# ---------------------------------------------------------------------------

def test_add_set_email_and_never(app, db):
    assert post(app, "/api/prospects", {"company": ""})[0] == 400
    assert post(app, "/api/prospects", {"company": "Initech", "email": "nope"})[0] == 400
    status, out = post(app, "/api/prospects", {"company": "Initech", "segment": "saas"})
    assert status == 200
    pid = out["id"]
    assert "already" in post(app, "/api/prospects", {"company": "Initech Inc"})[1]["message"]

    status, out = post(app, "/api/prospects/{}/email".format(pid),
                       {"email": "DevRel@Initech.example", "contact_name": "Peter"})
    assert status == 200
    assert store.get(db, pid).email == "devrel@initech.example"
    # The unique index on addresses comes back as a 409, not a 500.
    other = post(app, "/api/prospects", {"company": "Globex"})[1]["id"]
    assert post(app, "/api/prospects/{}/email".format(other),
                {"email": "devrel@initech.example"})[0] == 409

    status, out = post(app, "/api/prospects/{}/never".format(pid))
    assert status == 200
    assert store.get(db, pid).status == "declined"
    assert store.is_blocked(db, "devrel@initech.example")


def test_bulk_add_skips_companies_already_listed(app, prospect, db):
    status, out = post(app, "/api/prospects/bulk", {"leads": [
        {"company": "Acme Analytics"}, {"company": "Hooli", "website": "https://hooli.example"}]})
    assert status == 200
    assert [a["company"] for a in out["added"]] == ["Hooli"]
    assert store.find_by_company(db, "Hooli").source == "discover"


def test_export_is_a_csv(app, prospect):
    status, out = app.handle("GET", "/api/export.csv")
    assert status == 200 and isinstance(out, web.Raw)
    assert out.body.decode("utf-8").splitlines()[0].startswith("prospect_id,company")


# ---------------------------------------------------------------------------
# agent tasks
# ---------------------------------------------------------------------------

def _wait(app):
    assert app.busy.acquire(timeout=30), "task did not finish"
    app.busy.release()


def test_a_run_from_the_page_drafts_and_never_sends(app, prospect, db):
    status, out = post(app, "/api/tasks/run", {"limit": 2})
    assert status == 200 and out["task"]["state"] == "running"
    _wait(app)
    task = app.handle("GET", "/api/task")[1]["task"]
    assert task["state"] == "done", task["error"]
    assert task["result"]["drafts"] == 1
    assert db.one("SELECT COUNT(*) n FROM drafts WHERE sent_at IS NOT NULL")["n"] == 0


def test_one_task_at_a_time(app):
    assert post(app, "/api/tasks/explode")[0] == 404
    app.busy.acquire()
    try:
        assert post(app, "/api/tasks/doctor")[0] == 409
    finally:
        app.busy.release()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

@pytest.fixture
def server(app):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_port
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, res.getheader("Content-Type", ""), data


JSON = {"Content-Type": "application/json"}


def test_static_page_and_headers(server):
    status, ctype, data = request(server, "GET", "/")
    assert status == 200 and ctype.startswith("text/html") and b"app.js" in data
    assert request(server, "GET", "/app.js")[1].startswith("text/javascript")
    assert request(server, "GET", "/../web.py")[0] == 404
    assert request(server, "GET", "/%2e%2e/web.py")[0] == 404


def test_requests_a_browser_did_not_mean_are_refused(server, prospect):
    # DNS rebinding: a loopback server answers loopback names only.
    assert request(server, "GET", "/api/overview",
                   headers={"Host": "evil.example"})[0] == 400
    # A plain cross-site form cannot send JSON...
    assert request(server, "POST", "/api/prospects/P01/never", body="a=b",
                   headers={"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
    # ...and a page that tries harder is stopped by its Origin.
    assert request(server, "POST", "/api/prospects/P01/never", body="{}",
                   headers=dict(JSON, Origin="http://evil.example"))[0] == 403
    status, _, data = request(server, "POST", "/api/prospects", body="[1]", headers=JSON)
    assert status == 400


def test_a_same_origin_post_works(server, prospect, db):
    host = "127.0.0.1:{}".format(server)
    status, _, data = request(server, "POST", "/api/prospects",
                              body=json.dumps({"company": "Hooli"}),
                              headers=dict(JSON, Host=host, Origin="http://" + host))
    assert status == 200, data
    assert store.find_by_company(db, "Hooli") is not None


def test_a_token_guards_the_api(app, server):
    app.token = "s3cret"
    assert request(server, "GET", "/api/overview")[0] == 401
    assert request(server, "GET", "/api/overview",
                   headers={"X-Sponsy-Token": "wrong"})[0] == 401
    assert request(server, "GET", "/api/overview",
                   headers={"X-Sponsy-Token": "s3cret"})[0] == 200
    assert request(server, "GET", "/")[0] == 200          # the page itself is public


def test_no_public_bind_without_a_token(app):
    with pytest.raises(SystemExit, match="token"):
        web.serve(app, host="0.0.0.0", port=0)
    assert web.is_loopback("127.0.0.1") and web.is_loopback("localhost")
    assert not web.is_loopback("0.0.0.0") and not web.is_loopback("192.168.1.4")
