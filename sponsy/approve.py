"""The approval queue: how a draft becomes an email, and how a correction is kept.

[Send] delivers and advances the ladder. [Edit] takes your rewrite, stores the
before/after pair in `corrections` (the few-shot set the drafter learns from
next run) and sends your version. [Redo] takes an instruction and asks the job
for a new draft; the old card is retired. [Skip] parks the prospect for three
days. [Never] declines permanently and blocks the address.

Every verdict rewrites the Telegram card in place and removes its buttons, so
what you see in the chat is the state of the database, not a history of taps.
"""

from __future__ import annotations

import json
from datetime import date

from . import approvals, store
from .models import CLOSED, COLD_KINDS
from .telegram import esc

PREFIX = "d"

# Emoji are allowed here and only here: they are the tap targets on a phone.
# Written as escapes so the source file stays ASCII-safe on every console.
LABELS = {
    "send": "✅ Send",              # white heavy check mark
    "edit": "✏️ Edit",        # pencil
    "redo": "\U0001f504 Redo",          # counterclockwise arrows button
    "skip": "⏭️ Skip",        # next track button
    "never": "⛔ Never",            # no entry sign
}


def _data(draft_id, verdict: str) -> str:
    return "{}:{}:{}".format(PREFIX, draft_id, verdict)


def buttons(draft_id: int) -> list:
    return [[(LABELS["send"], _data(draft_id, "send")),
             (LABELS["edit"], _data(draft_id, "edit"))],
            [(LABELS["redo"], _data(draft_id, "redo")),
             (LABELS["skip"], _data(draft_id, "skip")),
             (LABELS["never"], _data(draft_id, "never"))]]


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------

def _detail_from_row(row) -> dict:
    """job.py stores the verifier's detail dict in drafts.checks, so a card can
    be rebuilt from the row alone (re-posts, the CLI)."""
    try:
        data = json.loads(row["checks"] or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) and "checks" in data else {}


def _grounded_count(claims: list, ungrounded: list) -> int:
    n = 0
    for claim in claims or []:
        if isinstance(claim, dict):
            n += int(bool(claim.get("grounded")))
        else:
            n += int(claim not in (ungrounded or []))
    return n


def draft_message(row, detail: dict | None = None) -> str:
    """The Telegram card for one queued draft."""
    detail = detail or _detail_from_row(row)
    checks = detail.get("checks") or {}
    claims = detail.get("claims") or []
    ungrounded = detail.get("ungrounded") or []
    words = detail.get("words") or len((row["body"] or "").split())
    sources = detail.get("sources") or 0
    # A soft failure is reported in its own line further down. Listing it here
    # too would stamp UNVERIFIED on a draft the verifier actually passed.
    soft = detail.get("soft_failures") or []
    failed = [name for name, ok in checks.items() if not ok and name not in soft]

    head = "<b>Draft for {}</b> ({})".format(esc(row["company"]),
                                             esc(row["prospect_id"]))
    if row["kind"] != "initial":
        head += " | touch {}".format(esc(row["kind"]))
    lines = [head,
             "To: {}".format(esc(row["email"] or "(no address)")),
             "Subject: {}".format(esc(row["subject"])),
             "",
             "<pre>{}</pre>".format(esc(row["body"]))]

    score = row["ai_tell_score"]
    status = "{} words | {}/{} claims traced to {} source(s) | human-score {}".format(
        words, _grounded_count(claims, ungrounded), len(claims), sources,
        "n/a" if score is None else "{:.2f}".format(float(score)))
    if row["humanized"]:
        status += " | humanized"
    if detail.get("attachment"):
        status += " | + {}".format(esc(detail["attachment"]))
    lines.append(status)

    if failed:
        lines.append("<b>UNVERIFIED</b> - failed: " + esc(", ".join(failed)))
        for claim in ungrounded:
            lines.append("  ungrounded: " + esc(str(claim)[:150]))
    soft = detail.get("soft_failures") or []
    issues = (detail.get("lint") or {}).get("issues") or []
    if ("sounds_human" in soft or "sounds_human" in failed) and issues:
        lines.append("reads a bit robotic: " + esc("; ".join(
            str(i) for i in issues[:3])))
    return "\n".join(lines)


def sent_message(row, out: dict, result: dict) -> str:
    """What the card becomes after [Send]."""
    touches = result.get("touches", "?")
    if out.get("blocked"):
        lines = ["<b>Held</b> - {}.".format(esc(out["blocked"])),
                 "Nothing was sent. Saved as {}".format(esc(out.get("eml", "?"))),
                 "touch {}/4".format(touches)]
    elif out.get("error"):
        lines = ["<b>Provider failed</b>: {}\nSaved as {}; send it by hand.".format(
            esc(out["error"]), esc(out.get("eml", "?")))]
        lines.append("touch {}/4".format(touches))
    else:
        lines = ["<b>Sent</b> to {} via {} | touch {}/4".format(
            esc(row["email"]), esc(out.get("channel", "?")), touches)]
        if out.get("attached"):
            lines.append("with {} attached".format(esc(out["attached"])))
    if out.get("attach_error"):
        lines.append("brochure NOT attached: {}".format(esc(out["attach_error"])))
    if result.get("next_kind"):
        lines.append("next: {} on {}".format(esc(result["next_kind"]),
                                             esc(result.get("next_touch"))))
    elif row["kind"] in COLD_KINDS and result.get("next_touch"):
        lines.append("ladder finished; marks cold on {}".format(
            esc(result["next_touch"])))
    if out.get("channel") == "manual" and not out.get("error"):
        lines.append("ready to send by hand: {}".format(esc(out.get("eml", "?"))))
        if out.get("attached"):
            # A mailto: link cannot carry a file, so with a brochure the .eml
            # is the only copy that is actually complete.
            lines.append("open the .eml -- the mailto link below has no "
                         "attachment")
        if out.get("mailto"):
            lines.append(esc(out["mailto"]))
    return "\n".join(lines)


def _retired(row, line: str) -> str:
    """A card with its buttons gone. The company stays so the chat history
    still reads; the draft text itself lives in the database."""
    return "<b>{}</b> ({})\n<i>{}</i>".format(esc(row["company"]),
                                              esc(row["prospect_id"]), line)


def _replace_card(ctx, row, text: str) -> None:
    """Edit the card in place; post a fresh message when the old one is gone.
    A Telegram hiccup here must never undo a verdict that is already written."""
    message_id = row["tg_message_id"]
    try:
        if message_id and ctx.notifier.edit(int(message_id), text, buttons=None):
            return
        ctx.notifier.send(text)
    except Exception as exc:
        print("[approve] could not update the card: {}".format(exc))


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

def _refusal(ctx, row, today: date) -> str | None:
    """Reasons a card may not be acted on any more. Checked before anything is
    written so a refused [Edit] does not leave a half-applied correction."""
    draft_id = int(ctx.target)
    if row is None:
        return "draft {} is gone".format(draft_id)
    if row["sent_at"]:
        return "already sent"
    if row["verdict"] == "superseded":
        return "draft {} was superseded; nothing sent".format(draft_id)
    prospect = store.get(ctx.db, row["prospect_id"])
    if prospect is None:
        return "prospect is gone"
    if store.is_blocked(ctx.db, prospect.email):
        return "that address is on the do-not-contact list; nothing sent"
    if prospect.status in CLOSED:
        store.skip_draft(ctx.db, draft_id, today)
        _replace_card(ctx, row, _retired(row, "stale: {} is {} now; nothing sent"
                                         .format(esc(prospect.id), prospect.status)))
        return "{} is {} now; stale card, nothing sent".format(prospect.id,
                                                               prospect.status)
    return None


def _send(ctx, event, voice, today: date, subject: str | None = None,
          body: str | None = None, edited: bool = False) -> str:
    db, cfg = ctx.db, ctx.cfg
    draft_id = int(ctx.target)
    row = store.get_draft(db, draft_id)
    refused = _refusal(ctx, row, today)
    if refused:
        return refused
    prospect = store.get(db, row["prospect_id"])

    subject = subject if subject is not None else row["subject"]
    body = body if body is not None else row["body"]
    changed = edited or subject != row["subject"] or body != row["body"]

    # Lazy so this module imports even while mail/ is missing or broken; the
    # import only matters at the moment a human taps [Send].
    from . import mail as mail_mod  # noqa: PLC0415

    out = mail_mod.deliver(
        cfg, event, voice, prospect, subject, body, row["kind"], today=today,
        in_reply_to=prospect.thread_id if row["kind"] == "needs_info" else None,
        thread_id=prospect.gmail_thread_id)
    result = store.mark_sent(db, draft_id, today, message_id=out.get("message_id"),
                             verdict="edited" if changed else "approved",
                             channel=out.get("channel", ""),
                             gmail_thread_id=out.get("thread_id"))
    if result.get("already_sent"):
        return "already sent"
    if row["action_id"]:
        db.set_verdict(row["action_id"], "approved")
    db.log_action(None, "send_email", prospect.id,
                  {"draft": draft_id, "kind": row["kind"], "channel": out.get("channel"),
                   "message_id": out.get("message_id"), "eml": out.get("eml"),
                   "edited": changed, "error": out.get("error")}, confidence=1.0)

    _replace_card(ctx, row, sent_message(row, out, result))

    channel = out.get("channel")
    if out.get("blocked"):
        return "held by SEND_ONLY_TO; saved as .eml, nothing sent"
    if out.get("error"):
        return "provider failed; saved as .eml"
    if channel == "gmail_api":
        return "sent via gmail"
    if channel == "smtp":
        return "sent via smtp"
    return "saved as .eml; send it by hand"


def _split_subject(text: str, default_subject: str) -> tuple[str, str]:
    """A first line 'Subject: ...' replaces the subject; the rest is the body."""
    text = (text or "").strip()
    if text.lower().startswith("subject:"):
        first, _, rest = text.partition("\n")
        return first.split(":", 1)[1].strip() or default_subject, rest.strip()
    return default_subject, text


def _edit(ctx, event, voice, today: date) -> str:
    draft_id = int(ctx.target)
    row = store.get_draft(ctx.db, draft_id)
    refused = _refusal(ctx, row, today)
    if refused:
        return refused
    subject, body = _split_subject(ctx.text, row["subject"])
    if not body:
        return "empty correction; nothing changed"
    store.record_correction(ctx.db, draft_id, subject, body)
    if row["action_id"]:
        ctx.db.set_verdict(row["action_id"], "corrected")
    return _send(ctx, event, voice, today, subject=subject, body=body, edited=True)


def _redo(ctx, regenerate, today: date) -> str:
    if regenerate is None:
        return "redo is not available here"
    draft_id = int(ctx.target)
    row = store.get_draft(ctx.db, draft_id)
    refused = _refusal(ctx, row, today)
    if refused:
        return refused
    instruction = (ctx.text or "").strip()
    if not instruction:
        return "empty instruction; nothing changed"
    try:
        new_id = regenerate(row, instruction)
    except Exception as exc:                       # the toast is the only feedback
        print("[approve] regenerate failed: {}".format(exc))
        return "could not regenerate: {}: {}".format(type(exc).__name__, exc)[:190]
    if new_id is None:
        return "could not regenerate"
    store.supersede_draft(ctx.db, draft_id, int(new_id))
    if row["action_id"]:
        ctx.db.set_verdict(row["action_id"], "corrected")
    _replace_card(ctx, row, _retired(row, "superseded by {}:{}".format(PREFIX, new_id)))
    return "new draft posted as {}:{}".format(PREFIX, new_id)


def _skip(ctx, today: date) -> str:
    draft_id = int(ctx.target)
    row = store.get_draft(ctx.db, draft_id)
    if row is None:
        return "draft {} is gone".format(draft_id)
    if row["sent_at"]:
        return "already sent; too late to skip"
    if row["verdict"] in ("skipped", "rejected", "superseded"):
        return "draft {} is already {}".format(draft_id, row["verdict"])
    out = store.skip_draft(ctx.db, draft_id, today)
    if row["action_id"]:
        ctx.db.set_verdict(row["action_id"], "rejected")
    _replace_card(ctx, row, _retired(row, "skipped; back on {}".format(
        esc(out["next_touch"]))))
    return "skipped; back on {}".format(out["next_touch"])


def _never(ctx) -> str:
    draft_id = int(ctx.target)
    row = store.get_draft(ctx.db, draft_id)
    if row is None:
        return "draft {} is gone".format(draft_id)
    store.mark_never(ctx.db, row["prospect_id"], reason="never, tapped in Telegram")
    if row["action_id"]:
        ctx.db.set_verdict(row["action_id"], "rejected")
    _replace_card(ctx, row, _retired(row, "never: {} marked declined, address blocked"
                                     .format(esc(row["company"]))))
    return "never: declined permanently"


def make_handler(event, voice, today: date | None = None, regenerate=None):
    """(handler, edit_handler) closed over the event, the voice and the clock.

    `today` is fixed for tests and pinned CLI runs; `serve` runs for days, so
    the default is read at tap time, not at registration."""

    def now() -> date:
        return today or date.today()

    def handler(ctx: approvals.Context) -> str:
        if ctx.verdict == "send":
            return _send(ctx, event, voice, now())
        if ctx.verdict in ("edit", "redo"):
            if (ctx.text or "").strip():
                return edit_handler(ctx)
            chat = ctx.chat_id or str(getattr(ctx.cfg, "telegram_chat_id", "") or "")
            approvals.set_pending_edit(ctx.db, chat, _data(ctx.target, ctx.verdict))
            return "send me the text"
        if ctx.verdict in ("skip", "reject"):
            return _skip(ctx, now())
        if ctx.verdict == "never":
            return _never(ctx)
        return "unknown verdict"

    def edit_handler(ctx: approvals.Context) -> str:
        if ctx.verdict == "redo":
            return _redo(ctx, regenerate, now())
        return _edit(ctx, event, voice, now())

    return handler, edit_handler


def register(event, voice, today: date | None = None, regenerate=None) -> None:
    handler, edit_handler = make_handler(event, voice, today, regenerate)
    approvals.register(PREFIX, handler, edit_handler)
