"""Offline stand-ins for the model, used by tests, evals and --offline runs.

These are stubs, not simulators. They exist so the pipeline, the verifier, the
humanizer, the ladder and the approval loop can be exercised end to end on a
laptop with no API key and no network -- and so a run against frozen sources
is byte-stable.

Set SPONSY_FAKE_HALLUCINATE=1 to make the fake drafter invent a statistic on
its first attempt. Set SPONSY_FAKE_ROBOTIC=1 to make it write like a bot, so
the humanizer has something to fix.
"""

from __future__ import annotations

import json
import os
import re

from .llm import LLMResponse, ToolCall, register_fake

STUDENTY = ("student", "campus", "college", "university", "intern", "hackathon",
            "fest", "sponsor", "scholarship", "learn", "education", "developer")


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip() for p in parts if len(p.strip()) > 40]


def _first_with(sentences: list[str], words) -> str:
    for sentence in sentences:
        low = sentence.lower()
        if any(w in low for w in words):
            return sentence
    return ""


# ---------------------------------------------------------------------------
# research: drives the real tool loop against real (or frozen) sources
# ---------------------------------------------------------------------------

@register_fake("research")
def fake_research(system: str, messages: list[dict]):
    company = ""
    match = re.search(r"Company:\s*(.+)", messages[0].get("text", ""))
    if match:
        company = match.group(1).strip()

    searches = [m for m in messages if m.get("role") == "tool"
                and m.get("name") == "web_search"]
    fetches = [m for m in messages if m.get("role") == "tool"
               and m.get("name") == "fetch_page"]
    fetched_urls = {m["response"].get("url") for m in fetches
                    if isinstance(m.get("response"), dict)}

    if not searches:
        return LLMResponse(tool_calls=[ToolCall(
            "web_search", {"query": company + " students hackathon"}, id="c1")],
            prompt_tokens=600, output_tokens=20)

    results = []
    for msg in searches:
        results.extend((msg.get("response") or {}).get("results") or [])
    todo = [r["url"] for r in results if r.get("url") not in fetched_urls]
    if todo and len(fetches) < 2:
        return LLMResponse(tool_calls=[ToolCall("fetch_page", {"url": todo[0]},
                                                id="c{}".format(len(fetches) + 2))],
                           prompt_tokens=900, output_tokens=20)

    sentences: list[str] = []
    for msg in fetches:
        text = (msg.get("response") or {}).get("text") or ""
        sentences.extend(_sentences(text))

    one_liner = sentences[0] if sentences else ""
    student = _first_with(sentences, STUDENTY)
    prior = _first_with(sentences, ("sponsor", "sponsored", "partnered with"))
    recent = _first_with(sentences, ("launched", "announced", "new ", "recently"))
    hook = student or one_liner
    urls = sorted(u for u in fetched_urls if u)
    domain = ""
    if urls:
        domain = re.sub(r"^https?://(www\.)?", "", urls[0]).split("/")[0]
    confidence = 0.85 if (student and len(urls) >= 2) else (0.55 if student else 0.3)
    return LLMResponse(text=json.dumps({
        "one_liner": one_liner,
        "student_relevance": student or None,
        "prior_campus_sponsorship": prior or None,
        "recent_signal": recent or None,
        "hook": hook,
        "company_domain": domain,
        "sources_used": urls,
        "confidence": confidence,
    }, ensure_ascii=False), prompt_tokens=1500, output_tokens=220)


# ---------------------------------------------------------------------------
# drafting
# ---------------------------------------------------------------------------

def _parse_prompt(user: str) -> dict:
    out: dict = {}
    match = re.search(r"Write a sponsorship email to (.+?) at (.+?)\.\n", user)
    if match:
        out["contact"], out["company"] = match.group(1), match.group(2)
    match = re.search(r"Research \(the ONLY facts[^)]*\):\n(\{.*?\n\})\n", user, re.S)
    try:
        out["research"] = json.loads(match.group(1)) if match else {}
    except json.JSONDecodeError:
        out["research"] = {}
    match = re.search(r"About our event: (.+?), (.+?)\.\n", user)
    if match:
        out.update(event=match.group(1), date=match.group(2))
    match = re.search(r"Audience: (.+)", user)
    if match:
        # The audience line is a sentence in event.yml; these templates splice
        # it mid-sentence, so the trailing full stop has to go or the demo
        # renders "across 25+ states., and it felt like a fit."
        out["colleges"] = match.group(1).strip().rstrip(". ")
    tiers = re.findall(r"^- (.+?) \(([A-Za-z$]+) ([\d,]+)\): (.+)$", user, re.M)
    out["tiers"] = [{"name": t[0], "currency": t[1], "amount": t[2],
                     "deliverables": t[3]} for t in tiers]
    match = re.search(r"Greet them as: (.+)", user)
    out["greeting"] = match.group(1).strip() if match else "Hi there,"
    match = re.search(r"Sign off exactly as:\n(.+?)\nContact details", user, re.S)
    out["signoff"] = match.group(1).strip() if match else "Thanks"
    match = re.search(r"you may cite \(nothing else about us is true\):\n- (.+)",
                      user)
    record = match.group(1).strip() if match else ""
    if record and record[-1] not in ".!?":
        record += "."                        # it is spliced mid-paragraph
    out["record"] = record
    match = re.search(r"Contact details you may add after the sign-off: (.+)", user)
    out["contact"] = match.group(1).strip() if match else ""
    out["kind"] = (re.search(r"Kind: (\w+)", user) or [None, "initial"])[1]
    out["retry"] = "rejected by the verifier" in user
    out["redo"] = "asked for this change" in user
    out["brochure"] = "brochure (PDF) is attached" in user
    return out


@register_fake("draft")
def fake_draft(system: str, messages: list[dict]):
    user = messages[0].get("text", "")
    ctx = _parse_prompt(user)
    research = ctx.get("research") or {}
    company = ctx.get("company", "your team")
    tiers = ctx.get("tiers") or [{"name": "Gold", "amount": "15,000",
                                  "currency": "Rs", "deliverables": "logo on all assets"}]
    tier = tiers[1] if len(tiers) > 1 else tiers[0]
    money = "{} {}".format(tier.get("currency", "Rs"), tier["amount"])
    hook = (research.get("hook") or research.get("one_liner") or "").strip()
    one_liner = (research.get("one_liner") or "").strip()
    thin = float(research.get("confidence") or 0) < 0.45

    claims = [c for c in (one_liner, hook) if c]
    claims = list(dict.fromkeys(claims))[:2]

    # The stub writes what the real model writes: the personalised opening
    # only. No tier, no price, no sign-off -- the brochure carries the tiers
    # and render.py adds the sign-off.
    want = (research.get("student_relevance") or research.get("hook") or "").strip()
    if ctx["kind"] == "returning":
        # The stub mirrors what the real returning prompt asks for: open on the
        # relationship, no who-we-are paragraph, no claims about the company
        # (the hook is our own record, and the verifier exempts this kind).
        body = (
            "{greeting}\n\n"
            "You backed a previous edition of {event}, and it made a real "
            "difference to what the teams could build that weekend. We're "
            "running it again on {date} and we'd like to have {company} "
            "back.\n\n"
            "Since you already know how the event runs, I'll skip the "
            "introduction. The brochure attached has this year's packages.\n\n"
            "If some parts of last time were worth more to you than others, "
            "tell me which and we'll build the package around that rather "
            "than around our own tiers.")
        claims = []
    elif thin:
        body = (
            "{greeting}\n\n"
            "I couldn't find much about how {company} works with student "
            "communities, so I'd rather ask than assume: do you back college "
            "hackathons at all?\n\n"
            "We'd like to invite you to sponsor {event}, our hackathon on "
            "{date} for {colleges}.\n\n"
            "If you do, we can shape what you get around whatever is worth "
            "most to you, and the attached brochure sets out the options.")
        claims = claims[:1]
    else:
        body = (
            "{greeting}\n\n"
            "{hook} That's why we're writing to you rather than to a general "
            "list: the people who turn up to build at our event are the ones "
            "{company} is already trying to reach.\n\n"
            "We'd like to invite you to sponsor {event}, our hackathon on "
            "{date} for {colleges}.\n\n"
            "Given that, the parts most likely to be worth your while are a "
            "challenge track built around your own product and access to the "
            "attendee resume book, so the teams build on {company} and your "
            "recruiters see who did it well. We can shape the rest around "
            "what you actually want out of it.")
    body = body.format(
        greeting=ctx.get("greeting", "Hi there,"), company=company, hook=hook,
        event=ctx.get("event", "our event"), date=ctx.get("date", "next month"),
        colleges=ctx.get("colleges", "nearby colleges"),
        signoff=ctx.get("signoff", "Thanks"))

    if os.environ.get("SPONSY_FAKE_HALLUCINATE") == "1" and not ctx["retry"]:
        invented = "{} sponsored 47 college hackathons last year".format(company)
        body = body.replace("That's why we're writing",
                            invented + ", so this should be familiar. "
                            "That's why we're writing", 1)
        claims.append(invented)
    if os.environ.get("SPONSY_FAKE_ROBOTIC") == "1" and not ctx["retry"] \
            and not ctx["redo"]:
        # Deliberately *only* the tells the lint can see: no contractions, a
        # pair of em-dashes, an exclamation mark. Stock phrases like "I hope
        # this email finds you well" would be caught by the verifier's own
        # checklist first, and then the humanizer would never run at all --
        # which is the opposite of what this switch is for.
        body = (body.replace("That's", "That is").replace("we're", "we are")
                    .replace("We'd", "We would").replace("that's", "that is")
                    .replace("couldn't", "could not").replace("I'd", "I would")
                    .replace("what's", "what is")
                    .replace("what you actually want out of it.",
                             "what you actually want out of it \u2014 quite "
                             "soon \u2014 please!"))

    subject = "Sponsoring {} at SVNIT".format(ctx.get("event", "our event"))         if company == "your team" else "{} sponsorship, {}".format(
            ctx.get("event", "our event"), company)
    if ctx["redo"]:
        subject = "a note about {}".format(ctx.get("event", "our event"))
    return {"subject": subject[:70], "body": body, "claims": claims,
            "suggested_tier": tier["name"]}


# ---------------------------------------------------------------------------
# humanize: strip the obvious tells, nothing more
# ---------------------------------------------------------------------------

ROBOTIC = (
    r"I hope this email finds you well[.!]?\s*",
    r"I am reaching out to explore a mutually beneficial partnership[.!]?\s*",
    r"I'm reaching out[^.!\n]*[.!]\s*",
)


@register_fake("humanize")
def fake_humanize(system: str, messages: list[dict]):
    text = messages[0].get("text", "")
    # The subject is one line. Letting `.` match newlines here made the greedy
    # group swallow the whole prompt and hand back only the sign-off as the
    # body, which the number check then rejected -- so the rewrite never
    # actually ran offline.
    match = re.search(r"Subject: ([^\n]+)\n\n(.+)$", text, re.S)
    subject = match.group(1).strip() if match else "hello"
    body = match.group(2).strip() if match else text
    for pattern in ROBOTIC:
        body = re.sub(pattern, "", body, flags=re.I)
    body = body.replace("!", ".").replace(" -- ", ", ").replace("—", ",")
    body = re.sub(r"\s+([,.])", r"\1", body)          # " ," left by the dashes
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return {"subject": subject.replace("!", ""), "body": body}


# ---------------------------------------------------------------------------
# reply classification, grounding, sponsor extraction
# ---------------------------------------------------------------------------

@register_fake("classify")
def fake_classify(system: str, messages: list[dict]):
    text = messages[0].get("text", "").lower()
    if any(w in text for w in ("out of office", "on leave", "auto-reply")):
        return {"label": "out_of_office", "confidence": 0.95, "extracted_ask": None}
    if any(w in text for w in ("not this year", "no budget", "we'll pass",
                               "we will pass", "not interested", "unfortunately")):
        return {"label": "declined", "confidence": 0.9, "extracted_ask": None}
    if any(w in text for w in ("call", "deck", "keen", "interested", "let's talk",
                               "lets talk", "sounds good")):
        return {"label": "interested", "confidence": 0.85, "extracted_ask": None}
    if "?" in text:
        ask = text[text.rfind("\n", 0, text.find("?")) + 1:text.find("?") + 1].strip()
        return {"label": "needs_info", "confidence": 0.8, "extracted_ask": ask[:200]}
    return {"label": "other", "confidence": 0.4, "extracted_ask": None}


@register_fake("ground")
def fake_ground(system: str, messages: list[dict]):
    from .verify import overlap  # noqa: PLC0415
    text = messages[0].get("text", "")
    claim = text.split("\n")[0].replace("Claim:", "").strip()
    excerpts = text.split("Excerpts:", 1)[-1]
    ratio = overlap(claim, excerpts)
    return {"supported": ratio >= 0.5, "why": "overlap {:.2f}".format(ratio)}


@register_fake("sponsors")
def fake_sponsors(system: str, messages: list[dict]):
    """Pull capitalised words after the word 'sponsor'."""
    text = messages[0].get("text", "")
    found = re.findall(r"[Ss]ponsor[eds]*(?:\s+by)?[:\s]+([A-Z][A-Za-z0-9&.\- ]{2,30})",
                       text)
    return {"event": None, "sponsors": [f.strip() for f in found][:10]}
