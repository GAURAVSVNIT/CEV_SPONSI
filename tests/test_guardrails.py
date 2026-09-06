"""The rules that must hold even if every prompt in the repo is wrong.

Ported from the ancestor. Every guardrail here is a SQLite trigger or a CHECK,
so the tests talk to the database directly: the point is that no Python has to
cooperate for the rule to hold.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from sponsy import ladder, store
from sponsy.models import (COLD_KINDS, KINDS, TRANSITIONS, Draft, IllegalTransition,
                           check_transition)

from .conftest import TODAY


def _draft(pid="P01", kind="initial"):
    return Draft(prospect_id=pid, kind=kind, subject="Example Hackathon, Gold tier",
                 body="Hi Devi,\n\nAcme Analytics runs a student programme.\n\n"
                      "Our Gold tier is Rs 20,000.\n\nRudraksh",
                 claims=["Acme Analytics runs a student programme"])


# ---------------------------------------------------------------------------
# triggers
# ---------------------------------------------------------------------------

def test_touch_cap_is_enforced_by_the_database(db, prospect):
    db.execute("UPDATE prospects SET touches=4 WHERE id='P01'")
    with pytest.raises(sqlite3.IntegrityError, match="touch cap"):
        store.insert_draft(db, _draft(), run_id=None)


def test_touches_cannot_exceed_four(db, prospect):
    db.execute("UPDATE prospects SET touches=4 WHERE id='P01'")
    with pytest.raises(sqlite3.IntegrityError, match="touch cap"):
        db.execute("UPDATE prospects SET touches=5 WHERE id='P01'")


def test_touches_check_constraint_holds_on_insert(db):
    """The trigger only fires on UPDATE; the CHECK covers a fresh row."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO prospects (id, company, status, touches) "
                   "VALUES ('P98','Over Cap','new',5)")


def test_declined_is_permanent_in_the_database(db, prospect):
    db.execute("UPDATE prospects SET status='declined' WHERE id='P01'")
    with pytest.raises(sqlite3.IntegrityError, match="permanent"):
        db.execute("UPDATE prospects SET status='interested' WHERE id='P01'")


def test_declined_is_permanent_in_the_status_machine(db, prospect):
    db.execute("UPDATE prospects SET status='declined' WHERE id='P01'")
    with pytest.raises(IllegalTransition):
        store.set_status(db, "P01", "queued")


@pytest.mark.parametrize("closed", ["cold", "declined", "interested"])
def test_no_draft_for_a_closed_prospect(db, prospect, closed):
    db.execute("UPDATE prospects SET status=? WHERE id='P01'", (closed,))
    with pytest.raises(sqlite3.IntegrityError, match="closed"):
        store.insert_draft(db, _draft(), run_id=None)


def test_no_draft_to_a_blocked_address(db, prospect):
    store.block_email(db, "devi@example.com", "asked us to stop")
    with pytest.raises(sqlite3.IntegrityError, match="do-not-contact"):
        store.insert_draft(db, _draft(), run_id=None)


def test_blocked_address_matching_ignores_case_and_whitespace(db, prospect):
    db.execute("UPDATE prospects SET email='  DEVI@Example.com ' WHERE id='P01'")
    store.block_email(db, "devi@example.com", "asked us to stop")
    with pytest.raises(sqlite3.IntegrityError, match="do-not-contact"):
        store.insert_draft(db, _draft(), run_id=None)


def test_no_send_without_a_human_verdict(db, prospect):
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    with pytest.raises(sqlite3.IntegrityError, match="human verdict"):
        db.execute("UPDATE drafts SET sent_at=datetime('now') WHERE id=?",
                   (draft_id,))
    db.execute("UPDATE drafts SET verdict='approved' WHERE id=?", (draft_id,))
    db.execute("UPDATE drafts SET sent_at=datetime('now') WHERE id=?", (draft_id,))
    assert store.get_draft(db, draft_id)["sent_at"]


@pytest.mark.parametrize("verdict", ["skipped", "rejected", "superseded"])
def test_a_negative_verdict_does_not_unlock_sending(db, prospect, verdict):
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    db.execute("UPDATE drafts SET verdict=? WHERE id=?", (verdict, draft_id))
    with pytest.raises(sqlite3.IntegrityError, match="human verdict"):
        db.execute("UPDATE drafts SET sent_at=datetime('now') WHERE id=?",
                   (draft_id,))


def test_an_edited_verdict_also_unlocks_sending(db, prospect):
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    db.execute("UPDATE drafts SET verdict='edited' WHERE id=?", (draft_id,))
    db.execute("UPDATE drafts SET sent_at=datetime('now') WHERE id=?", (draft_id,))


def test_a_draft_is_sent_only_once(db, prospect):
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    db.execute("UPDATE drafts SET verdict='approved' WHERE id=?", (draft_id,))
    db.execute("UPDATE drafts SET sent_at='2026-09-05T10:00:00' WHERE id=?",
               (draft_id,))
    with pytest.raises(sqlite3.IntegrityError, match="already sent"):
        db.execute("UPDATE drafts SET sent_at='2026-09-06T10:00:00' WHERE id=?",
                   (draft_id,))


def test_mark_sent_refuses_to_send_twice(db, prospect):
    draft_id = store.insert_draft(db, _draft(), run_id=None)
    first = store.mark_sent(db, draft_id, TODAY, message_id="<m1>")
    assert first["touches"] == 1
    second = store.mark_sent(db, draft_id, TODAY, message_id="<m2>")
    assert second.get("already_sent") is True
    assert store.get(db, "P01").touches == 1, "a repeat tap must not count a touch"


def test_a_reply_to_a_question_is_allowed_past_the_cap(db, prospect):
    """The cap is on cold outreach, not on answering someone who wrote to us."""
    db.execute("UPDATE prospects SET touches=4, status='replied' WHERE id='P01'")
    store.insert_draft(db, _draft(kind="needs_info"), run_id=None)


def test_a_reply_to_a_question_does_not_count_as_a_touch(db, prospect):
    db.execute("UPDATE prospects SET touches=2, status='replied', "
               "first_touch='2026-08-27', next_touch='2026-09-03' WHERE id='P01'")
    draft_id = store.insert_draft(db, _draft(kind="needs_info"), run_id=None)
    out = store.mark_sent(db, draft_id, TODAY, message_id="<a1>")
    after = store.get(db, "P01")
    assert after.touches == 2
    assert out["next_touch"] == "2026-09-03", "answering must not move the ladder"


def test_duplicate_email_addresses_are_rejected(db, prospect):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO prospects (id, company, email, status) "
                   "VALUES ('P99','Acme Again','DEVI@example.com','new')")


def test_empty_emails_do_not_collide(db):
    """Discovered prospects arrive without an address; several of them must
    coexist until find-contacts fills the column in."""
    db.execute("INSERT INTO prospects (id, company, email, status) "
               "VALUES ('S001','One','','new')")
    db.execute("INSERT INTO prospects (id, company, email, status) "
               "VALUES ('S002','Two','','new')")
    db.execute("INSERT INTO prospects (id, company, email, status) "
               "VALUES ('S003','Three',NULL,'new')")


def test_unknown_status_and_kind_are_rejected(db, prospect):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE prospects SET status='maybe' WHERE id='P01'")
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_draft(db, _draft(kind="spam"), run_id=None)


# ---------------------------------------------------------------------------
# status machine
# ---------------------------------------------------------------------------

def test_declined_has_no_way_out():
    assert TRANSITIONS["declined"] == set()
    for status in TRANSITIONS:
        if status != "declined":
            with pytest.raises(IllegalTransition):
                check_transition("declined", status)


def test_a_reply_is_reachable_from_every_open_state():
    for status in ("new", "researched", "queued", "sent", "cold"):
        check_transition(status, "replied")
        check_transition(status, "interested")
        check_transition(status, "declined")


def test_same_state_is_a_no_op():
    check_transition("declined", "declined")


def test_cold_kinds_are_the_outreach_kinds():
    assert set(COLD_KINDS) == set(KINDS) - {"needs_info"}


# ---------------------------------------------------------------------------
# ladder arithmetic
# ---------------------------------------------------------------------------

def test_ladder_days_are_zero_three_seven_fourteen():
    assert [s.day for s in ladder.LADDER] == [0, 3, 7, 14]
    assert [s.kind for s in ladder.LADDER] == ["initial", "nudge", "new_angle",
                                               "last_touch"]
    assert ladder.MAX_TOUCHES == 4
    assert ladder.COLD_AFTER_DAYS == 21


@pytest.mark.parametrize("touches,expected_kind,expected_day", [
    (0, "nudge", 3), (1, "new_angle", 7), (2, "last_touch", 14), (3, None, 21),
])
def test_schedule_after_send(touches, expected_kind, expected_day):
    anchor = TODAY
    when, kind = ladder.schedule_after_send(anchor, touches + 1, anchor)
    assert kind == expected_kind
    assert when == (anchor + timedelta(days=expected_day)).isoformat()


def test_next_step_walks_the_ladder():
    assert [ladder.next_step(n).kind for n in range(4)] == [
        "initial", "nudge", "new_angle", "last_touch"]


def test_no_fifth_step():
    assert ladder.next_step(4) is None
    assert ladder.next_step(9) is None


def test_cold_after_21_days():
    assert ladder.is_cold(TODAY, TODAY + timedelta(days=21))
    assert not ladder.is_cold(TODAY, TODAY + timedelta(days=20))
    assert not ladder.is_cold(None, TODAY), "never touched means never cold"
    assert ladder.cold_on("2026-09-05") == "2026-09-26"
    assert ladder.cold_on(None) is None


def test_ladder_anchored_in_the_past_still_lands_in_the_future():
    anchor = date(2026, 1, 1)
    when, kind = ladder.schedule_after_send(anchor, 1, TODAY)
    assert kind == "nudge"
    assert date.fromisoformat(when) > TODAY


def test_ladder_accepts_iso_strings_and_timestamps():
    when, kind = ladder.schedule_after_send("2026-09-05T09:00:00+00:00", 1, TODAY)
    assert (when, kind) == ("2026-09-08", "nudge")


def test_push_and_skip_constants():
    assert ladder.push(ladder.SKIP_PUSH_DAYS, TODAY) == "2026-09-08"
    assert ladder.push(ladder.NO_SOURCES_PUSH_DAYS, TODAY) == "2026-09-06"


def test_sweep_cold_marks_day_21_and_leaves_pending_cards_alone(db, prospect):
    db.execute("UPDATE prospects SET status='sent', touches=4, "
               "first_touch='2026-08-15', next_touch='2026-09-05' WHERE id='P01'")
    assert store.sweep_cold(db, date(2026, 9, 4)) == []
    assert store.sweep_cold(db, date(2026, 9, 5)) == ["P01"]
    after = store.get(db, "P01")
    assert after.status == "cold"
    assert after.next_touch is None


def test_sweep_cold_waits_for_a_verdict(db, prospect):
    db.execute("UPDATE prospects SET status='queued', touches=3, "
               "first_touch='2026-08-01' WHERE id='P01'")
    store.insert_draft(db, _draft(kind="last_touch"), run_id=None)
    assert store.sweep_cold(db, TODAY) == [], "a card is still waiting to be tapped"
    assert store.get(db, "P01").status == "queued"
