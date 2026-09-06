"""Drafting. The model sees the research, the excerpts, our own tiers, and the
sender's voice. Nothing else: no open web, no prior companies, no memory.

Whatever it asserts about the company has to have come from the block of text
above it in the prompt, and it has to enumerate those assertions in `claims` so
the verifier can check them one by one.

The voice block is what makes the result readable as a person rather than a
template. It carries three things: how the sender writes (voice.yml), up to
three emails they actually sent, and the last few corrections they made to this
agent's drafts. The third is the one that improves on its own -- every time
someone taps [Edit] and rewrites a line, the pair is stored, and the next draft
sees it.
"""

from __future__ import annotations

import json
import re

from . import ladder, prompts, store
from .agent import structured
from .models import Draft, Excerpt, Research

# Bullet characters, bold/underline runs, and ATX headings. A model that
# formats an email is a model that forgot it is writing an email.
MARKDOWN = re.compile(r"^[ \t]*[-*+•·]\s+|\*\*|__|^#{1,6}\s+", re.M)
LEADING_SUBJECT = re.compile(r"^\s*subject:.*\n+", re.I)


def _clean_body(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    text = LEADING_SUBJECT.sub("", text)      # it goes in its own field
    text = MARKDOWN.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _dedupe_signoff(body: str, voice) -> str:
    """Models that are given a sign-off sometimes use it twice: once where the
    email ends and once again underneath. Keep the last one."""
    signoff = (getattr(voice, "signoff", "") or "").strip()
    first_line = next((ln.strip() for ln in signoff.split("\n") if ln.strip()), "")
    if not first_line or body.count(first_line) < 2:
        return body
    head, _, tail = body.rpartition(first_line)
    head = re.sub(re.escape(first_line) + r".*?(?=\n\n|\Z)", "", head, flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", (head + first_line + tail)).strip()


def contact_block(event) -> str:
    bits = [event.reply_to, event.phone, event.website]
    return ", ".join(b for b in bits if b) or "(none configured)"


def _excerpt_block(excerpts: list[Excerpt]) -> str:
    if not excerpts:
        return "(no sources were retrieved)"
    return "\n\n".join(e.block(i + 1) for i, e in enumerate(excerpts))


def fewshot_block(db, voice=None, limit: int = 3) -> str:
    """The sender's own corrections, as before/after pairs.

    This is the part of the prompt that gets better with use. `corrections`
    holds every [Edit] and every [Redo]: what the agent wrote, what the human
    sent instead, and -- for a redo -- what they asked for in words.
    """
    if db is None:
        return ""
    try:
        rows = store.recent_corrections(db, limit)
    except Exception:
        return ""
    if not rows:
        return ""
    pairs = []
    for row in rows:
        block = ["--- draft ---",
                 "Subject: {}".format(row.get("before_subject") or ""),
                 (row.get("before_body") or "").strip()]
        if (row.get("instruction") or "").strip():
            block.append('--- they asked for: "{}" ---'.format(
                row["instruction"].strip()))
        block += ["--- what they sent instead ---",
                  "Subject: {}".format(row.get("after_subject") or ""),
                  (row.get("after_body") or "").strip()]
        pairs.append("\n".join(block))
    return prompts.FEWSHOT_CORRECTIONS.format(pairs="\n\n".join(pairs))


def _extra_context(event, kind: str, previous: list[dict] | None,
                   research: Research, brochure: bool = False) -> str:
    bits: list[str] = []
    if brochure:
        bits.append(prompts.BROCHURE_NOTE)
    if research.thin:
        bits.append(prompts.THIN_RESEARCH_NOTE)
    if kind in ("nudge", "new_angle"):
        # A follow-up needs one thing the first email did not have. These are
        # ours, so they are true by construction -- but only if event.yml is.
        facts = []
        if event.registrations_so_far:
            facts.append("{} teams have registered so far".format(
                event.registrations_so_far))
        if event.confirmed_sponsors:
            facts.append("confirmed sponsors so far: {}".format(
                ", ".join(event.confirmed_sponsors)))
        if facts:
            bits.append("\nNew details you may use (ours, not theirs): "
                        + "; ".join(facts) + ".")
    if previous:
        bits.append(prompts.PREVIOUS_TOUCHES.format(previous="\n\n".join(
            "--- {} ---\nSubject: {}\n{}".format(p["kind"], p["subject"], p["body"])
            for p in previous)))
    return "\n".join(bits)


def draft_email(llm, cfg, db, prospect, research: Research, excerpts: list[Excerpt],
                event, voice, kind: str, run_id: int | None = None,
                previous: list[dict] | None = None, feedback: str = "",
                instruction: str = "",
                prev_draft: dict | None = None) -> tuple[Draft, float]:
    """One draft. `feedback` is the verifier's checklist on a retry;
    `instruction` + `prev_draft` is a [Redo] from Telegram."""
    step = ladder.BY_KIND.get(kind)
    shift = step.shift if step else prompts.NEEDS_INFO_SHIFT
    touch = step.touch if step else (prospect.touches or 0)

    # Whether this touch will actually carry the brochure, so the copy can say
    # "attached" instead of offering to send something already in the message.
    try:
        from .mail import message as message_mod  # noqa: PLC0415
        attachment = message_mod.attachment_for(cfg, kind)
        brochure = bool(attachment) and not message_mod.attachment_problem(attachment)
    except Exception:                       # mail/ optional deps, never fatal
        brochure = False

    instruction_block = ""
    if instruction and prev_draft:
        instruction_block = prompts.REDO_INSTRUCTION.format(
            instruction=instruction.strip(),
            prev_subject=prev_draft.get("subject", ""),
            prev_body=(prev_draft.get("body") or "").strip())
    elif instruction:
        instruction_block = ('\nThe sender asked for this change: "{}"'
                             .format(instruction.strip()))

    user = prompts.DRAFT_USER.format(
        contact_name=prospect.contact_name or "the team",
        company=prospect.company,
        research_json=json.dumps(research.to_json(), indent=2, ensure_ascii=False),
        excerpts=_excerpt_block(excerpts),
        event_name=event.event_name, date=event.date,
        summary=event.summary or "(no summary configured)",
        colleges=event.colleges,
        track_record=event.track_record_block(),
        tiers=event.tier_block(),
        extra_context=_extra_context(event, kind, previous, research, brochure),
        touch=touch, kind=kind, shift=shift,
        voice_block=voice.style_block(),
        examples_block=voice.examples_block(),
        fewshot_block=fewshot_block(db, voice),
        greeting=voice.greeting_for(prospect.contact_name),
        signoff=voice.signoff,
        contact_block=contact_block(event),
        instruction_block=instruction_block)

    if feedback:
        user += ("\n\nYour previous attempt was rejected by the verifier:\n"
                 + feedback + "\nFix exactly those problems. Do not add new claims.")

    data, cost = structured(llm, system=prompts.DRAFT_SYSTEM.format(
        max_words=voice.max_words), user=user, temperature=0.5, db=db,
        run_id=run_id, kind="draft_attempt", target=prospect.id)

    claims = data.get("claims") or []
    if isinstance(claims, str):
        claims = [claims]
    body = _dedupe_signoff(_clean_body(data.get("body")), voice)
    draft = Draft(
        prospect_id=prospect.id,
        kind=kind,
        subject=(data.get("subject") or "").strip()[:120],
        body=body,
        claims=[str(c).strip() for c in claims if str(c).strip()],
        suggested_tier=(data.get("suggested_tier") or "").strip(),
        confidence=research.confidence)
    return draft, cost


def previous_touches(db, prospect_id: str, limit: int = 3) -> list[dict]:
    """What we have already sent this prospect, oldest first. The drafter is
    shown these so a follow-up does not repeat the first email's opening line."""
    rows = db.query(
        "SELECT kind, subject, body FROM drafts WHERE prospect_id=? "
        "AND sent_at IS NOT NULL ORDER BY id DESC LIMIT ?", (prospect_id, limit))
    return [{"kind": r["kind"], "subject": r["subject"], "body": r["body"]}
            for r in reversed(rows)]
