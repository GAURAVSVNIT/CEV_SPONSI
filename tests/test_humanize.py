"""The AI-tell lint and the rewrite pass.

Every deduction rule gets one focused test, because the lint is the only part
of the humanizer that runs on every draft for free -- if it is wrong, either
good emails get rewritten for no reason or robotic ones sail through.
"""

from __future__ import annotations

import json

import pytest

from sponsy import humanize
from sponsy.config import Voice
from sponsy.llm import FakeLLM, LLMResponse
from sponsy.models import Draft

HUMAN = (
    "Hi Priya,\n\n"
    "Quick one. Your team ran the API workshop at our last edition and people "
    "still bring it up.\n\n"
    "We're doing it again on 20 February. The Beta tier is USD 500 and covers "
    "a workshop slot plus the resume book. Does that fit?\n\n"
    "Thanks,\nRudraksh\nSponsorship Lead, ACM SVNIT")

ROBOTIC = (
    "Hi Priya,\n\n"
    "I hope this email finds you well! I am reaching out to explore a mutually "
    "beneficial partnership between our esteemed organisations.\n\n"
    "I wanted to share that we are excited to host an innovative event. I "
    "believe your cutting-edge platform would elevate the experience. I would "
    "love to leverage this opportunity.\n\n"
    "- Logo on all assets\n"
    "- A workshop slot\n\n"
    "Please feel free to reach out at your earliest convenience!\n\n"
    "Best regards,\nRudraksh")


def _draft(body: str = ROBOTIC, subject: str = "Partnership Opportunity") -> Draft:
    return Draft(prospect_id="P01", kind="initial", subject=subject, body=body,
                 claims=["Acme runs a student programme"])


# ---------------------------------------------------------------------------
# the two ends of the scale
# ---------------------------------------------------------------------------

def test_a_human_email_scores_clean(voice):
    result = humanize.lint(HUMAN, "DotSlash sponsorship, Beta tier", voice)
    assert result.score >= 0.8, result.issues
    assert result.passed


def test_a_robotic_email_scores_badly_and_says_why(voice):
    result = humanize.lint(ROBOTIC, "Partnership Opportunity", voice)
    assert result.score < 0.5
    assert not result.passed
    joined = " ".join(result.issues).lower()
    assert "i hope this email finds you well" in joined
    assert "mutually beneficial" in joined
    assert "esteemed" in joined


def test_empty_input_is_clean_not_a_pile_of_issues():
    result = humanize.lint("", "")
    assert result.score == 1.0 and result.issues == []


def test_score_never_goes_below_zero(voice):
    result = humanize.lint(ROBOTIC * 4, "Exciting Partnership Opportunity!", voice)
    assert result.score == 0.0


# ---------------------------------------------------------------------------
# one test per deduction rule
# ---------------------------------------------------------------------------

def test_banned_phrases_are_found_whole_word():
    assert "leverage" in humanize.lint("We can leverage this.").flags["banned"]
    # "leverages" is a different word and must not trip the rule
    assert "leverage" not in humanize.lint("The leverages differ.").flags["banned"]


def test_the_banned_deduction_is_capped():
    """One bad paragraph must not zero a draft that is otherwise fine."""
    body = ("I hope this email finds you well. I am reaching out. We are excited. "
            "It is innovative, cutting-edge, seamless and robust.")
    assert humanize.lint(body).score >= 1.0 - humanize.BANNED_CAP - 0.001


def test_phrases_the_sender_avoids_are_enforced():
    voice = Voice(phrases_i_avoid=["circle back", "touch base"])
    result = humanize.lint("Let's circle back next week.", voice=voice)
    assert result.flags["avoided"] == ["circle back"]
    assert "never writes that" in " ".join(result.issues)


def test_a_phrase_is_not_penalised_twice():
    """A phrase on both lists is one problem, not two."""
    voice = Voice(phrases_i_avoid=["leverage"])
    result = humanize.lint("We will leverage it.", voice=voice)
    assert result.flags["banned"] == ["leverage"]
    assert result.flags["avoided"] == []


@pytest.mark.parametrize("body,expected", [
    ("One thing — another — a third.", 2),
    ("One thing -- another -- a third.", 2),
    ("A word - another word here.", 1),
    ("A well-known pre-paid non-issue.", 0),
])
def test_em_dashes_are_counted_but_hyphens_are_not(body, expected):
    assert humanize.lint(body).flags["em_dashes"] == expected


def test_more_than_one_em_dash_is_a_tell():
    assert humanize.lint("A — b — c.").score < 1.0
    assert humanize.lint("A — b.").score == 1.0


def test_exclamation_marks_are_docked_and_capped():
    assert humanize.lint("Hello!").flags["exclamations"] == 1
    # Six of them, but sentence lengths vary so nothing else fires: the whole
    # deduction has to be the exclamation cap and no more.
    shouty = ("Wow! That is genuinely a very good idea indeed! Yes! We should "
              "absolutely do it again next year with the same team! Great! "
              "Come along!")
    assert humanize.lint(shouty).flags["exclamations"] == 6
    assert humanize.lint(shouty).score == pytest.approx(
        1.0 - humanize.EXCLAMATION_CAP)


def test_a_subject_exclamation_counts_too():
    assert humanize.lint("Body text.", "Great news!").flags["exclamations"] == 1


@pytest.mark.parametrize("body", [
    "Some **bold** text.",
    "# A heading",
    "## Another heading",
])
def test_markdown_formatting_is_always_caught(body):
    assert humanize.lint(body).flags["markdown"]


@pytest.mark.parametrize("body", [
    "What you get:\n- logo on the shirts\n- a workshop slot",
    "What you get:\n* logo on the shirts\n* a workshop slot",
    "What you get:\n1. logo on the shirts\n2. a workshop slot",
])
def test_a_short_list_of_what_a_sponsor_gets_is_allowed(body):
    """A proposal may list what is on offer; that is structure, not a model
    forgetting it is writing an email."""
    result = humanize.lint(body)
    assert not result.flags["markdown"]
    assert result.flags["list_lines"] == 2


def test_a_wall_of_bullets_is_the_brochure_pasted_in():
    body = "What you get:\n" + "\n".join("- item {}".format(i) for i in range(6))
    result = humanize.lint(body)
    assert result.flags["markdown"]
    assert "brochure" in " ".join(result.issues)


def test_a_single_numbered_line_is_not_a_list():
    assert not humanize.lint("2. February is when we run it.").flags["markdown"]


def test_three_sentences_starting_with_i():
    body = "I ran it. I liked it. I would do it again."
    assert humanize.lint(body).flags["i_sentences"] == 3
    assert humanize.lint(body).score < 1.0


def test_no_contractions_in_a_long_body():
    body = " ".join(["We would like to inform you of the event details."] * 9)
    result = humanize.lint(body)
    assert result.flags["no_contractions"]
    assert "contraction" in " ".join(result.issues)


def test_contractions_are_recognised():
    body = "We're running it again. " + " ".join(["It is on in February."] * 12)
    assert not humanize.lint(body).flags["no_contractions"]


def test_a_short_stiff_body_is_not_docked_for_contractions():
    assert not humanize.lint("We would like to sponsor.").flags["no_contractions"]


def test_uniform_sentence_length_is_a_tell():
    body = " ".join(["The team builds tools for people who write code."] * 6)
    assert humanize.lint(body).flags["uniform_sentences"]


def test_varied_sentences_are_not():
    body = ("Quick one. Your team ran the workshop at our last edition and the "
            "people who came still bring it up months afterwards. We're doing "
            "it again in February. Interested? The tier is USD 500 and it "
            "covers a slot plus the resume book.")
    assert not humanize.lint(body).flags["uniform_sentences"]


@pytest.mark.parametrize("subject", [
    "Partnership Opportunity", "Collaboration Proposal", "Sponsorship Request",
    "An exciting chance", "DotSlash: sponsorship", "DotSlash | sponsorship",
])
def test_salesy_subjects_are_caught(subject):
    assert humanize.lint("Body.", subject).flags["salesy_subject"]


def test_a_quiet_subject_is_fine():
    assert not humanize.lint("Body.", "DotSlash 10.0 sponsorship").flags["salesy_subject"]


def test_a_long_subject_is_docked():
    assert humanize.lint("Body.", "x" * 80).flags["subject_long"]


def test_a_signoff_in_the_prose_is_a_duplicate(voice):
    """render.py adds the sign-off and the contact block, so one inside the
    model's prose would appear twice in the finished email."""
    result = humanize.lint(HUMAN, "", voice)
    assert result.flags["signoff_mismatch"]
    assert "would appear twice" in " ".join(result.issues)
    opening = HUMAN.split("Thanks,")[0].strip()
    assert not humanize.lint(opening, "", voice).flags["signoff_mismatch"]


def test_a_missing_signoff_is_caught_when_one_is_expected(voice):
    """The old plain-text behaviour, still available for anything that sends
    the prose as the whole email."""
    opening = HUMAN.split("Thanks,")[0].strip()
    assert humanize.lint(opening, "", voice, signoff_expected=True) \
        .flags["signoff_mismatch"]
    assert not humanize.lint(HUMAN, "", voice, signoff_expected=True) \
        .flags["signoff_mismatch"]


def test_no_voice_means_no_signoff_check():
    assert not humanize.lint("Body text here.").flags["signoff_mismatch"]


@pytest.mark.parametrize("body", [
    "Hi [NAME], we run it.", "Hi {company}, we run it.",
    "<insert detail here>", "TODO write this bit.",
])
def test_placeholders_are_caught(body):
    assert humanize.lint(body).flags["placeholder"]


@pytest.mark.parametrize("body", [
    "Dear Sir/Madam,\n\nWe run an event.",
    "Dear Sir or Madam,\n\nWe run an event.",
    "To whom it may concern,\n\nWe run an event.",
])
def test_form_letter_salutations_are_caught(body):
    assert humanize.lint(body).flags["generic_salutation"]


def test_curly_apostrophes_are_normalised():
    """A smart quote must not hide a contraction from the rhythm check, nor a
    banned phrase from the phrase check."""
    assert not humanize.lint("We’re on. " + "It is fine. " * 20) \
        .flags["no_contractions"]
    assert humanize.lint("I don’t hesitate to ask.").flags["banned"] == \
        ["don't hesitate"]


def test_issues_are_instructions_not_labels(voice):
    """The list is fed to a model, so each line has to say what to do."""
    for issue in humanize.lint(ROBOTIC, "Partnership Opportunity", voice).issues:
        assert issue[0].isupper() or issue[0].isdigit(), issue
        assert issue.rstrip().endswith((".", ")")), issue
        assert len(issue.split()) >= 4, issue


def test_to_dict_round_trips(voice):
    data = humanize.lint(ROBOTIC, "Partnership Opportunity", voice).to_dict()
    assert set(data) == {"score", "passed", "issues", "flags"}
    assert json.dumps(data)                 # it is stored in drafts.checks


# ---------------------------------------------------------------------------
# the rewrite pass
# ---------------------------------------------------------------------------

# Bad enough to fail, not so bad that the score floors at zero and no
# improvement can be observed.
MODERATE = (
    "Hi Priya,\n\n"
    "I hope this email finds you well! We would like to propose a sponsorship "
    "of our event — the Beta tier is USD 500 — and it covers a workshop "
    "slot.\n\n"
    "Thanks,\nRudraksh\nSponsorship Lead, ACM SVNIT")


def test_the_rewrite_raises_the_score_and_keeps_the_numbers(cfg, db, llm, voice):
    draft = _draft(MODERATE, subject="DotSlash sponsorship")
    before = humanize.lint(draft.body, draft.subject, voice)
    assert not before.passed, "the fixture must actually fail the lint"

    rewritten, _cost = humanize.humanize_pass(llm, cfg, db, draft, voice,
                                              before.issues)
    after = humanize.lint(rewritten.body, rewritten.subject, voice)
    assert after.score > before.score
    assert rewritten.humanized
    assert rewritten.claims == draft.claims
    assert "500" in rewritten.body, "the ask must survive the edit"
    assert "!" not in rewritten.body
    assert "hope this email finds you well" not in rewritten.body.lower()


def test_the_rewrite_is_rejected_when_it_invents_a_number(cfg, db, voice):
    draft = _draft(body="Hi Priya,\n\nThe Beta tier is USD 500.\n\nThanks,\nRudraksh")
    scripted = LLMResponse(text=json.dumps(
        {"subject": "ok", "body": "Hi Priya,\n\nThe Beta tier is USD 900.\n\n"
                                  "Thanks,\nRudraksh"}))
    out, _cost = humanize.humanize_pass(FakeLLM(cfg, scripted=[scripted]), cfg, db,
                                        draft, voice, ["fix it"])
    assert out is draft, "a rewrite that changes money must not be accepted"
    row = db.one("SELECT payload FROM actions WHERE kind='humanize_rejected'")
    assert row and "900" in row["payload"]


def test_the_rewrite_is_rejected_when_it_drops_a_number(cfg, db, voice):
    draft = _draft(body="Hi Priya,\n\nThe Beta tier is USD 500.\n\nThanks,\nRudraksh")
    scripted = LLMResponse(text=json.dumps(
        {"subject": "ok", "body": "Hi Priya,\n\nThe Beta tier is cheap.\n\n"
                                  "Thanks,\nRudraksh"}))
    out, _ = humanize.humanize_pass(FakeLLM(cfg, scripted=[scripted]), cfg, db,
                                    draft, voice, ["fix it"])
    assert out is draft
    assert db.one("SELECT 1 FROM actions WHERE kind='humanize_rejected'")


def test_the_rewrite_is_rejected_when_it_grows(cfg, db, voice):
    draft = _draft(body="Hi Priya,\n\nShort note.\n\nThanks,\nRudraksh")
    padded = draft.body + " " + " ".join(["padding"] * 40)
    scripted = LLMResponse(text=json.dumps({"subject": "ok", "body": padded}))
    out, _ = humanize.humanize_pass(FakeLLM(cfg, scripted=[scripted]), cfg, db,
                                    draft, voice, ["fix it"])
    assert out is draft


def test_the_rewrite_is_rejected_when_it_comes_back_empty(cfg, db, voice):
    draft = _draft()
    scripted = LLMResponse(text=json.dumps({"subject": "ok", "body": "  "}))
    out, _ = humanize.humanize_pass(FakeLLM(cfg, scripted=[scripted]), cfg, db,
                                    draft, voice, ["fix it"])
    assert out is draft


def test_the_rewrite_never_touches_the_claims(cfg, db, llm, voice):
    draft = _draft()
    out, _ = humanize.humanize_pass(llm, cfg, db, draft, voice, ["fix it"])
    assert out.claims == draft.claims
    assert out.prospect_id == draft.prospect_id and out.kind == draft.kind


def test_the_prompt_carries_the_voice_and_the_issues(cfg, db, llm, voice):
    humanize.humanize_pass(llm, cfg, db, _draft(), voice,
                           ["Remove the exclamation mark."])
    sent = llm.calls[-1]["messages"][0]["text"]
    assert "Remove the exclamation mark." in sent
    assert voice.sender_name in sent
    assert voice.sample_emails[0].split("\n")[0] in sent


# ---------------------------------------------------------------------------
# pressure: we are asking a company for a favour, not selling to them
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "There are limited slots left, so act now.",
    "You must decide before the deadline is approaching.",
    "Secure your spot today, this offer expires soon.",
    "This is an exclusive opportunity you don't want to miss out on.",
    "The Beta tier is the only tier available to you.",
])
def test_pressure_language_is_caught(body):
    result = humanize.lint(body)
    assert result.flags["pressure"], body
    assert any("asking a company for a favour" in i for i in result.issues)


def test_an_ordinary_ask_is_not_pressure():
    body = ("Our Beta tier is USD 500, though it's only a suggestion and the "
            "other tiers are open. If the timing is off, just say so and I "
            "won't chase it.")
    assert humanize.lint(body).flags["pressure"] == []


def test_pressure_costs_more_than_a_stock_phrase():
    """It is the one thing the sender explicitly does not want to do."""
    assert humanize.lint("Act now, limited slots.").score < \
        humanize.lint("We are excited to work with you.").score
