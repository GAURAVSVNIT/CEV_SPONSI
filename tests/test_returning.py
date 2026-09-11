"""The returning-sponsor variant.

Touch 1 has two forms. A company already on our past-sponsor list gets
`returning` instead of `initial`: the relationship is the hook, so the email
opens on it and skips the who-we-are paragraph entirely. Writing the cold
version to a company whose logo is on our own site is the failure this whole
variant exists to prevent, and it is the kind of mistake nobody notices until
a sponsor points it out.
"""

from __future__ import annotations

import pytest

from sponsy import approve, drafting, ladder, prompts, store, verify
from sponsy.config import Event, Tier
from sponsy.mail import message as message_mod
from sponsy.models import Draft

from .conftest import TODAY


# ---------------------------------------------------------------------------
# who counts as a returning sponsor
# ---------------------------------------------------------------------------

@pytest.fixture
def past_event(event) -> Event:
    event.past_sponsors = ["DigitalOcean", "Devfolio", "Coding Blocks"]
    return event


@pytest.mark.parametrize("company", [
    "DigitalOcean",
    "Digital Ocean",          # the prospect sheet spells it with a space
    "digitalocean",
    "DIGITALOCEAN",
    "DigitalOcean, Inc.",     # legal suffix stripped by norm_company
    "Coding Blocks",
])
def test_a_past_sponsor_is_recognised_however_it_is_spelled(past_event, company):
    assert past_event.is_past_sponsor(company)
    assert ladder.first_kind(past_event, company) == "returning"


@pytest.mark.parametrize("company", ["Postman", "Stripe", "", "Ocean", "Digital"])
def test_everyone_else_gets_the_cold_email(past_event, company):
    """The dangerous direction. A "welcome back" to a company that never
    sponsored us is a worse error than a cold pitch to one that did, because
    it is a false claim about them rather than a forgetful one about us."""
    assert not past_event.is_past_sponsor(company)
    assert ladder.first_kind(past_event, company) == "initial"


def test_an_event_without_the_method_still_drafts(event):
    """first_kind is consulted on every touch 1, so an Event loaded from a
    config written before this existed must not break the run."""
    assert ladder.first_kind(object(), "Anyone") == "initial"


# ---------------------------------------------------------------------------
# where it sits in the ladder
# ---------------------------------------------------------------------------

def test_returning_replaces_touch_one_only(past_event):
    assert ladder.step_for(past_event, "DigitalOcean", 0).kind == "returning"
    assert ladder.step_for(past_event, "Postman", 0).kind == "initial"
    # Every later touch reads the same to a returning sponsor as to anyone else.
    assert ladder.step_for(past_event, "DigitalOcean", 1).kind == "nudge"
    assert ladder.step_for(past_event, "DigitalOcean", 2).kind == "new_angle"
    assert ladder.step_for(past_event, "DigitalOcean", 3).kind == "last_touch"
    assert ladder.step_for(past_event, "DigitalOcean", 4) is None


def test_returning_is_touch_one_and_day_zero():
    """It is a variant of the first touch, not a fifth touch bolted on. If it
    ever landed at a different touch number the 4-touch cap would be wrong."""
    step = ladder.BY_KIND["returning"]
    assert (step.touch, step.day) == (1, 0)
    assert ladder.RETURNING.kind not in {s.kind for s in ladder.LADDER}
    assert set(ladder.FIRST_KINDS) == {"initial", "returning"}


def test_the_touch_cap_still_counts_a_returning_email(db, past_event):
    """A returning email is a touch. If the cap trigger did not list the new
    kind, a returning sponsor could be mailed five times."""
    db.execute("INSERT INTO prospects (id, company, email, status, touches) "
               "VALUES ('P90','DigitalOcean','a@do.com','sent',4)")
    with pytest.raises(Exception, match="touch cap"):
        db.execute("INSERT INTO drafts (prospect_id, kind, subject, body) "
                   "VALUES ('P90','returning','x','y')")


# ---------------------------------------------------------------------------
# the email itself
# ---------------------------------------------------------------------------

def test_the_returning_prompt_forbids_the_who_we_are_paragraph():
    text = prompts.DRAFT_STRUCTURE_RETURNING
    assert "Do NOT explain who we are" in text
    assert "THE RELATIONSHIP" in text
    # and the cold structure keeps the paragraph the returning one drops
    assert "WHY THEM" in prompts.DRAFT_STRUCTURE_COLD
    assert "WHY THEM" not in text


def test_the_drafter_is_told_not_to_invent_a_change(past_event):
    """"We are doing it again" is a weak ask, so the model will want a hook.
    With nothing on record it must use the edition number, not imagination."""
    past_event.whats_new = ""
    block = drafting._extra_context(past_event, "returning", None,
                                    _thin_research(False))
    assert "do not" in block.lower() and "invent" in block.lower()

    past_event.whats_new = "A hardware track, and prize money doubled."
    block = drafting._extra_context(past_event, "returning", None,
                                    _thin_research(False))
    assert "hardware track" in block


def test_only_the_returning_kind_gets_the_relationship_block(past_event):
    for kind in ("initial", "nudge", "new_angle", "last_touch"):
        block = drafting._extra_context(past_event, kind, None, _thin_research(False))
        assert "past-sponsor list" not in block


def _thin_research(thin: bool):
    from sponsy.models import Research
    return Research(prospect_id="P01", one_liner="does things",
                    confidence=0.2 if thin else 0.9)


# ---------------------------------------------------------------------------
# the verifier
# ---------------------------------------------------------------------------

def _draft(kind: str, body: str, claims=None) -> Draft:
    return Draft(prospect_id="P01", kind=kind, subject="DotSlash 10.0 sponsorship",
                 body=body, claims=claims or [], suggested_tier="", confidence=0.9)


RETURNING_BODY = (
    "Hi Devi,\n\n"
    "You backed a previous edition of Acme Analytics and it made a real "
    "difference to what the teams could build. We are running it again on "
    "17 October and would like you back.\n\n"
    "Since you know how it runs, I will skip the introduction. If parts of "
    "last time were worth more than others, tell me which and we will build "
    "the package around that. Happy to sponsor on whichever terms suit.")

COLD_LOOKING_BODY = (
    "Hi Devi,\n\n"
    "Acme Analytics runs a student programme, which is why we are writing to "
    "you rather than to a general list. We would like to invite you to "
    "sponsor our hackathon on 17 October, and we can shape the package "
    "around whatever is worth most to your team this time around.")


def test_a_returning_email_needs_no_research_claims(event, voice):
    """Its hook is our own record of the relationship, not something research
    turned up, so zero claims is correct rather than lazy."""
    ok, detail = verify.verify_draft(_draft("returning", RETURNING_BODY), [], event,
                                     company="Acme Analytics",
                                     contact_name="Devi Sharma", voice=voice)
    assert detail["checks"]["has_claims"]
    assert detail["checks"]["mentions_relationship"]
    assert ok, [k for k, v in detail["checks"].items() if not v]


def test_a_returning_email_that_forgets_the_relationship_is_rejected(event, voice):
    """The whole point of the variant. If the model writes the cold pitch
    under this kind, the sponsor gets "hello, we are ACM SVNIT" with their own
    logo already on our site."""
    ok, detail = verify.verify_draft(_draft("returning", COLD_LOOKING_BODY), [],
                                     event, company="Acme Analytics",
                                     contact_name="Devi Sharma", voice=voice)
    assert not detail["checks"]["mentions_relationship"]
    assert not ok
    assert "never says so" in verify.failure_feedback(detail)


def test_the_relationship_check_does_not_touch_other_kinds(event, voice):
    """A cold email has no relationship to mention, so the check must pass
    vacuously rather than failing every initial draft ever written."""
    for kind in ("initial", "nudge", "new_angle", "last_touch", "needs_info"):
        _, detail = verify.verify_draft(_draft(kind, COLD_LOOKING_BODY, ["x"]), [],
                                        event, company="Acme Analytics",
                                        contact_name="Devi Sharma", voice=voice)
        assert detail["checks"]["mentions_relationship"], kind


# ---------------------------------------------------------------------------
# what actually goes in the envelope
# ---------------------------------------------------------------------------

def test_a_returning_sponsor_gets_the_brochure_and_the_full_deck(cfg, event, voice,
                                                                 prospect, tmp_path):
    """They are being asked to decide again, not merely reminded: the tiers
    change between editions, so the deck and the brochure both go."""
    brochure = tmp_path / "brochure.pdf"
    brochure.write_bytes(b"%PDF-1.4 fake")
    cfg.brochure = brochure

    assert message_mod.attachment_for(cfg, "returning") == brochure
    assert message_mod.attachment_for(cfg, "nudge") is None

    event.what_we_offer = [{"title": "Challenge track", "detail": "your own problem"},
                           {"title": "Resume book", "detail": "every attendee"}]
    body = "Hi Devi,\n\nYou backed us before."

    def html_for(kind):
        msg = message_mod.build_message(cfg, event, voice, prospect, "Subject",
                                        body, kind=kind)
        return "".join(p.get_content() for p in msg.walk()
                       if p.get_content_type() == "text/html")

    returning, initial, nudge = html_for("returning"), html_for("initial"), html_for("nudge")
    # treated as a first touch, not a follow-up: same deck as `initial`...
    assert "What We Offer Our Sponsors" in returning
    assert len(returning) == len(initial)
    # ...and strictly more than a nudge, which never repeats the deck
    assert "What We Offer Our Sponsors" not in nudge
    assert len(returning) > len(nudge)

    # the real call path: the caller resolves the attachment from the kind
    msg = message_mod.build_message(
        cfg, event, voice, prospect, "Subject", body, kind="returning",
        attachment=message_mod.attachment_for(cfg, "returning"))
    assert any(p.get_filename() == "brochure.pdf" for p in msg.walk())


def test_the_card_names_the_variant(db, cfg, event, voice, notifier, prospect):
    """The approver should be able to see which premise they are agreeing to
    before tapping Send, because the premise is a claim about our own history."""
    db.execute("INSERT INTO drafts (id, prospect_id, kind, subject, body, claims, "
               "checks, verified, confidence, created_at) VALUES "
               "(1,'P01','returning','S','Hi Devi,\n\nYou backed us before.',"
               "'[]','{}',1,0.9,datetime('now'))")
    row = store.pending_drafts(db)[0]
    approve.register(event, voice, TODAY)
    assert "returning sponsor" in approve.draft_message(row)


# ---------------------------------------------------------------------------
# migrating a database that predates the variant
# ---------------------------------------------------------------------------

def _old_schema() -> str:
    """The drafts table exactly as it stood before `returning` existed."""
    import re
    return re.sub(
        r"kind          TEXT CHECK \(kind IN \([^)]*\)\)",
        "kind          TEXT CHECK (kind IN ('initial','nudge','new_angle',"
        "'last_touch','needs_info'))",
        store.SCHEMA, count=1)


def test_an_existing_database_is_migrated_without_losing_drafts(tmp_path):
    """SQLite cannot ALTER a CHECK, so the table is rebuilt. Anyone who has
    been running this already has drafts in it, and a migration that drops
    them is worse than no variant at all."""
    from sponsy.state import Database

    db = Database(tmp_path / "old.db")
    db.executescript(_old_schema())
    for table in store.LATER_COLUMNS:
        store._add_later_columns(db, table)
    db.execute("INSERT INTO prospects (id, company, email, status) "
               "VALUES ('P01','Acme','a@acme.com','sent')")
    db.execute("INSERT INTO drafts (id, prospect_id, kind, subject, body, verdict, "
               "sent_at) VALUES (1,'P01','initial','S','B','approved','2026-01-01')")
    db.execute("INSERT INTO drafts (id, prospect_id, kind, subject, body) "
               "VALUES (2,'P01','nudge','S2','B2')")

    with pytest.raises(Exception, match="CHECK constraint"):
        db.execute("INSERT INTO drafts (prospect_id, kind, subject, body) "
                   "VALUES ('P01','returning','x','y')")

    assert store.migrate_draft_kinds(db) is True

    rows = {r["id"]: r for r in db.query("SELECT * FROM drafts ORDER BY id")}
    assert len(rows) == 2
    assert rows[1]["verdict"] == "approved" and rows[1]["sent_at"] == "2026-01-01"
    assert rows[2]["kind"] == "nudge"
    db.execute("INSERT INTO drafts (id, prospect_id, kind, subject, body) "
               "VALUES (3,'P01','returning','x','y')")


def test_the_migration_puts_the_guardrails_back(tmp_path):
    """Dropping a table drops its triggers. If the rebuild did not restore
    them the approval gate would be gone, which is the one failure this
    project cannot have -- and it would be silent."""
    from sponsy.state import Database

    db = Database(tmp_path / "old.db")
    db.executescript(_old_schema())
    for table in store.LATER_COLUMNS:
        store._add_later_columns(db, table)
    before = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}

    store.migrate_draft_kinds(db)

    after = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert before == after and len(after) == 7
    assert {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name LIKE 'ix_drafts%'")} == {"ix_drafts_prospect", "ix_drafts_pending"}

    # the gate itself, not just its name in the catalogue
    db.execute("INSERT INTO prospects (id, company, email, status) "
               "VALUES ('P01','Acme','a@acme.com','new')")
    db.execute("INSERT INTO drafts (id, prospect_id, kind, subject, body) "
               "VALUES (1,'P01','returning','S','B')")
    with pytest.raises(Exception, match="no send without a human verdict"):
        db.execute("UPDATE drafts SET sent_at='2026-02-02' WHERE id=1")


def test_migrating_twice_is_a_no_op(tmp_path):
    from sponsy.state import Database

    db = Database(tmp_path / "fresh.db")
    store.ensure_schema(db)
    assert store.migrate_draft_kinds(db) is False
    store.ensure_schema(db)                       # the normal startup path
    assert store.migrate_draft_kinds(db) is False


# ---------------------------------------------------------------------------
# regressions from the first live returning draft
# ---------------------------------------------------------------------------

# Written by Gemini for DigitalOcean on the first live run, kept verbatim. It
# asks plainly -- "we want to invite you to support this milestone edition" --
# and the verifier rejected it, because ASK_WORDS only matched "your support"
# and "support us". A returning email phrases the ask as a return, so the
# narrow list would have failed most of them.
LIVE_RETURNING_DRAFT = (
    "Hi there,\n\n"
    "DigitalOcean supported a previous edition of our hackathon, helping "
    "student developers bring their ideas to life on the cloud. We are "
    "incredibly grateful for your past backing and would love to welcome you "
    "back as we prepare for our tenth edition.\n\n"
    "DotSlash 10.0 will take place on 20-21 February 2027 as a 36-hour "
    "continuous building event. We expect student innovators from across "
    "India to gather and create next-generation solutions, and we want to "
    "invite you to support this milestone edition.\n\n"
    "We can offer API workshops, custom challenges, or direct mentorship "
    "opportunities to connect your team with our builders. We are happy to "
    "tailor a package around what worked best for you last time, or focus on "
    "your updated Hatch startup program to help transition the best student "
    "AI projects into early-stage startups.")


def test_the_first_live_returning_draft_passes(event, voice):
    """Whatever else changes, this real email must keep verifying."""
    ok, detail = verify.verify_draft(
        _draft("returning", LIVE_RETURNING_DRAFT,
               ["DigitalOcean runs the Hatch startup program."]),
        [], event, company="DigitalOcean", contact_name="", voice=voice)
    failed = [k for k, v in detail["checks"].items() if not v]
    assert failed == ["claims_traceable"], failed   # no excerpts passed in here
    assert detail["checks"]["has_ask"]
    assert detail["checks"]["mentions_relationship"]


@pytest.mark.parametrize("phrase", [
    "we want to invite you to support this milestone edition",
    "we would love to have you back for the tenth edition",
    "we hope you will come back as a sponsor",
    "would you like to be part of the tenth edition",
    "we would love to welcome you back this February",
])
def test_a_return_worded_ask_counts_as_an_ask(phrase):
    assert any(w in phrase for w in verify.ASK_WORDS), phrase


@pytest.mark.parametrize("body", [
    "We are running our hackathon in February. It will be our tenth edition.",
    "Thank you for reading. Our event has grown every year since 2016.",
])
def test_an_email_that_never_asks_is_still_caught(body):
    """The widened list must not turn the check off: an email that only
    describes the event and never asks for anything is the failure this
    exists to catch."""
    assert not any(w in body.lower() for w in verify.ASK_WORDS), body
