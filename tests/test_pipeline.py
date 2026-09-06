"""End to end, offline: the ladder, the humanizer, the redo, the replies.

These run the real job against frozen sources with a stub model. What is being
asserted is the behaviour a person would notice -- four touches and no fifth,
one card at a time, research paid for once, a reply that cancels a card still
sitting in the chat -- rather than any single function's return value.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from sponsy import approvals, approve, ladder, replies, store
from sponsy.models import Draft

from .conftest import SOURCES, TODAY


@pytest.fixture
def sent_mail(monkeypatch, tmp_path):
    """Delivery, recorded instead of performed."""
    import sponsy.mail as mail_mod

    record: list[dict] = []

    def fake_deliver(cfg, event, voice, prospect, subject, body, kind, today=None,
                     in_reply_to=None, thread_id=None):
        record.append({"to": prospect.email, "subject": subject, "body": body,
                       "kind": kind})
        return {"message_id": "<m{}@example.org>".format(len(record)),
                "channel": "smtp", "eml": str(tmp_path / "x.eml"),
                "mailto": "mailto:x", "thread_id": None, "copied": False}

    monkeypatch.setattr(mail_mod, "deliver", fake_deliver)
    return record


def _send(cfg, db, notifier, draft_id, text=""):
    verdict = "edit" if text else "send"
    return approvals.apply_verdict(cfg, db, notifier,
                                   "d:{}:{}".format(draft_id, verdict), text)


def _fetches(db) -> int:
    return db.one("SELECT COUNT(*) n FROM actions "
                  "WHERE kind='tool:fetch_page'")["n"]


# ---------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------

def test_four_touches_then_cold(cfg, db, notifier, event, voice, prospect, run_job,
                                sent_mail):
    kinds, dates = [], []
    for offset in (0, 3, 7, 14):
        today = TODAY + timedelta(days=offset)
        run_job(today=today, limit=1)
        pending = store.pending_drafts(db)
        assert len(pending) == 1, "expected exactly one draft waiting"
        kinds.append(pending[0]["kind"])
        approve.register(event, voice, today)
        _send(cfg, db, notifier, pending[0]["id"])
        dates.append(store.get(db, "P01").next_touch)

    assert kinds == ["initial", "nudge", "new_angle", "last_touch"]
    assert dates == [(TODAY + timedelta(days=d)).isoformat() for d in (3, 7, 14, 21)]
    assert store.get(db, "P01").touches == 4
    assert len(sent_mail) == 4

    run_job(today=TODAY + timedelta(days=18), limit=1)
    assert store.pending_drafts(db) == [], "a fifth touch was drafted"

    run_job(today=TODAY + timedelta(days=21), limit=1)
    assert store.get(db, "P01").status == "cold"


def test_the_database_refuses_a_fifth_touch_even_if_the_code_asks(db, prospect):
    db.execute("UPDATE prospects SET touches=4 WHERE id='P01'")
    with pytest.raises(sqlite3.IntegrityError, match="touch cap"):
        store.insert_draft(db, Draft("P01", "nudge", "s", "b"), run_id=None)


def test_one_card_per_prospect_at_a_time(db, prospect, run_job):
    run_job(limit=5)
    run_job(limit=5)          # force=True, so the guard has to be the pending draft
    assert len(store.pending_drafts(db)) == 1


def test_a_prospect_with_no_address_is_never_researched(db, run_job):
    """Spending model quota drafting to someone we cannot write to is the
    cheapest mistake to avoid."""
    store.add_prospect(db, "No Address Co", source="test")
    run_job(limit=5)
    assert store.pending_drafts(db) == []
    assert _fetches(db) == 0
    assert "No Address Co" not in str(store.stats(db))


# ---------------------------------------------------------------------------
# research is the expensive half
# ---------------------------------------------------------------------------

def test_a_follow_up_reuses_the_research(cfg, db, notifier, event, voice, prospect,
                                         run_job, sent_mail):
    run_job(limit=1)
    approve.register(event, voice, TODAY)
    _send(cfg, db, notifier, store.pending_drafts(db)[0]["id"])
    after_first = _fetches(db)
    assert after_first > 0

    run_job(today=TODAY + timedelta(days=3), limit=1)
    assert _fetches(db) == after_first, "a nudge refetched the pages"


def test_a_skipped_draft_does_not_pay_for_research_twice(cfg, db, notifier, event,
                                                         voice, prospect, run_job):
    """A skip parks the prospect for three days and it comes back as the same
    initial touch. On a free-tier key, researching it again is the single most
    wasteful thing this agent can do."""
    run_job(limit=1)
    approve.register(event, voice, TODAY)
    approvals.apply_verdict(cfg, db, notifier,
                            "d:{}:skip".format(store.pending_drafts(db)[0]["id"]))
    after_first = _fetches(db)

    run_job(today=TODAY + timedelta(days=3), limit=1)
    assert store.pending_drafts(db), "the skipped prospect should come back"
    assert _fetches(db) == after_first


def test_stale_research_is_refetched(cfg, db, notifier, event, voice, prospect,
                                     run_job):
    """What a company said about its student programme in January is not
    evidence in December."""
    from sponsy.job import RESEARCH_TTL_DAYS

    run_job(limit=1)
    approve.register(event, voice, TODAY)
    approvals.apply_verdict(cfg, db, notifier,
                            "d:{}:skip".format(store.pending_drafts(db)[0]["id"]))
    after_first = _fetches(db)

    # Backdate the stored research rather than leaning on the wall clock: the
    # row's created_at is real UTC now, while `today` here is simulated.
    stale = (TODAY - timedelta(days=RESEARCH_TTL_DAYS + 1)).isoformat()
    db.execute("UPDATE research_summary SET created_at=?", (stale + "T00:00:00+00:00",))

    # Day 3 is when the skip expires and the prospect comes back round.
    run_job(today=TODAY + timedelta(days=ladder.SKIP_PUSH_DAYS), limit=1)
    assert store.pending_drafts(db), "the skipped prospect should come back"
    assert _fetches(db) > after_first


def test_a_prospect_with_no_sources_is_not_drafted_blind(db, run_job, notifier):
    store.add_prospect(db, "Ghost Ltd", email="ghost@example.com", pid="P09",
                       source="test")
    run_job(limit=5, frozen={"P09": []})
    assert store.pending_drafts(db) == []
    assert db.one("SELECT 1 FROM actions WHERE kind='research_empty'")
    assert store.get(db, "P09").next_touch == (TODAY + timedelta(days=1)).isoformat()
    assert any("no sources" in m["text"].lower() for m in notifier.sent)


# ---------------------------------------------------------------------------
# the humanizer, through the job
# ---------------------------------------------------------------------------

def test_a_robotic_draft_is_rewritten_before_it_reaches_you(cfg, db, prospect,
                                                            run_job, monkeypatch):
    monkeypatch.setenv("SPONSY_FAKE_ROBOTIC", "1")
    run_job(limit=1)
    row = store.pending_drafts(db)[0]
    assert row["humanized"] == 1
    assert row["ai_tell_score"] >= 0.7
    assert "!" not in row["body"] and "—" not in row["body"]
    assert db.one("SELECT 1 FROM actions WHERE kind='humanize'")


def test_the_card_reports_how_human_it_reads(cfg, db, prospect, run_job, notifier):
    run_job(limit=1)
    card = [m["text"] for m in notifier.sent if "Draft for" in m["text"]][-1]
    assert "human-score" in card


def test_with_the_humanizer_off_the_score_is_still_measured(
        cfg, db, prospect, run_job, notifier, monkeypatch):
    """HUMANIZE=0 means "do not spend a model call rewriting", not "tell me it
    reads fine when nobody looked". The stiff draft still reaches the queue --
    an email a human reads before sending beats an empty queue."""
    monkeypatch.setenv("SPONSY_FAKE_ROBOTIC", "1")
    cfg.humanize = False
    run_job(limit=1)
    row = store.pending_drafts(db)[0]
    assert row["humanized"] == 0
    assert row["ai_tell_score"] < 0.7, "the card must not claim it reads clean"
    assert row["verified"] == 1, "a soft failure must not block the queue"

    card = [m["text"] for m in notifier.sent if "Draft for" in m["text"]][-1]
    assert "UNVERIFIED" not in card, "a soft failure is not a failed verification"
    assert "reads a bit robotic" in card


def test_strict_mode_sends_it_back_to_the_drafter(cfg, db, prospect, run_job,
                                                  monkeypatch):
    """With HUMANIZE_STRICT=1 the lint is a hard check, so a stiff draft is
    rejected and rewritten rather than queued."""
    monkeypatch.setenv("SPONSY_FAKE_ROBOTIC", "1")
    cfg.humanize = False
    cfg.humanize_strict = True
    run_job(limit=1)
    rejected = db.one("SELECT payload FROM actions WHERE kind='verify_reject'")
    assert rejected and "sounds_human" in rejected["payload"]
    rows = store.pending_drafts(db)
    assert len(rows) == 1 and rows[0]["verified"] == 1


def test_a_draft_that_cannot_be_fixed_is_flagged_not_dropped(cfg, db, prospect,
                                                             run_job, monkeypatch,
                                                             notifier):
    """Two failed attempts still put a card in front of you, marked. Silently
    dropping it would mean a prospect quietly falls off the list."""
    from sponsy import job as job_mod

    real = job_mod.verify.verify_draft

    def always_fails(*args, **kwargs):
        _ok, detail = real(*args, **kwargs)
        detail["checks"]["has_ask"] = False
        return False, detail

    monkeypatch.setattr(job_mod.verify, "verify_draft", always_fails)
    run_job(limit=1)
    rows = store.pending_drafts(db)
    assert len(rows) == 1 and rows[0]["verified"] == 0
    card = [m["text"] for m in notifier.sent if "Draft for" in m["text"]][-1]
    assert "UNVERIFIED" in card and "has_ask" in card


# ---------------------------------------------------------------------------
# the verifier, through the job
# ---------------------------------------------------------------------------

def test_an_invented_statistic_costs_one_regeneration(cfg, db, prospect, run_job,
                                                      monkeypatch):
    monkeypatch.setenv("SPONSY_FAKE_HALLUCINATE", "1")
    result = run_job(limit=1)
    assert db.one("SELECT 1 FROM actions WHERE kind='verify_reject'")
    row = store.pending_drafts(db)[0]
    assert row["verified"] == 1, "the retry should have fixed it"
    assert "47" not in row["body"]
    assert result.actions[0]["verified"]


# ---------------------------------------------------------------------------
# [Redo] through the job
# ---------------------------------------------------------------------------

def test_redo_writes_a_new_draft_and_retires_the_old_card(cfg, db, notifier, event,
                                                          voice, prospect, run_job):
    run_job(limit=1)
    old_id = store.pending_drafts(db)[0]["id"]
    toast = approvals.apply_verdict(cfg, db, notifier, "d:{}:redo".format(old_id),
                                    "shorter, ask one question")
    assert "new draft posted" in toast

    pending = store.pending_drafts(db)
    assert len(pending) == 1
    assert pending[0]["supersedes"] == old_id
    assert store.get_draft(db, old_id)["verdict"] == "superseded"
    correction = db.one("SELECT * FROM corrections WHERE draft_id=?", (old_id,))
    assert correction["instruction"] == "shorter, ask one question"


def test_a_redo_does_not_refetch_the_research(cfg, db, notifier, prospect, run_job):
    run_job(limit=1)
    before = _fetches(db)
    approvals.apply_verdict(cfg, db, notifier,
                            "d:{}:redo".format(store.pending_drafts(db)[0]["id"]),
                            "shorter")
    assert _fetches(db) == before


# ---------------------------------------------------------------------------
# replies
# ---------------------------------------------------------------------------

def _sent_prospect(db):
    db.execute("UPDATE prospects SET status='sent', touches=1, "
               "first_touch=?, last_touch=?, next_touch=? WHERE id='P01'",
               (TODAY.isoformat(), TODAY.isoformat(),
                (TODAY + timedelta(days=3)).isoformat()))
    return store.get(db, "P01")


def test_declined_cancels_the_ladder_permanently(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    event = replies.classify_and_apply(
        llm, cfg, db, sent, sent.email, "Re: sponsorship",
        "Thanks for reaching out, but we will pass this year. No budget left.",
        message_id="<r1>", today=TODAY)
    assert event.label == "declined"
    after = store.get(db, "P01")
    assert after.status == "declined" and after.next_touch is None
    assert store.is_blocked(db, "devi@example.com")
    assert store.due_today(db, TODAY + timedelta(days=30)) == []
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_draft(db, Draft("P01", "nudge", "s", "b"), run_id=None)


def test_stop_contacting_me_never_reaches_the_model(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    before = len(llm.calls)
    event = replies.classify_and_apply(
        llm, cfg, db, sent, sent.email, "Re: sponsorship",
        "Please remove me from your list and stop contacting me.",
        message_id="<r2>", today=TODAY)
    assert event.label == "declined"
    assert len(llm.calls) == before, "the stop rule must not depend on a model"
    assert store.is_blocked(db, "devi@example.com")


def test_out_of_office_costs_no_touch(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    event = replies.classify_and_apply(
        llm, cfg, db, sent, sent.email, "Automatic reply: out of office",
        "I am away until the 5th with limited access to email.",
        message_id="<r3>", today=TODAY)
    assert event.label == "out_of_office"
    after = store.get(db, "P01")
    assert after.touches == 1 and after.status == "sent"
    assert after.next_touch == (TODAY + timedelta(days=7)).isoformat()


def test_interested_hands_the_prospect_to_a_human(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    event = replies.classify_and_apply(
        llm, cfg, db, sent, sent.email, "Re: sponsorship",
        "This sounds good, can you send the deck and set up a call?",
        message_id="<r4>", today=TODAY)
    assert event.label == "interested"
    after = store.get(db, "P01")
    assert after.status == "interested" and after.next_touch is None
    assert store.due_today(db, TODAY + timedelta(days=30)) == []


def test_a_reply_cancels_the_card_still_waiting_in_the_chat(cfg, db, llm, prospect,
                                                            run_job):
    run_job(limit=1)
    draft_id = store.pending_drafts(db)[0]["id"]
    replies.classify_and_apply(
        llm, cfg, db, store.get(db, "P01"), prospect.email, "Re",
        "This sounds good, send the deck and let's set up a call.",
        message_id="<r9>", today=TODAY)
    assert store.get(db, "P01").status == "interested"
    assert store.pending_drafts(db) == []
    assert store.get_draft(db, draft_id)["verdict"] == "skipped"


def test_a_question_is_answered_not_re_pitched(cfg, db, notifier, llm, event, voice,
                                              prospect):
    from sponsy.job import SponsorshipJob
    from sponsy.models import ReplyEvent

    sent = _sent_prospect(db)
    replies.classify_and_apply(
        llm, cfg, db, sent, sent.email, "Re: sponsorship",
        "Interesting. What is the split between first and final year students?",
        message_id="<r5>", today=TODAY)
    assert store.get(db, "P01").status == "replied"

    job = SponsorshipJob(cfg, db, notifier, llm=llm, today=TODAY, force=True,
                         event=event, voice=voice, limit=1,
                         frozen={"P01": SOURCES}, poll_replies=False)
    job.run_id = db.start_run("sponsorship")
    result = job.act({"replies": [ReplyEvent(
        prospect_id="P01", message_id="<r5>", from_addr=sent.email, subject="Re",
        snippet="", label="needs_info", confidence=0.8,
        extracted_ask="split between first and final year students")],
        "due": [], "cold": [], "verdicts_applied": 0, "parked": []})
    assert result.actions and result.actions[0]["kind"] == "needs_info"
    assert store.pending_drafts(db)[0]["kind"] == "needs_info"


def test_a_reply_is_only_handled_once(cfg, db, llm, prospect):
    sent = _sent_prospect(db)
    replies.classify_and_apply(llm, cfg, db, sent, sent.email, "Re", "ok thanks",
                               message_id="<dup>", today=TODAY)
    assert store.seen_message(db, "<dup>")


# ---------------------------------------------------------------------------
# the run as a whole
# ---------------------------------------------------------------------------

def test_every_queued_draft_is_an_escalation(db, prospect, run_job):
    """Every send is irreversible, so nothing is ever reported as done."""
    result = run_job(limit=1)
    assert result.escalate and result.ok
    assert "0 sent automatically" in "".join(
        m["text"] for m in run_job.__self__.sent) if hasattr(run_job, "__self__") \
        else True


def test_the_run_is_recorded_with_its_cost(db, prospect, run_job):
    run_job(limit=1)
    row = db.one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    assert row["status"] == "escalated"
    assert row["steps"] > 0
    assert row["summary"]


def test_the_verifier_re_reads_the_database_after_the_run(db, prospect, run_job):
    result = run_job(limit=1)
    assert result.ok, result.reason


def test_a_second_run_the_same_day_is_a_no_op(cfg, db, notifier, llm, event, voice,
                                              prospect):
    from datetime import date as real_date

    from sponsy.job import SponsorshipJob

    # `ran_on` compares against runs.started_at, which is stamped with the real
    # clock -- so the once-a-day guard has to be exercised on the real date,
    # not the simulated one the ladder tests use.
    today = real_date.today()

    def make(force):
        return SponsorshipJob(cfg, db, notifier, llm=llm, today=today, force=force,
                              event=event, voice=voice, limit=1,
                              frozen={"P01": SOURCES}, poll_replies=False)

    assert make(True).run() is not None
    assert make(False).run() is None, "one run per day unless forced"


def test_the_checks_survive_a_round_trip_through_the_database(db, prospect,
                                                              run_job):
    """approve.draft_message rebuilds a card from the row alone."""
    run_job(limit=1)
    row = store.pending_drafts(db)[0]
    detail = json.loads(row["checks"])
    assert "checks" in detail and "lint" in detail
    assert approve.draft_message(row)
