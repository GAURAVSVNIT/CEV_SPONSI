"""The verifier: the checklist, groundedness, and the salutation check.

Most of these are ported from the ancestor's suite, because they are each a
bug that actually happened on a live run: an invented statistic, a name lifted
from a blog byline, three hundred words of flattery, a claim that was
plausible and absent.
"""

from __future__ import annotations

import pytest

from sponsy import humanize, verify
from sponsy.models import Draft

from .conftest import SOURCES

# A draft that passes every check: contractions, varied sentences, the voice's
# own sign-off, one tier named, one grounded claim.
GOOD_BODY = (
    "Hi Devi,\n\n"
    "Acme Analytics runs a student programme that gives free licences to "
    "college societies.\n\n"
    "That's the reason I'm writing. We run Example Hackathon on 2026-10-17 with "
    "12 colleges in Gujarat, and a licence giveaway would land well with the "
    "teams building on the day.\n\n"
    "Our Gold tier is Rs 20,000 and covers the stage backdrop. Does that fit?\n\n"
    "Thanks,\nRudraksh\nSponsorship Lead, ACM SVNIT")

GOOD_CLAIMS = ["Acme Analytics runs a student programme that gives free "
               "licences to college societies"]


def _draft(body: str = GOOD_BODY, claims=None,
           subject: str = "Example Hackathon, Gold tier", kind: str = "initial") -> Draft:
    return Draft(prospect_id="P01", kind=kind, subject=subject, body=body,
                 claims=list(GOOD_CLAIMS if claims is None else claims))


def _verify(draft, cfg, event, llm, voice=None, **kwargs):
    kwargs.setdefault("company", "Acme Analytics")
    kwargs.setdefault("contact_name", "Devi Sharma")
    return verify.verify_draft(draft, SOURCES, event, cfg=cfg, llm=llm,
                               voice=voice, **kwargs)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_a_good_draft_passes(cfg, event, llm, voice):
    ok, detail = _verify(_draft(), cfg, event, llm, voice)
    assert ok, [k for k, v in detail["checks"].items() if not v]
    assert detail["soft_failures"] == []
    assert detail["lint"]["score"] >= humanize.PASS_SCORE


def test_the_detail_carries_what_the_card_prints(cfg, event, llm, voice):
    _ok, detail = _verify(_draft(), cfg, event, llm, voice)
    assert set(detail) >= {"checks", "claims", "words", "ungrounded", "sources",
                           "greeting", "lint", "soft_failures", "max_words"}
    assert detail["sources"] == 2
    assert detail["words"] == len(GOOD_BODY.split())
    assert detail["claims"][0]["grounded"] is True


# ---------------------------------------------------------------------------
# salutations -- the failure groundedness can never see
# ---------------------------------------------------------------------------

def test_a_name_lifted_from_the_sources_is_caught(cfg, event, llm, voice):
    """Regression: a live run greeted "Hi Smit" -- the byline on a blog post on
    the prospect's own site -- while the contact was a team inbox."""
    body = GOOD_BODY.replace("Hi Devi,", "Hi Smit,")
    ok, detail = _verify(_draft(body, claims=[]), cfg, event, llm, voice,
                         contact_name="DevRel team")
    assert not ok
    assert detail["checks"]["greets_our_contact"] is False
    assert "Smit" in detail["greeting"]


@pytest.mark.parametrize("greeting,contact,expected", [
    ("Hi Devi,", "Devi Sharma", True),
    ("Dear Devi Sharma,", "Devi Sharma", True),
    ("Hi there,", "", True),
    ("Hello DevRel team,", "DevRel team", True),
    ("Hi team,", "", True),
    ("Hi Smit,", "Devi", False),
    ("Hi Smit,", "", False),
])
def test_greeting_check(greeting, contact, expected):
    ok, why = verify.greeting_ok(greeting + "\n\nbody text here.", contact)
    assert ok is expected, why


def test_a_draft_with_no_greeting_is_not_penalised():
    assert verify.greeting_ok("Acme Analytics runs a programme.", "Devi")[0]


# ---------------------------------------------------------------------------
# groundedness
# ---------------------------------------------------------------------------

def test_an_invented_statistic_is_caught(cfg, event, llm, voice):
    claim = "Acme Analytics sponsored 47 college hackathons last year"
    body = GOOD_BODY.replace("Hi Devi,", "Hi Devi,\n\n" + claim + ".")
    ok, detail = _verify(_draft(body, claims=[claim]), cfg, event, llm, voice)
    assert not ok
    assert not detail["checks"]["claims_traceable"]
    assert "47" in detail["claims"][0]["why"]


def test_a_plausible_but_absent_claim_is_caught(cfg, event, llm, voice):
    # Entirely believable of a company like this, and nowhere in the sources.
    claim = "Acme Analytics sponsors a scholarship fund for women in engineering"
    ok, detail = _verify(_draft(claims=[claim]), cfg, event, llm, voice)
    assert not ok
    assert not detail["checks"]["claims_traceable"]
    assert detail["ungrounded"] == [claim]


def test_our_own_event_facts_are_not_claims_about_them(cfg, event, llm, voice):
    """Models list "the event is on 2026-10-17" among their claims. Grounding
    that against the prospect's website is nonsense: it is ours."""
    ok, why = verify.grounded("Example Hackathon runs on 2026-10-17 with 12 "
                              "colleges in Gujarat", SOURCES, cfg=cfg, llm=llm,
                              allowed_numbers=verify.allowed_numbers_for(event),
                              own_facts=verify.own_facts(event))
    assert ok and "our own event facts" in why


def test_a_tier_amount_is_an_allowed_number(event):
    assert "20000" in verify.allowed_numbers_for(event)


def test_grounding_needs_at_least_one_source(cfg, llm):
    ok, _why = verify.grounded("anything at all", [], cfg=cfg, llm=llm)
    assert not ok


def test_grounding_can_be_turned_off(cfg, llm):
    cfg.grounding_mode = "off"
    ok, why = verify.grounded("total nonsense", [], cfg=cfg, llm=llm)
    assert ok and "disabled" in why


def test_sentence_final_punctuation_does_not_break_grounding():
    """Regression: "education." never matched "education", which made every
    legitimate closing claim look ungrounded."""
    assert "education" in verify._tokens("free student education.")
    assert "co-founder" in verify._tokens("their co-founder said")
    source = " ".join(e.text for e in SOURCES)
    assert verify.overlap("Acme Analytics gives free licences to college "
                          "societies.", source) >= 0.7


def test_the_judge_is_asked_on_the_cheap_model(cfg, db, llm, event):
    """The groundedness question is yes/no over a page of text; it must not
    burn the expensive model."""
    cfg.llm_provider = "anthropic"
    cfg.anthropic_api_key = "x"
    # A claim in the ambiguous middle: too far from the sources to wave
    # through on token overlap, too close to reject outright. That band is the
    # only one that costs a model call.
    verify.grounded("Acme Analytics helps teams monitor dashboards during "
                    "hackathons", SOURCES, cfg=cfg, llm=llm, db=db)
    assert llm.calls, "an ambiguous claim should have reached the judge"
    assert llm.calls[-1]["model"] == cfg.resolved_models()[1]


# ---------------------------------------------------------------------------
# the rest of the checklist
# ---------------------------------------------------------------------------

def test_three_hundred_words_of_flattery_is_caught(cfg, event, llm, voice):
    body = GOOD_BODY + "\n" + ("Your work is genuinely impressive. " * 40)
    ok, detail = _verify(_draft(body, claims=[]), cfg, event, llm, voice)
    assert not ok and not detail["checks"]["length"]


def test_the_length_limit_comes_from_the_voice(cfg, event, llm, voice):
    voice.max_words = 20
    _ok, detail = _verify(_draft(), cfg, event, llm, voice)
    assert detail["max_words"] == 20 and not detail["checks"]["length"]


@pytest.mark.parametrize("body,failing", [
    ("Hi Devi,\n\nI hope this email finds you well. Acme Analytics runs a "
     "student programme. Our Gold tier is Rs 20,000 and covers the backdrop "
     "and a stall on finals day for your team.", "no_filler"),
    ("Hi [NAME],\n\nAcme Analytics runs a student programme, and our Gold tier "
     "is Rs 20,000 which covers the backdrop and a stall on the finals day.",
     "no_placeholder"),
    ("Hi Devi,\n\nAcme Analytics is a world-class team and our Gold tier is Rs "
     "20,000, which covers the backdrop and a stall on the day of the finals.",
     "no_superlatives"),
    # Describes the event at length and never actually asks for anything.
    ("Hi Devi,\n\nAcme Analytics runs a student programme that gives free "
     "licences to college societies. Our event runs in October with twelve "
     "colleges taking part over a single long day of judging.", "has_ask"),
    ("Hi Devi,\n\nOur Gold tier is Rs 20,000.", "not_empty"),
])
def test_checklist_items(cfg, event, llm, body, failing):
    ok, detail = _verify(_draft(body, claims=[]), cfg, event, llm)
    assert not ok
    assert detail["checks"][failing] is False


def test_the_company_must_be_named(cfg, event, llm, voice):
    body = GOOD_BODY.replace("Acme Analytics", "The company")
    _ok, detail = _verify(_draft(body, claims=[]), cfg, event, llm, voice)
    assert detail["checks"]["names_company"] is False


@pytest.mark.parametrize("subject", ["Hi", "x" * 80])
def test_subject_length_is_checked(cfg, event, llm, voice, subject):
    _ok, detail = _verify(_draft(subject=subject), cfg, event, llm, voice)
    assert not detail["checks"]["subject_ok"]


@pytest.mark.parametrize("kind", ["last_touch", "needs_info", "nudge"])
def test_a_follow_up_needs_no_claims(cfg, event, llm, voice, kind):
    _ok, detail = _verify(_draft(claims=[], kind=kind), cfg, event, llm, voice)
    assert detail["checks"]["has_claims"] is True


def test_an_initial_touch_does_need_claims(cfg, event, llm, voice):
    _ok, detail = _verify(_draft(claims=[]), cfg, event, llm, voice)
    assert detail["checks"]["has_claims"] is False


@pytest.mark.parametrize("phrase", [
    "fantastic", "outstanding", "world-class", "mutually beneficial",
    "invaluable", "we are confident this",
])
def test_flattery_words_are_rejected(cfg, event, llm, phrase):
    """A live draft called a programme "a fantastic resource" and passed,
    because the word was not on the list. It is now."""
    body = ("Hi Devi,\n\nAcme Analytics runs a {} student programme and our "
            "Gold tier is Rs 20,000, covering the backdrop and a stall on the "
            "day of the finals.").format(phrase)
    _ok, detail = _verify(_draft(body, claims=[]), cfg, event, llm)
    assert detail["checks"]["no_superlatives"] is False


# ---------------------------------------------------------------------------
# sounds_human: soft by default, hard with HUMANIZE_STRICT=1
# ---------------------------------------------------------------------------

ROBOTIC_BODY = (
    "Hi Devi,\n\n"
    "I hope this email finds you well! I am reaching out to explore a mutually "
    "beneficial partnership.\n\n"
    "Acme Analytics runs a student programme that gives free licences to "
    "college societies.\n\n"
    "Our Gold tier is Rs 20,000 and covers the stage backdrop.\n\n"
    "Thanks,\nRudraksh\nSponsorship Lead, ACM SVNIT")

# Passes every hard check -- grounded, named, one tier, right greeting, right
# length -- and still reads like a machine wrote it: no contractions, three
# sentences opening with "I", a pair of em-dashes. This is the draft the soft
# failure exists for.
STIFF_BODY = (
    "Hi Devi,\n\n"
    "I noted that Acme Analytics runs a student programme which gives free "
    "licences to college societies across the country.\n\n"
    "I would like to propose a sponsorship arrangement for our competition "
    "which takes place in the middle of October this year.\n\n"
    "I would value a short call with your team at some point next week — very "
    "soon — to discuss the details.\n\n"
    "Our Gold tier is Rs 20,000 and it covers the stage backdrop at the "
    "venue.\n\n"
    "Thanks,\nRudraksh\nSponsorship Lead, ACM SVNIT")


def test_the_stiff_draft_fails_only_the_human_check(cfg, event, llm, voice):
    _ok, detail = _verify(_draft(STIFF_BODY), cfg, event, llm, voice)
    failed = [k for k, v in detail["checks"].items() if not v]
    assert failed == ["sounds_human"], failed


def test_a_robotic_draft_is_a_soft_failure_by_default(cfg, event, llm, voice):
    """It still reaches the queue: a stiff email a human reads before sending
    is a smaller problem than a queue that never fills."""
    cfg.humanize_strict = False
    ok, detail = _verify(_draft(STIFF_BODY), cfg, event, llm, voice)
    assert detail["checks"]["sounds_human"] is False
    assert detail["soft_failures"] == ["sounds_human"]
    assert ok, "a soft failure must not block the queue"


def test_strict_mode_makes_it_a_hard_failure(cfg, event, llm, voice):
    cfg.humanize_strict = True
    ok, detail = _verify(_draft(STIFF_BODY), cfg, event, llm, voice)
    assert not ok
    assert detail["soft_failures"] == []


def test_the_feedback_names_the_lint_issues(cfg, event, llm, voice):
    cfg.humanize_strict = True
    _ok, detail = _verify(_draft(ROBOTIC_BODY, claims=[]), cfg, event, llm, voice)
    feedback = verify.failure_feedback(detail)
    assert "machine-written" in feedback
    assert "i hope this email finds you well" in feedback.lower()


def test_the_feedback_is_actionable_for_every_failure(cfg, event, llm, voice):
    body = "Hi Smit,\n\nAcme Analytics sponsored 47 hackathons. " + "pad " * 300
    _ok, detail = _verify(_draft(body, claims=["Acme sponsored 47 hackathons"],
                                 subject="Hi"), cfg, event, llm, voice,
                          contact_name="Devi Sharma")
    feedback = verify.failure_feedback(detail)
    for fragment in ("Too long", "Subject must be", "Wrong salutation",
                     "Not supported by the sources"):
        assert fragment in feedback, feedback


# ---------------------------------------------------------------------------
# confidence
# ---------------------------------------------------------------------------

def test_confidence_rewards_checks_research_and_sources(cfg, event, llm, voice):
    _ok, detail = _verify(_draft(), cfg, event, llm, voice)
    strong = verify.confidence_from(detail, 0.9)
    weak = verify.confidence_from(detail, 0.1)
    assert 0.0 <= weak < strong <= 1.0


def test_a_draft_with_no_sources_scores_lower(cfg, event, llm, voice):
    _ok, good = _verify(_draft(), cfg, event, llm, voice)
    ok_none, thin = verify.verify_draft(_draft(claims=[]), [], event, cfg=cfg,
                                        llm=llm, company="Acme Analytics",
                                        contact_name="Devi Sharma", voice=voice)
    assert verify.confidence_from(thin, 0.2) < verify.confidence_from(good, 0.85)


# ---------------------------------------------------------------------------
# no_pressure: a hard check, strict mode or not
# ---------------------------------------------------------------------------

PUSHY_BODY = (
    "Hi Devi,\n\n"
    "Acme Analytics runs a student programme that gives free licences to "
    "college societies.\n\n"
    "There are limited slots left for our Gold tier at Rs 20,000, so you must "
    "act now to secure your spot before this offer expires.\n\n"
    "Thanks,\nRudraksh\nSponsorship Lead, ACM SVNIT")


def test_pressure_never_reaches_a_card(cfg, event, llm, voice):
    """Not a style preference: the sender asked that companies never be
    pushed, so it fails the verifier whether or not strict mode is on."""
    cfg.humanize_strict = False
    ok, detail = _verify(_draft(PUSHY_BODY), cfg, event, llm, voice)
    assert not ok
    assert detail["checks"]["no_pressure"] is False
    assert "no_pressure" not in detail["soft_failures"]


def test_the_feedback_names_the_pressure(cfg, event, llm, voice):
    _ok, detail = _verify(_draft(PUSHY_BODY), cfg, event, llm, voice)
    feedback = verify.failure_feedback(detail)
    assert "Remove the pressure" in feedback
    assert "limited slots" in feedback
    assert "asking, not selling" in feedback


def test_a_polite_offer_passes(cfg, event, llm, voice):
    body = GOOD_BODY.replace(
        "Our Gold tier is Rs 20,000 and covers the stage backdrop. Does that fit?",
        "Our Gold tier is Rs 20,000 and covers the stage backdrop. That's only "
        "a suggestion though, the other tiers are open and we can shape the "
        "package around what's useful to you. If the timing is off, just say "
        "so and I won't chase it.")
    ok, detail = _verify(_draft(body), cfg, event, llm, voice)
    assert detail["checks"]["no_pressure"] is True
    assert ok, [k for k, v in detail["checks"].items() if not v]


# ---------------------------------------------------------------------------
# the email invites; the brochure quotes
# ---------------------------------------------------------------------------

def test_an_email_that_never_asks_is_caught(cfg, event, llm, voice):
    body = ("Hi Devi,\n\nAcme Analytics runs a student programme that gives "
            "free licences to college societies. Our event runs in October "
            "with twelve colleges over one long day of building and judging.")
    _ok, detail = _verify(_draft(body, claims=[]), cfg, event, llm, voice)
    assert detail["checks"]["has_ask"] is False
    assert "never actually asks" in verify.failure_feedback(detail)


@pytest.mark.parametrize("phrase", [
    "we would like to invite you to sponsor DotSlash",
    "we would love Acme as a partner",
    "we are asking for your support",
])
def test_an_invitation_counts_as_an_ask(cfg, event, llm, voice, phrase):
    body = ("Hi Devi,\n\nAcme Analytics runs a student programme that gives "
            "free licences to college societies. " + phrase +
            ", which runs in October with twelve colleges taking part.")
    _ok, detail = _verify(_draft(body, claims=[]), cfg, event, llm, voice)
    assert detail["checks"]["has_ask"] is True


def test_a_draft_no_longer_has_to_name_a_tier(cfg, event, llm, voice):
    """Tiers and amounts live in the brochure now: naming one in the email
    turns an invitation into a quote."""
    body = GOOD_BODY.replace(
        "Our Gold tier is Rs 20,000 and covers the stage backdrop. Does that fit?",
        "We would like to invite you to sponsor it, and we can shape what you "
        "get around what is worth most to you.")
    ok, detail = _verify(_draft(body), cfg, event, llm, voice)
    assert not any(t.lower() in body.lower() for t in event.tier_names)
    assert ok, [k for k, v in detail["checks"].items() if not v]
