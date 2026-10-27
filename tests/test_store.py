"""store.py: prospects, drafts, corrections, replies, migration, stats.

The triggers have their own file (test_guardrails). This one covers the
queries and the bookkeeping around them.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from sponsy import store
from sponsy.models import Draft, Excerpt, Research, ReplyEvent
from sponsy.state import Database

from .conftest import TODAY


def _draft(pid="P01", kind="initial", subject="Example Hackathon, Gold tier",
           body="Hi Devi,\n\nAcme runs a student programme.\n\nRudraksh"):
    return Draft(prospect_id=pid, kind=kind, subject=subject, body=body,
                 claims=["Acme runs a student programme"])


# ---------------------------------------------------------------------------
# prospects
# ---------------------------------------------------------------------------

def test_add_prospect_ids_run_s001_s002(db):
    assert store.add_prospect(db, "Acme Corp") == "S001"
    assert store.add_prospect(db, "Beta Labs") == "S002"
    assert store.get(db, "S002").status == "new"


def test_add_prospect_dedups_by_normalised_company(db):
    first = store.add_prospect(db, "ACME Corp Inc", email="a@acme.example")
    again = store.add_prospect(db, "Acme Corp", email="other@acme.example")
    assert first == again == "S001"
    assert store.get(db, "S001").email == "a@acme.example", \
        "a duplicate must not overwrite the existing row"
    assert len(store.all_prospects(db)) == 1


def test_add_prospect_normalises_email_and_source(db):
    pid = store.add_prospect(db, "Gamma", email="  DevRel@Gamma.IO ",
                             contact_name=" Priya ", source="discover")
    row = store.get(db, pid)
    assert row.email == "devrel@gamma.io"
    assert row.contact_name == "Priya"
    assert row.source == "discover"


def test_add_prospect_requires_a_company(db):
    with pytest.raises(ValueError):
        store.add_prospect(db, "   ")


def test_add_prospect_skips_ids_already_taken(db, prospect):
    """P01 exists from the fixture; S-ids are counted separately, and an
    explicit pid is honoured."""
    assert store.add_prospect(db, "Delta") == "S001"
    assert store.add_prospect(db, "Epsilon", pid="X9") == "X9"
    assert store.add_prospect(db, "Zeta") == "S002"


def test_norm_company_strips_legal_suffixes_and_punctuation():
    assert store.norm_company("Acme Corp, Inc.") == store.norm_company("ACME")
    assert store.norm_company("Zerodha Technologies Pvt Ltd") == "zerodha"
    assert store.norm_company("") == ""


def test_find_by_company(db, prospect):
    assert store.find_by_company(db, "acme analytics").id == "P01"
    assert store.find_by_company(db, "Nobody") is None
    assert store.find_by_company(db, "") is None


def test_export_csv_round_trips_through_import_csv(db, tmp_path):
    store.add_prospect(db, "Acme Corp", email="a@acme.example", contact_name="Ana",
                       segment="saas", notes="warm", website="https://acme.example")
    store.add_prospect(db, "Beta Labs", email="b@beta.example")
    db.execute("UPDATE prospects SET status='sent', touches=2 WHERE id='S001'")
    out = tmp_path / "export.csv"
    assert store.export_csv(db, out) == 2
    header = out.read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == list(store.EXPORT_COLUMNS)

    other = Database(tmp_path / "other.db")
    store.ensure_schema(other)
    try:
        report = store.import_csv(other, out)
        assert report["added"] == 2 and report["problems"] == []
        got = store.get(other, "S001")
        assert (got.company, got.email, got.contact_name, got.segment, got.notes,
                got.website, got.status) == (
            "Acme Corp", "a@acme.example", "Ana", "saas", "warm",
            "https://acme.example", "sent")
        assert store.get(other, "S002").email == "b@beta.example"
    finally:
        other.close()


def test_import_csv_reports_bad_rows_and_duplicates(db, tmp_path):
    csv = tmp_path / "p.csv"
    csv.write_text("prospect_id,company,email\n"
                   ",No Id,x@example.com\n"
                   "P10,First,dup@example.com\n"
                   "P11,Second,DUP@example.com\n", encoding="utf-8")
    report = store.import_csv(db, csv)
    assert report["added"] == 1
    assert report["skipped"] == 2
    assert len(report["problems"]) == 2


def test_reimport_keeps_an_address_found_after_the_first_import(db, tmp_path):
    """find-contacts --apply writes to the database only. The list still has
    a blank cell, and re-importing it must not park the prospect again."""
    csv = tmp_path / "p.csv"
    csv.write_text("prospect_id,company,contact_name,email,segment,notes\n"
                   "P20,Acme Corp,,,saas,warm\n", encoding="utf-8")
    store.import_csv(db, csv)
    store.set_email(db, "P20", "sponsorships@acme.example", contact_name="DevRel team")
    assert store.import_csv(db, csv)["updated"] == 1
    row = store.get(db, "P20")
    assert (row.email, row.contact_name, row.segment, row.notes) == (
        "sponsorships@acme.example", "DevRel team", "saas", "warm")
    assert "P20" not in store.awaiting_contact(db)


def test_reimport_with_a_new_address_replaces_the_old_one(db, tmp_path):
    csv = tmp_path / "p.csv"
    csv.write_text("prospect_id,company,contact_name,email\n"
                   "P20,Acme Corp,Ana,old@acme.example\n", encoding="utf-8")
    store.import_csv(db, csv)
    csv.write_text("prospect_id,company,contact_name,email\n"
                   "P20,Acme Corp,Ravi,new@acme.example\n", encoding="utf-8")
    store.import_csv(db, csv)
    row = store.get(db, "P20")
    assert (row.email, row.contact_name) == ("new@acme.example", "Ravi")


def test_set_email_logs_a_contact_set_action(db, prospect):
    store.set_email(db, "P01", " Hello@Acme.Example ", contact_name="DevRel team",
                    source_url="https://acme.example/sponsors")
    row = store.get(db, "P01")
    assert row.email == "hello@acme.example"
    assert row.contact_name == "DevRel team"
    action = db.one("SELECT * FROM actions WHERE kind='contact_set' AND target='P01'")
    assert action is not None
    assert "acme.example/sponsors" in action["payload"]


def test_set_email_keeps_the_contact_name_when_not_given(db, prospect):
    store.set_email(db, "P01", "new@example.com")
    assert store.get(db, "P01").contact_name == "Devi Sharma"


def test_set_status_checks_the_machine(db, prospect):
    store.set_status(db, "P01", "researched")
    store.set_status(db, "P01", "queued")
    with pytest.raises(KeyError):
        store.set_status(db, "nope", "queued")


# ---------------------------------------------------------------------------
# due_today
# ---------------------------------------------------------------------------

def test_due_today_lists_new_prospects_with_an_address(db, prospect):
    assert [p.id for p in store.due_today(db, TODAY)] == ["P01"]


def test_due_today_excludes_prospects_with_a_pending_draft(db, prospect):
    store.insert_draft(db, _draft(), run_id=None)
    assert store.due_today(db, TODAY) == []
    db.execute("UPDATE drafts SET verdict='skipped'")
    assert [p.id for p in store.due_today(db, TODAY)] == ["P01"]


def test_due_today_excludes_prospects_with_no_email(db, prospect):
    store.add_prospect(db, "No Address Yet")
    store.add_prospect(db, "Blank Address", email="   ")
    assert [p.id for p in store.due_today(db, TODAY)] == ["P01"]
    assert store.awaiting_contact(db) == ["S001", "S002"]


def test_due_today_respects_next_touch_and_the_cap(db, prospect):
    db.execute("UPDATE prospects SET status='sent', touches=1, "
               "next_touch=? WHERE id='P01'", ((TODAY + timedelta(days=1)).isoformat(),))
    assert store.due_today(db, TODAY) == []
    assert [p.id for p in store.due_today(db, TODAY + timedelta(days=1))] == ["P01"]
    db.execute("UPDATE prospects SET touches=4 WHERE id='P01'")
    assert store.due_today(db, TODAY + timedelta(days=1)) == []


def test_due_today_excludes_closed_replied_and_blocked(db, prospect):
    store.add_prospect(db, "Blocked", email="stop@example.com")
    store.block_email(db, "stop@example.com", "asked")
    for status in ("replied", "interested", "cold", "declined"):
        db.execute("UPDATE prospects SET status=? WHERE id='P01'", (status,))
        assert store.due_today(db, TODAY) == []
        if status == "declined":
            break


def test_due_today_orders_new_before_scheduled_and_honours_limit(db):
    store.add_prospect(db, "Later", email="l@example.com")
    db.execute("UPDATE prospects SET status='sent', touches=1, next_touch=? "
               "WHERE id='S001'", (TODAY.isoformat(),))
    store.add_prospect(db, "Fresh", email="f@example.com")
    ids = [p.id for p in store.due_today(db, TODAY)]
    assert ids == ["S002", "S001"]
    assert len(store.due_today(db, TODAY, limit=1)) == 1


# ---------------------------------------------------------------------------
# mark_never / do-not-contact
# ---------------------------------------------------------------------------

def test_mark_never_blocks_declines_and_cancels(db, prospect):
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    cancelled = store.mark_never(db, "P01", reason="the human said so")
    assert cancelled == 1
    after = store.get(db, "P01")
    assert after.status == "declined"
    assert after.next_touch is None
    assert store.is_blocked(db, "devi@example.com")
    assert store.is_blocked(db, "DEVI@example.com ")
    assert store.get_draft(db, draft_id)["verdict"] == "skipped"
    assert store.pending_drafts(db) == []
    assert db.one("SELECT 1 FROM actions WHERE kind='never' AND target='P01'")
    assert db.one("SELECT 1 FROM actions WHERE kind='cancel_draft' AND target='P01'")


def test_mark_never_is_idempotent(db, prospect):
    store.mark_never(db, "P01")
    assert store.mark_never(db, "P01") == 0
    assert store.get(db, "P01").status == "declined"
    assert db.one("SELECT COUNT(*) n FROM do_not_contact")["n"] == 1


def test_mark_never_without_an_address_still_declines(db):
    pid = store.add_prospect(db, "Quiet Co")
    store.mark_never(db, pid)
    assert store.get(db, pid).status == "declined"
    assert db.one("SELECT COUNT(*) n FROM do_not_contact")["n"] == 0


def test_mark_never_unknown_prospect(db):
    with pytest.raises(KeyError):
        store.mark_never(db, "P404")


def test_block_email_ignores_empty_and_is_blocked_is_lenient(db):
    store.block_email(db, "", "nothing")
    assert not store.is_blocked(db, "")
    assert not store.is_blocked(db, None)


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------

def test_excerpts_are_returned_oldest_first_up_to_limit(db, prospect):
    for i in range(10):
        store.save_excerpt(db, "P01", None, "https://x/{}".format(i), "t{}".format(i),
                           "text {}".format(i))
    got = store.excerpts_for(db, "P01", limit=3)
    assert [e.url for e in got] == ["https://x/7", "https://x/8", "https://x/9"]
    assert isinstance(got[0], Excerpt)
    assert got[0].block(1).startswith("[S1] t7")


def test_research_round_trips_through_json(db, prospect):
    research = Research(prospect_id="P01", one_liner="Acme builds dashboards",
                        hook="student programme", sources_used=["https://x"],
                        confidence=0.8, recent_signal="launched a free tier")
    assert store.latest_research(db, "P01") is None
    store.save_research(db, research, run_id=None)
    back = store.latest_research(db, "P01")
    assert back.one_liner == "Acme builds dashboards"
    assert back.recent_signal == "launched a free tier"
    assert back.sources_used == ["https://x"]
    assert back.confidence == 0.8
    assert not back.thin
    assert Research(prospect_id="P01", confidence=0.9).thin, "no hook, no sources"


# ---------------------------------------------------------------------------
# drafts
# ---------------------------------------------------------------------------

def test_insert_and_get_draft_join_the_prospect(db, prospect):
    draft = _draft()
    draft.checks = {"checks": {"length": True}}
    draft.ai_tell_score = 0.9
    draft_id = store.insert_draft(db, draft, run_id=None)
    assert draft.id == draft_id
    row = store.get_draft(db, draft_id)
    assert row["company"] == "Acme Analytics"
    assert row["email"] == "devi@example.com"
    assert row["ai_tell_score"] == 0.9
    assert row["verdict"] is None
    assert store.has_pending_draft(db, "P01")
    assert [r["id"] for r in store.drafts_for(db, "P01")] == [draft_id]
    assert store.get_draft(db, 999) is None


def test_supersede_draft_links_old_and_new(db, prospect):
    old = store.insert_draft(db, _draft(), run_id=None)
    new = store.insert_draft(db, _draft(subject="shorter"), run_id=None)
    store.supersede_draft(db, old, new)
    assert store.get_draft(db, old)["verdict"] == "superseded"
    assert store.get_draft(db, new)["supersedes"] == old
    assert [r["id"] for r in store.pending_drafts(db)] == [new]


def test_supersede_draft_leaves_a_decided_draft_alone(db, prospect):
    old = store.insert_draft(db, _draft(), run_id=None)
    db.execute("UPDATE drafts SET verdict='edited' WHERE id=?", (old,))
    store.supersede_draft(db, old)
    assert store.get_draft(db, old)["verdict"] == "edited"


def test_record_redo_lands_in_corrections_with_the_instruction(db, prospect):
    old = store.insert_draft(db, _draft(), run_id=None)
    new = _draft(subject="a note about the case comp", body="Hi Devi,\n\nShorter.")
    store.record_redo(db, old, new, "make it shorter")
    row = db.one("SELECT * FROM corrections WHERE draft_id=?", (old,))
    assert row["instruction"] == "make it shorter"
    assert row["before_subject"] == "Example Hackathon, Gold tier"
    assert row["after_body"] == "Hi Devi,\n\nShorter."
    assert row["kind"] == "initial"
    assert store.get_draft(db, old)["subject"] == "Example Hackathon, Gold tier", \
        "a redo does not rewrite the old draft in place"
    with pytest.raises(KeyError):
        store.record_redo(db, 999, new, "x")


def test_recent_corrections_are_oldest_first(db, prospect):
    old = store.insert_draft(db, _draft(), run_id=None)
    store.record_redo(db, old, _draft(subject="one"), "first")
    store.record_redo(db, old, _draft(subject="two"), "second")
    store.record_redo(db, old, _draft(subject="three"), "third")
    store.record_redo(db, old, _draft(subject="four"), "fourth")
    got = store.recent_corrections(db, limit=3)
    assert [c["instruction"] for c in got] == ["second", "third", "fourth"]
    assert got[-1]["after_subject"] == "four"
    assert store.recent_corrections(db, limit=1)[0]["instruction"] == "fourth"


def test_record_correction_rewrites_the_draft_and_marks_it_edited(db, prospect):
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    store.record_correction(db, draft_id, "Shorter", "Hi Devi,\n\nOne line.",
                            instruction="")
    row = store.get_draft(db, draft_id)
    assert row["verdict"] == "edited"
    assert row["subject"] == "Shorter"
    pair = db.one("SELECT * FROM corrections WHERE draft_id=?", (draft_id,))
    assert pair["before_body"] != pair["after_body"]
    assert store.recent_corrections(db)[0]["after_subject"] == "Shorter"
    with pytest.raises(KeyError):
        store.record_correction(db, 999, "s", "b")


def test_trg_send_only_once_rejects_a_second_sent_at(db, prospect):
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    store.mark_sent(db, draft_id, TODAY, message_id="<one>")
    with pytest.raises(sqlite3.IntegrityError, match="already sent"):
        db.execute("UPDATE drafts SET sent_at='2030-01-01T00:00:00' WHERE id=?",
                   (draft_id,))


def test_mark_sent_advances_the_ladder_and_records_the_thread(db, prospect):
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    out = store.mark_sent(db, draft_id, TODAY, message_id="<m1@example.org>",
                          channel="gmail_api", gmail_thread_id="thr1")
    assert out["touches"] == 1
    assert out["next_kind"] == "nudge"
    assert out["next_touch"] == (TODAY + timedelta(days=3)).isoformat()
    row = store.get_draft(db, draft_id)
    assert row["verdict"] == "approved"
    assert row["channel"] == "gmail_api"
    assert row["message_id"] == "<m1@example.org>"
    after = store.get(db, "P01")
    assert after.status == "sent"
    assert after.first_touch == after.last_touch == TODAY.isoformat()
    assert after.thread_id == "<m1@example.org>"
    assert after.gmail_thread_id == "thr1"


def test_mark_sent_keeps_the_first_thread_id(db, prospect):
    first = store.insert_draft(db, _draft(), run_id=None)
    store.mark_sent(db, first, TODAY, message_id="<first>")
    second = store.insert_draft(db, _draft(kind="nudge"), run_id=None)
    store.mark_sent(db, second, TODAY + timedelta(days=3), message_id="<second>")
    after = store.get(db, "P01")
    assert after.thread_id == "<first>"
    assert after.touches == 2
    assert after.first_touch == TODAY.isoformat()


def test_mark_sent_unknown_draft(db):
    with pytest.raises(KeyError):
        store.mark_sent(db, 42, TODAY)


def test_skip_draft_pushes_three_days(db, prospect):
    db.execute("UPDATE prospects SET status='queued' WHERE id='P01'")
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    out = store.skip_draft(db, draft_id, TODAY)
    assert out["next_touch"] == (TODAY + timedelta(days=3)).isoformat()
    assert store.get_draft(db, draft_id)["verdict"] == "skipped"
    after = store.get(db, "P01")
    assert after.status == "researched", "no touch yet, so back to researched"
    assert after.next_touch == out["next_touch"]


def test_skip_draft_after_a_send_goes_back_to_sent(db, prospect):
    db.execute("UPDATE prospects SET status='queued', touches=1 WHERE id='P01'")
    draft_id = store.insert_draft(db, _draft(kind="nudge"), run_id=None)
    store.skip_draft(db, draft_id, TODAY)
    assert store.get(db, "P01").status == "sent"


def test_reject_draft_is_a_skip_with_a_harsher_verdict(db, prospect):
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    store.reject_draft(db, draft_id, TODAY)
    assert store.get_draft(db, draft_id)["verdict"] == "rejected"
    assert store.get(db, "P01").next_touch == (TODAY + timedelta(days=3)).isoformat()


def test_cancel_pending_only_touches_undecided_drafts(db, prospect):
    a = store.insert_draft(db, _draft(), run_id=None)
    b = store.insert_draft(db, _draft(kind="nudge"), run_id=None)
    db.execute("UPDATE drafts SET verdict='edited' WHERE id=?", (a,))
    assert store.cancel_pending(db, "P01", "they replied") == 1
    assert store.get_draft(db, a)["verdict"] == "edited"
    assert store.get_draft(db, b)["verdict"] == "skipped"
    assert store.cancel_pending(db, "P01") == 0


def test_set_tg_message(db, prospect):
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    store.set_tg_message(db, draft_id, 77)
    assert store.get_draft(db, draft_id)["tg_message_id"] == 77


# ---------------------------------------------------------------------------
# replies
# ---------------------------------------------------------------------------

def _reply(pid="P01", message_id="<r1>", label="interested"):
    return ReplyEvent(prospect_id=pid, message_id=message_id,
                      from_addr="devi@example.com", subject="Re: hi",
                      snippet="x" * 900, label=label, confidence=0.9)


def test_record_reply_and_seen_message(db, prospect):
    assert not store.seen_message(db, "<r1>")
    assert not store.seen_message(db, "")
    store.record_reply(db, _reply(), uid="42", action="interested -> human")
    assert store.seen_message(db, "<r1>")
    row = db.one("SELECT * FROM replies WHERE message_id='<r1>'")
    assert len(row["snippet"]) == 500, "snippets are capped"
    assert row["uid"] == "42"
    assert row["action"] == "interested -> human"


def test_match_thread_resolves_by_gmail_thread_id(db, prospect):
    db.execute("UPDATE prospects SET gmail_thread_id='thr-77' WHERE id='P01'")
    assert store.match_thread(db, [], "", gmail_thread_id="thr-77") == "P01"
    assert store.match_thread(db, [], "", gmail_thread_id="thr-00") is None


def test_match_thread_prefers_headers_then_thread_then_address(db, prospect):
    store.add_prospect(db, "Other", email="other@example.com")
    db.execute("UPDATE prospects SET thread_id='<first@ours>', "
               "gmail_thread_id='g1' WHERE id='P01'")
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    store.mark_sent(db, draft_id, TODAY, message_id="<sent@ours>")
    # References header pointing at the draft's own Message-ID
    assert store.match_thread(db, ["<sent@ours>"], "other@example.com") == "P01"
    # the prospect-level thread id
    assert store.match_thread(db, ["<x>", "<first@ours>"], "other@example.com") == "P01"
    # gmail thread beats the address
    assert store.match_thread(db, [], "other@example.com", gmail_thread_id="g1") == "P01"
    # address as the last resort, case-insensitive
    assert store.match_thread(db, ["<unknown>"], " Other@Example.com ") == "S001"
    assert store.match_thread(db, [], "nobody@example.com") is None


# ---------------------------------------------------------------------------
# schema migration
# ---------------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE prospects (
  id TEXT PRIMARY KEY, company TEXT NOT NULL, contact_name TEXT, email TEXT,
  segment TEXT, status TEXT NOT NULL DEFAULT 'new', first_touch TEXT,
  last_touch TEXT, next_touch TEXT, thread_id TEXT,
  touches INTEGER NOT NULL DEFAULT 0, notes TEXT, created_at TEXT
);
CREATE TABLE drafts (
  id INTEGER PRIMARY KEY, prospect_id TEXT REFERENCES prospects(id), run_id INTEGER,
  action_id INTEGER, kind TEXT, subject TEXT, body TEXT, claims TEXT, checks TEXT,
  verified INTEGER DEFAULT 0, confidence REAL, created_at TEXT, verdict TEXT,
  sent_at TEXT, message_id TEXT
);
CREATE TABLE corrections (
  id INTEGER PRIMARY KEY, draft_id INTEGER, prospect_id TEXT, kind TEXT,
  before_subject TEXT, before_body TEXT, after_subject TEXT, after_body TEXT,
  created_at TEXT
);
"""


def test_ensure_schema_adds_later_columns_to_an_old_database(tmp_path):
    old = Database(tmp_path / "old.db")
    try:
        old.executescript(OLD_SCHEMA)
        old.execute("INSERT INTO prospects (id, company, status) VALUES "
                    "('P01','Old Co','new')")
        for table, cols in store.LATER_COLUMNS.items():
            assert not set(cols) & old.columns(table), "fixture must predate them"

        store.ensure_schema(old)

        for table, cols in store.LATER_COLUMNS.items():
            missing = set(cols) - old.columns(table)
            assert not missing, "{} still lacks {}".format(table, missing)
        # the existing row survived and the new columns behave
        assert store.get(old, "P01").company == "Old Co"
        draft_id = store.insert_draft(old, _draft(), run_id=None)
        row = store.get_draft(old, draft_id)
        assert row["humanized"] == 0 and row["supersedes"] is None
        # the triggers were created on the migrated table too
        with pytest.raises(sqlite3.IntegrityError, match="human verdict"):
            old.execute("UPDATE drafts SET sent_at=datetime('now') WHERE id=?",
                        (draft_id,))
        # and running the migration again is harmless
        store.ensure_schema(old)
    finally:
        old.close()


def test_ensure_schema_is_idempotent_on_a_current_database(db):
    before = {t: db.columns(t) for t in ("prospects", "drafts", "corrections")}
    store.ensure_schema(db)
    assert {t: db.columns(t) for t in before} == before


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_stats_totals(db, prospect):
    store.add_prospect(db, "Parked")
    pid = store.add_prospect(db, "Gone", email="gone@example.com")
    store.mark_never(db, pid)
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    store.mark_sent(db, draft_id, TODAY, message_id="<s1>")
    store.insert_draft(db, _draft(kind="nudge"), run_id=None)

    out = store.stats(db)
    assert out["total"] == 3
    assert out["prospects"] == {"sent": 1, "new": 1, "declined": 1}
    assert out["drafts"] == {"approved": 1, "pending": 1}
    assert out["sent"] == 1
    assert out["blocked"] == 1
    assert out["awaiting_contact"] == ["S001"]


def test_stats_on_an_empty_database(db):
    out = store.stats(db)
    assert out["total"] == 0
    assert out["sent"] == 0
    assert out["blocked"] == 0
    assert out["awaiting_contact"] == []
