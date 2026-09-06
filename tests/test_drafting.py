"""The closed-world drafter.

Two things are being checked here. First that the prompt actually carries the
closed world -- the research, the excerpts, our tiers -- and nothing else.
Second that it carries the *voice*: the sender's style block, their real
emails, and the corrections they have made before. That second half is what
makes the output read like a person, so it is worth asserting rather than
hoping.
"""

from __future__ import annotations

import json

import pytest

from sponsy import drafting, store
from sponsy.models import Draft, Research

from .conftest import SOURCES

RESEARCH = Research(
    prospect_id="P01",
    one_liner="Acme Analytics builds a data analytics workbench for product teams.",
    student_relevance="Runs a student programme giving free licences to college "
                      "societies.",
    prior_campus_sponsorship="Has sponsored college hackathons with challenge "
                             "prizes.",
    recent_signal="Launched a free tier for learners.",
    hook="Their student programme already gives free licences to college "
         "societies.",
    company_domain="acme-analytics.example.com",
    sources_used=[e.url for e in SOURCES],
    confidence=0.85)

THIN = Research(prospect_id="P01", one_liner="A consulting firm.", confidence=0.2)


def _draft(db, cfg, llm, event, voice, prospect, **kwargs):
    return drafting.draft_email(llm, cfg, db, prospect, kwargs.pop("research",
                                                                   RESEARCH),
                                SOURCES, event, voice, kwargs.pop("kind", "initial"),
                                **kwargs)


def _prompt(llm) -> str:
    return llm.calls[-1]["messages"][0]["text"]


def _system(llm) -> str:
    return llm.calls[-1]["system"]


# ---------------------------------------------------------------------------
# the draft itself
# ---------------------------------------------------------------------------

def test_a_draft_comes_back_whole(cfg, db, llm, event, voice, prospect):
    draft, cost = _draft(db, cfg, llm, event, voice, prospect)
    assert isinstance(draft, Draft)
    assert draft.prospect_id == "P01" and draft.kind == "initial"
    assert draft.subject and draft.body and draft.claims
    assert draft.confidence == RESEARCH.confidence
    assert cost >= 0


def test_it_opens_with_the_greeting_and_stops_before_the_signoff(
        cfg, db, llm, event, voice, prospect):
    """The model writes the personalised opening only. render.py adds the
    sign-off and the contact block, so a copy in the prose would appear
    twice in the finished email."""
    draft, _ = _draft(db, cfg, llm, event, voice, prospect)
    assert draft.body.startswith(voice.greeting_for(prospect.contact_name))
    assert voice.signoff.strip().splitlines()[0].lower() not in draft.body.lower()
    assert event.reply_to not in draft.body


def test_a_named_group_is_greeted_by_its_own_name(cfg, db, llm, event, voice,
                                                  prospect):
    prospect.contact_name = "DevRel team"
    draft, _ = _draft(db, cfg, llm, event, voice, prospect)
    assert draft.body.startswith("Hi DevRel team,")


def test_a_bare_role_mailbox_is_greeted_as_a_team(cfg, db, llm, event, voice,
                                                  prospect):
    """We have an address and no human behind it; greeting it by the mailbox
    name would be worse than greeting the team."""
    prospect.contact_name = "sponsorships@acme.com"
    draft, _ = _draft(db, cfg, llm, event, voice, prospect)
    assert draft.body.startswith("Hi team,")


def test_markdown_and_a_stray_subject_line_are_stripped(cfg, db, llm, event,
                                                        voice, prospect):
    from sponsy.llm import FakeLLM, LLMResponse
    scripted = LLMResponse(text=json.dumps({
        "subject": "a quiet subject",
        "body": "Subject: leave me out\nHi Devi,\n\n- **bold** bullet\n\n"
                "# heading\n\n\n\nThanks,\nRudraksh",
        "claims": [], "suggested_tier": "Gold"}))
    draft, _ = _draft(db, cfg, FakeLLM(cfg, scripted=[scripted]), event, voice,
                      prospect)
    assert "Subject:" not in draft.body
    assert "**" not in draft.body and "- " not in draft.body
    assert "# heading" not in draft.body
    assert "\n\n\n" not in draft.body


def test_a_doubled_signoff_is_collapsed(cfg, db, event, voice, prospect):
    """Models given a sign-off sometimes use it twice."""
    from sponsy.llm import FakeLLM, LLMResponse
    body = ("Hi Devi,\n\nA line.\n\nThanks,\nRudraksh\nSponsorship Lead, ACM "
            "SVNIT\n\nThanks,\nRudraksh\nSponsorship Lead, ACM SVNIT")
    scripted = LLMResponse(text=json.dumps({"subject": "s", "body": body,
                                            "claims": [], "suggested_tier": ""}))
    draft, _ = _draft(db, cfg, FakeLLM(cfg, scripted=[scripted]), event, voice,
                      prospect)
    assert draft.body.count("Thanks,") == 1


def test_claims_are_cleaned_and_a_bare_string_is_accepted(cfg, db, event, voice,
                                                          prospect):
    from sponsy.llm import FakeLLM, LLMResponse
    scripted = LLMResponse(text=json.dumps({
        "subject": "s", "body": "Hi Devi,\n\nBody.\n\nThanks,\nRudraksh",
        "claims": "one claim only", "suggested_tier": "Gold"}))
    draft, _ = _draft(db, cfg, FakeLLM(cfg, scripted=[scripted]), event, voice,
                      prospect)
    assert draft.claims == ["one claim only"]


def test_the_subject_is_clipped(cfg, db, event, voice, prospect):
    from sponsy.llm import FakeLLM, LLMResponse
    scripted = LLMResponse(text=json.dumps({
        "subject": "x" * 300, "body": "Hi Devi,\n\nBody.\n\nThanks,\nRudraksh",
        "claims": [], "suggested_tier": ""}))
    draft, _ = _draft(db, cfg, FakeLLM(cfg, scripted=[scripted]), event, voice,
                      prospect)
    assert len(draft.subject) == 120


# ---------------------------------------------------------------------------
# the closed world
# ---------------------------------------------------------------------------

def test_the_prompt_carries_the_research_and_the_excerpts(cfg, db, llm, event,
                                                          voice, prospect):
    _draft(db, cfg, llm, event, voice, prospect)
    sent = _prompt(llm)
    assert RESEARCH.hook in sent
    for excerpt in SOURCES:
        assert excerpt.url in sent
        assert excerpt.text[:60] in sent


def test_the_prompt_carries_our_tiers_and_track_record(cfg, db, llm, event, voice,
                                                       prospect):
    _draft(db, cfg, llm, event, voice, prospect)
    sent = _prompt(llm)
    for tier in event.tiers:
        assert tier.name in sent
    assert event.track_record[0] in sent
    assert event.reply_to in sent


def test_the_word_limit_comes_from_the_voice(cfg, db, llm, event, voice, prospect):
    voice.max_words = 99
    _draft(db, cfg, llm, event, voice, prospect)
    assert "99 words" in _system(llm)


# ---------------------------------------------------------------------------
# the voice
# ---------------------------------------------------------------------------

def test_the_prompt_carries_the_style_block_and_the_real_emails(
        cfg, db, llm, event, voice, prospect):
    _draft(db, cfg, llm, event, voice, prospect)
    sent = _prompt(llm)
    assert voice.sender_name in sent
    assert voice.tone_notes[0] in sent
    assert voice.phrases_i_use[0] in sent
    assert voice.phrases_i_avoid[0] in sent
    assert voice.sample_emails[0].split("\n")[0] in sent
    assert "Greet them as: Hi Devi," in sent
    assert voice.signoff.strip() in sent


def test_with_no_corrections_there_is_no_fewshot_block(cfg, db, llm, event, voice,
                                                       prospect):
    assert drafting.fewshot_block(db, voice) == ""
    _draft(db, cfg, llm, event, voice, prospect)
    assert "what they sent instead" not in _prompt(llm)


def test_a_correction_becomes_a_few_shot_pair(cfg, db, llm, event, voice, prospect):
    """This is the part that improves with use: every [Edit] teaches the next
    draft what the sender actually sends."""
    draft_id = store.insert_draft(db, Draft("P01", "initial", "Old subject",
                                            "Hi Devi,\n\nThe old body.\n\nThanks"),
                                  run_id=None)
    store.record_correction(db, draft_id, "New subject",
                            "Hi Devi,\n\nThe body they actually sent.\n\nThanks")
    block = drafting.fewshot_block(db, voice)
    assert "The old body." in block and "The body they actually sent." in block
    _draft(db, cfg, llm, event, voice, prospect)
    assert "The body they actually sent." in _prompt(llm)


def test_a_redo_instruction_is_carried_as_a_correction_too(cfg, db, llm, event,
                                                           voice, prospect):
    draft_id = store.insert_draft(db, Draft("P01", "initial", "s", "old body"),
                                  run_id=None)
    store.record_redo(db, draft_id, Draft("P01", "initial", "s2", "new body"),
                      "make it shorter")
    block = drafting.fewshot_block(db, voice)
    assert "make it shorter" in block


def test_only_the_last_few_corrections_are_shown(cfg, db, voice, prospect):
    for i in range(6):
        draft_id = store.insert_draft(db, Draft("P01", "initial", "s",
                                                "body {}".format(i)), run_id=None)
        store.record_correction(db, draft_id, "s", "corrected {}".format(i))
    block = drafting.fewshot_block(db, voice, limit=3)
    assert "corrected 5" in block and "corrected 2" not in block


def test_fewshot_block_survives_a_missing_database(voice):
    assert drafting.fewshot_block(None, voice) == ""


# ---------------------------------------------------------------------------
# context that changes with the touch
# ---------------------------------------------------------------------------

def test_thin_research_asks_for_a_shorter_email(cfg, db, llm, event, voice,
                                                prospect):
    _draft(db, cfg, llm, event, voice, prospect, research=THIN)
    assert "research on this company is thin" in _prompt(llm)


def test_good_research_does_not(cfg, db, llm, event, voice, prospect):
    _draft(db, cfg, llm, event, voice, prospect)
    assert "research on this company is thin" not in _prompt(llm)


@pytest.mark.parametrize("kind,fragment", [
    ("initial", "Lead with the researched hook"),
    ("nudge", "Add exactly one new detail"),
    ("new_angle", "Change the frame"),
    ("last_touch", "will not write again"),
    ("needs_info", "They replied with a question"),
])
def test_each_touch_gets_its_own_instruction(cfg, db, llm, event, voice, prospect,
                                             kind, fragment):
    _draft(db, cfg, llm, event, voice, prospect, kind=kind)
    assert fragment in _prompt(llm)
    assert "Kind: {}".format(kind) in _prompt(llm)


def test_a_follow_up_may_cite_progress_we_have_made(cfg, db, llm, event, voice,
                                                    prospect):
    event.registrations_so_far = 42
    event.confirmed_sponsors = ["Postman"]
    _draft(db, cfg, llm, event, voice, prospect, kind="nudge")
    sent = _prompt(llm)
    assert "42 teams have registered" in sent
    assert "confirmed sponsors so far: Postman" in sent


def test_an_initial_touch_may_not(cfg, db, llm, event, voice, prospect):
    event.registrations_so_far = 42
    _draft(db, cfg, llm, event, voice, prospect, kind="initial")
    assert "42 teams have registered" not in _prompt(llm)


def test_previous_emails_are_shown_so_the_nudge_does_not_repeat_them(
        cfg, db, llm, event, voice, prospect):
    previous = [{"kind": "initial", "subject": "First one",
                 "body": "The opening line we already used."}]
    _draft(db, cfg, llm, event, voice, prospect, kind="nudge", previous=previous)
    assert "The opening line we already used." in _prompt(llm)
    assert "do not repeat their content" in _prompt(llm)


# ---------------------------------------------------------------------------
# retries and redos
# ---------------------------------------------------------------------------

def test_verifier_feedback_is_handed_back_to_the_model(cfg, db, llm, event, voice,
                                                       prospect):
    _draft(db, cfg, llm, event, voice, prospect,
           feedback="- Not supported by the sources: they sponsor 47 hackathons")
    sent = _prompt(llm)
    assert "rejected by the verifier" in sent
    assert "47 hackathons" in sent
    assert "Do not add new claims" in sent


def test_a_redo_carries_the_instruction_and_the_previous_draft(cfg, db, llm, event,
                                                               voice, prospect):
    _draft(db, cfg, llm, event, voice, prospect,
           instruction="shorter, and ask one question",
           prev_draft={"subject": "The old subject", "body": "The old body."})
    sent = _prompt(llm)
    assert "asked for this change" in sent
    assert "shorter, and ask one question" in sent
    assert "The old subject" in sent and "The old body." in sent


def test_an_instruction_without_a_previous_draft_still_lands(cfg, db, llm, event,
                                                             voice, prospect):
    _draft(db, cfg, llm, event, voice, prospect, instruction="be blunter")
    assert "be blunter" in _prompt(llm)


# ---------------------------------------------------------------------------
# previous_touches
# ---------------------------------------------------------------------------

def test_previous_touches_returns_only_sent_drafts_oldest_first(db, prospect):
    from datetime import date

    first = store.insert_draft(db, Draft("P01", "initial", "One", "first body"),
                               run_id=None)
    store.mark_sent(db, first, date(2026, 9, 5), message_id="<a>")
    second = store.insert_draft(db, Draft("P01", "nudge", "Two", "second body"),
                                run_id=None)
    store.mark_sent(db, second, date(2026, 9, 8), message_id="<b>")
    store.insert_draft(db, Draft("P01", "new_angle", "Three", "unsent body"),
                       run_id=None)

    out = drafting.previous_touches(db, "P01")
    assert [t["subject"] for t in out] == ["One", "Two"]


def test_previous_touches_is_empty_for_a_fresh_prospect(db, prospect):
    assert drafting.previous_touches(db, "P01") == []
