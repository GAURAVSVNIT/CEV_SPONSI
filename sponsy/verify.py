"""The verifier. Cheap, deterministic, and it runs on every draft.

The interesting check is claims_traceable: the drafter had to enumerate its own
factual assertions, so grounding them is a per-claim lookup against the fetched
excerpts instead of an open-ended "is this email true?" question.

grounded() is deliberately keyword-first. Most claims are near-verbatim
restatements and settle on token overlap for free; only the ambiguous middle
costs a model call, and that call goes to the cheap model. Any number in a
claim that does not appear in the sources fails it outright -- invented
statistics are the failure mode that matters.

sounds_human is the humanizer's lint folded into the checklist. It is a hard
failure only with HUMANIZE_STRICT=1; otherwise it is reported on the card as a
soft failure, because a slightly stiff email that a human reads before sending
is a smaller problem than a queue that never fills.
"""

from __future__ import annotations

import re

from . import humanize, prompts
from .agent import structured

# Only a fallback: voice.max_words governs when a voice is configured. A
# sponsorship proposal has to say who we are, why them, and what is on offer,
# so the ceiling is a proposal's, not a cold one-liner's.
MAX_WORDS = 260
MIN_WORDS = 25
# One of these has to appear, or the email never actually asks for anything.
ASK_WORDS = ("sponsor", "partner", "back us", "backing us", "your support",
             "support us",
             # A returning email phrases the ask as a return, not a proposal:
             # "we want to invite you to support this edition", "we'd like to
             # have you back". The first live returning draft asked plainly and
             # was rejected because none of the words above appear in it, which
             # is the check being wrong rather than the email.
             "to support", "support this", "support the", "support our",
             "have you back", "welcome you back", "come back", "join us again",
             "be part of", "on board")
# A `returning` email whose whole premise is the existing relationship has to
# actually refer to it. If the model writes a cold pitch under this kind, the
# recipient gets "hello, we are ACM SVNIT" with their own logo on our site --
# the exact failure the variant exists to prevent -- so this is a hard check.
RETURNING_WORDS = ("again", "back", "previous", "last year", "past edition",
                   "earlier edition", "before", "returning", "renew",
                   "supported us", "sponsored us", "backed us", "worked with",
                   "9.0", "ninth", "last edition", "since then")
PLACEHOLDER = re.compile(r"\[|\{|XYZ|TODO|Lorem|<insert", re.I)
FILLER = (
    "hope this email finds you well",
    "hope you are doing well",
    "hope this finds you well",
    "i hope you're doing well",
    "trust this email finds you",
)
SUPERLATIVES = (
    "world-class", "cutting-edge", "unparalleled", "revolutionary",
    "game-changing", "industry-leading", "unmatched", "prestigious",
    "incredible", "amazing", "thrilled", "delighted", "excited to",
    "one of the best", "leading provider", "mutually beneficial", "win-win",
    "we are confident this", "we believe your", "fantastic", "outstanding",
    "exceptional", "tremendous", "perfect fit", "invaluable", "remarkable",
)
STOPWORDS = {
    "about", "above", "after", "again", "against", "their", "there", "these",
    "those", "which", "while", "would", "could", "should", "with", "that",
    "this", "have", "has", "had", "for", "and", "the", "are", "was", "were",
    "they", "them", "from", "into", "over", "also", "been", "being", "your",
    "you", "our", "its", "it", "of", "in", "on", "to", "as", "at", "by", "is",
    "an", "a", "or", "be", "we", "us", "who", "how", "why", "what", "when",
    "company", "companies", "students", "student", "campus", "college",
}
TOKEN = re.compile(r"[a-z0-9][a-z0-9'&.-]*")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

GREETING = re.compile(r"^\s*(?:hi|hello|dear|hey)\b[ ,]*([^\n,]*)", re.I)
# Salutations that name nobody, and so cannot name the wrong person.
GENERIC_GREETINGS = {"", "there", "team", "all", "everyone", "folks", "sir",
                     "madam", "sir/madam", "colleagues", "devrel", "hiring team"}


def greeting_ok(body: str, contact_name: str = "") -> tuple[bool, str]:
    """Does the salutation address the contact we actually have?

    Models happily lift a human name out of the sources -- a blog byline, a
    quoted engineer -- and greet them by it. It never appears in `claims`, so
    groundedness will not catch it, and it is the single most visible way to
    get an outbound email wrong.
    """
    match = GREETING.match((body or "").strip())
    if not match:
        return True, "no greeting"
    greeted = match.group(1).strip().rstrip(",.!").strip()
    low = greeted.lower()
    if low in GENERIC_GREETINGS or "team" in low:
        return True, "generic greeting"
    if not contact_name:
        return False, "greets {!r} but we have no contact name on file".format(
            greeted)
    first = contact_name.split()[0].lower()
    if first in low or low in contact_name.lower():
        return True, "matches the contact"
    return False, "greets {!r} but our contact is {!r}".format(greeted,
                                                              contact_name)


# ---------------------------------------------------------------------------
# groundedness
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    # Punctuation is kept *inside* a token (co-founder, at&t, d.o.o) but
    # stripped at the edges -- otherwise the last word of every sentence carries
    # a full stop and never matches the same word in the sources, which makes
    # the whole groundedness score quietly pessimistic.
    out = set()
    for raw in TOKEN.findall(str(text or "").lower()):
        token = raw.strip(".-'&")
        if len(token) > 3 and token not in STOPWORDS:
            out.add(token)
    return out


def _numbers(text: str) -> set[str]:
    out = set()
    for raw in NUMBER.findall(str(text or "")):
        cleaned = raw.replace(",", "").rstrip(".")
        if cleaned:
            out.add(cleaned)
    return out


def overlap(claim: str, source_text: str) -> float:
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return 0.0
    return len(claim_tokens & _tokens(source_text)) / len(claim_tokens)


def grounded(claim: str, excerpts, cfg=None, llm=None, db=None, run_id=None,
             allowed_numbers: set[str] | None = None,
             own_facts: str = "") -> tuple[bool, str]:
    """Is this claim supported by the fetched sources?

    `own_facts` is what we know about ourselves (event.yml). Models routinely
    list "the event is on 17 October" as one of their claims, and grounding
    that against the prospect's website is nonsense -- it is true by
    construction, so it is checked against our own config instead.
    """
    mode = getattr(cfg, "grounding_mode", "keyword") if cfg else "keyword"
    if mode == "off":
        return True, "grounding disabled"
    source_text = "\n".join(e.text for e in excerpts)
    if not source_text.strip() and not own_facts.strip():
        return False, "no sources at all"

    if own_facts and overlap(claim, own_facts) >= 0.75:
        return True, "our own event facts, not a claim about them"

    allowed = set(allowed_numbers or set())
    stray = {n for n in _numbers(claim)
             if n not in allowed and n not in _numbers(source_text)}
    if stray:
        return False, "number(s) not in sources: " + ", ".join(sorted(stray))

    ratio = overlap(claim, source_text)
    if mode == "keyword":
        return ratio >= 0.7, "token overlap {:.2f}".format(ratio)
    if mode == "hybrid":
        if ratio >= 0.8:
            return True, "token overlap {:.2f}".format(ratio)
        # Only auto-reject when there is enough of the claim to be confident the
        # mismatch is real. Short claims are mostly paraphrase ("focuses on
        # financial education" vs "free stock market education initiative"), and
        # a lexical miss on four words is noise, not evidence. Those go to the
        # judge instead.
        if ratio < 0.35 and len(_tokens(claim)) >= 5:
            return False, "token overlap {:.2f}".format(ratio)
    if llm is None:
        return ratio >= 0.6, "token overlap {:.2f} (no judge available)".format(ratio)

    # The judge is a yes/no question over a page of text: the cheap model.
    model = None
    resolve = getattr(cfg, "resolved_models", None)
    if callable(resolve):
        model = resolve()[1]
    numbered = "\n\n".join(e.block(i + 1) for i, e in enumerate(excerpts))
    data, _ = structured(llm, system=prompts.GROUND_SYSTEM,
                         user=prompts.GROUND_USER.format(claim=claim,
                                                         excerpts=numbered),
                         model=model, temperature=0.0, db=db, run_id=run_id,
                         kind="ground_claim")
    return bool(data.get("supported")), str(data.get("why", ""))[:200]


# ---------------------------------------------------------------------------
# the checklist
# ---------------------------------------------------------------------------

def own_facts(event) -> str:
    """Everything we assert about ourselves, from event.yml. True by
    construction, so a claim that restates it is not a claim about them."""
    parts = [event.event_name, str(event.date), event.colleges, event.summary,
             event.club, event.sender_name, event.sender_role, event.website,
             "{} registrations so far".format(event.registrations_so_far)]
    if event.expected_participants:
        parts.append("{} participants".format(event.expected_participants))
    if event.prize_pool:
        parts.append("prize pool {}".format(event.prize_pool))
    parts += list(event.track_record or [])
    parts += [t.line() for t in event.tiers]
    parts += list(event.confirmed_sponsors or [])
    return " . ".join(str(p) for p in parts if p)


def allowed_numbers_for(event) -> set[str]:
    """Numbers the draft may contain without a source: they are ours."""
    allowed = {str(event.expected_participants), str(event.prize_pool),
               str(event.registrations_so_far)}
    for tier in event.tiers:
        allowed.add(str(tier.amount))
    # numbers we have vouched for ourselves (dates, past-edition stats, the
    # audience line, phone, site)
    for fact in (list(event.track_record or [])
                 + [event.date, event.event_name, event.phone, event.summary,
                    event.website, event.colleges]):
        allowed |= _numbers(fact)
    allowed |= {"1", "2", "3", "4", "5"}               # ordinary counting
    return allowed


def verify_draft(draft, excerpts, event, cfg=None, llm=None, company: str = "",
                 db=None, run_id=None, contact_name: str = "",
                 voice=None) -> tuple[bool, dict]:
    company = company or ""
    body = draft.body or ""
    subject = draft.subject or ""
    low = body.lower()
    kind = getattr(draft, "kind", "") or ""
    max_words = int(getattr(voice, "max_words", 0) or 0) or MAX_WORDS

    greet_ok, greet_why = greeting_ok(body, contact_name)
    own = own_facts(event)
    allowed = allowed_numbers_for(event)
    claim_results: list[dict] = []
    for claim in draft.claims:
        ok, why = grounded(claim, excerpts, cfg=cfg, llm=llm, db=db, run_id=run_id,
                           allowed_numbers=allowed, own_facts=own)
        claim_results.append({"claim": claim, "grounded": ok, "why": why})

    lint = humanize.lint(body, subject, voice)

    checks = {
        "length": draft.words <= max_words,
        "names_company": bool(company) and company.split()[0].lower() in low,
        # The email no longer names a tier or a price -- those are in the
        # brochure -- so the ask is the invitation itself. An email that never
        # actually asks is the one failure this check exists to catch.
        "has_ask": any(word in low for word in ASK_WORDS),
        "no_placeholder": not PLACEHOLDER.search(body),
        "no_filler": not any(f in low for f in FILLER),
        "subject_ok": 5 <= len(subject) <= 70,
        "claims_traceable": all(c["grounded"] for c in claim_results),
        "no_superlatives": not any(s in low for s in SUPERLATIVES),
        "greets_our_contact": greet_ok,
        # `returning` joins the exempt list: its hook is our own record of the
        # relationship, not anything research turned up, so a returning email
        # with no claims about the company is correct rather than lazy.
        "has_claims": bool(draft.claims) or kind in ("last_touch", "needs_info",
                                                     "nudge", "returning"),
        "mentions_relationship": (kind != "returning"
                                  or any(w in low for w in RETURNING_WORDS)),
        "not_empty": len(body.split()) >= MIN_WORDS,
        "sounds_human": lint.score >= humanize.PASS_SCORE,
        # Not a style question: we are asking a company for a favour, so
        # manufactured urgency or "you must" is a defect that never reaches a
        # card, strict mode or not.
        "no_pressure": not lint.flags.get("pressure"),
    }
    strict = bool(getattr(cfg, "humanize_strict", False)) if cfg else False
    hard = {k: v for k, v in checks.items() if k != "sounds_human" or strict}
    soft = [] if (checks["sounds_human"] or strict) else ["sounds_human"]

    detail = {
        "checks": checks,
        "claims": claim_results,
        "words": draft.words,
        "max_words": max_words,
        "ungrounded": [c["claim"] for c in claim_results if not c["grounded"]],
        "sources": len({e.url for e in excerpts}),
        "greeting": greet_why,
        "lint": lint.to_dict(),
        "soft_failures": soft,
    }
    return all(hard.values()), detail


def failure_feedback(detail: dict) -> str:
    """Turn a failed checklist into instructions the drafter can act on."""
    checks = detail["checks"]
    lines = []
    if not checks["length"]:
        lines.append("- Too long ({} words). Hard limit is {}.".format(
            detail["words"], detail.get("max_words", MAX_WORDS)))
    if not checks["not_empty"]:
        lines.append("- Too short to be a real email.")
    if not checks["names_company"]:
        lines.append("- The company name does not appear in the body.")
    if not checks["has_ask"]:
        lines.append("- The email never actually asks for anything. Invite them "
                     "to sponsor the event in plain words.")
    if not checks["no_placeholder"]:
        lines.append("- Contains placeholder text or brackets.")
    if not checks["no_filler"]:
        lines.append("- Opens with filler. Delete it.")
    if not checks["subject_ok"]:
        lines.append("- Subject must be between 5 and 70 characters.")
    if not checks["no_superlatives"]:
        lines.append("- Remove superlatives and flattery.")
    if not checks.get("greets_our_contact", True):
        lines.append("- Wrong salutation: {}. Use only the contact name given "
                     "in the instructions, or a generic greeting if there is "
                     "none. Never take a name from the sources.".format(
                         detail.get("greeting", "")))
    if not checks.get("no_pressure", True):
        pressure = (detail.get("lint") or {}).get("flags", {}).get("pressure") or []
        lines.append("- Remove the pressure: {}. We are asking, not selling. No "
                     "urgency, no deadline, no obligation, and never imply the "
                     "suggested tier is the only one.".format(", ".join(
                         '"{}"'.format(p) for p in pressure)))
    if not checks["has_claims"]:
        lines.append("- You must enumerate your factual claims in 'claims'.")
    if not checks.get("mentions_relationship", True):
        lines.append("- This company has sponsored a past edition and the email "
                     "never says so. Open on that, not on who we are.")
    for claim in detail["ungrounded"]:
        lines.append("- Not supported by the sources, remove or rewrite: "
                     + claim[:160])
    # Soft or hard, the lint issues are the most actionable lines here: they
    # name the phrase, the punctuation, the rhythm.
    if not checks.get("sounds_human", True):
        lint = detail.get("lint") or {}
        lines.append("- It reads as machine-written (human score {}). Fix these "
                     "without changing any fact or number:".format(
                         lint.get("score", "?")))
        for issue in lint.get("issues") or []:
            lines.append("  - " + str(issue))
    return "\n".join(lines)


def confidence_from(detail: dict, research_confidence: float) -> float:
    """Blend the checklist with how good the research was underneath it."""
    checks = detail["checks"]
    passed = sum(1 for v in checks.values() if v) / len(checks)
    sources = min(detail["sources"], 3) / 3
    return round(0.5 * passed + 0.3 * research_confidence + 0.2 * sources, 2)
