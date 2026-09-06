"""What actually happens when you tap [Send].

Three channels, one rule: the .eml is written to data/outbox BEFORE any
provider is tried, so an email a human approved is never lost to a mail
server having a bad day.

  gmail_api  OAuth token; sends as the user, threads properly (preferred)
  smtp       Gmail app password over STARTTLS 587
  manual     .eml + mailto link + clipboard; nothing leaves the machine

A provider failure is reported in the result, never raised: approve.py has
already recorded the verdict and must still edit the card and tell the human
what happened. The trigger on drafts.sent_at is what stops any of this
running without a verdict; this module just cooperates.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from .gmail_api import GmailClient, GmailNotConfigured, RawMail
from .message import (ATTACH_KINDS, attachment_for, attachment_problem,
                      build_message, mailto_url, to_clipboard, write_eml)
from .smtp import send_smtp

__all__ = ["deliver", "test_send", "build_message", "mailto_url", "write_eml",
           "to_clipboard", "send_smtp", "GmailClient", "GmailNotConfigured",
           "RawMail", "attachment_for", "attachment_problem", "ATTACH_KINDS"]


def deliver(cfg, event, voice, prospect, subject: str, body: str, kind: str,
            today: date | None = None, in_reply_to: str | None = None,
            thread_id: str | None = None) -> dict:
    """Build, persist, then send through the resolved provider.

    Returns {message_id, channel, eml, mailto, thread_id, copied, attached,
    error?, attach_error?}. `channel` is what really happened: a configured
    provider that failed reports "manual" plus an `error`, and the clipboard
    gets the text so the sender can paste it into their own client.
    """
    today = today or date.today()
    attachment = attachment_for(cfg, kind)
    attach_error = attachment_problem(attachment)
    if attach_error:
        # Say so, send anyway: a pitch with no PDF beats no pitch.
        attachment = None
    msg = build_message(cfg, event, voice, prospect, subject, body, in_reply_to,
                        attachment=attachment, kind=kind)
    eml = write_eml(cfg, msg, prospect, kind, today)
    result: dict = {"message_id": msg["Message-ID"], "eml": str(eml),
                    "mailto": mailto_url(prospect, subject, body),
                    "channel": "manual", "thread_id": None, "copied": False,
                    "attached": attachment.name if attachment else None}
    if attach_error:
        result["attach_error"] = attach_error
    if cfg.dry_run:
        # A dry run leaves the machine alone entirely: no provider and no
        # clipboard (the tests run this way and must not clobber yours).
        result["dry_run"] = True
        return result

    provider = cfg.resolved_mail_provider()
    to_addr = (getattr(prospect, "email", "") or "").strip()
    if provider in ("gmail_api", "smtp") and not cfg.may_send_to(to_addr):
        # Held, not failed: the .eml is written and the ladder still advances,
        # but nothing reaches an address that is not on the test allowlist.
        result["blocked"] = ("SEND_ONLY_TO is set and {} is not on it"
                             .format(to_addr or "(no address)"))
        result["copied"] = to_clipboard(
            "To: {}\nSubject: {}\n\n{}".format(to_addr, subject, body))
        return result
    try:
        if provider in ("gmail_api", "smtp") and not to_addr:
            raise ValueError("prospect {} has no email address".format(prospect.id))
        if provider == "gmail_api":
            out = GmailClient(cfg).authorize(interactive=False).send(
                msg, thread_id=thread_id)
            result["channel"] = "gmail_api"
            result["thread_id"] = out.get("thread_id")
            result["gmail_id"] = out.get("gmail_id")
            return result
        if provider == "smtp":
            send_smtp(cfg, msg)
            result["channel"] = "smtp"
            return result
    except Exception as exc:
        # You already approved this email. Losing it because the mail server
        # said no is the worst outcome, so fall back to the manual path and
        # say plainly what went wrong.
        result["error"] = "{}: {}".format(type(exc).__name__, exc)[:300]
    result["copied"] = to_clipboard(
        "To: {}\nSubject: {}\n\n{}".format(to_addr, subject, body))
    return result


def test_send(cfg, event, voice, to: str | None = None) -> dict:
    """One email to yourself through whatever [Send] would use. Goes through
    deliver() on purpose, so what you get is exactly what a prospect gets.

    `to` overrides the destination, which is the point when you are testing
    from a personal account before the club one is set up.
    """
    to_addr = (to or "").strip() or (event.reply_to or cfg.gmail_sender
                                     or cfg.smtp_user)
    target = SimpleNamespace(id="TEST", company="yourself",
                             contact_name=voice.sender_name, email=to_addr,
                             thread_id=None, gmail_thread_id=None)
    # Realistic prose, so the test email you receive looks exactly like the one
    # a sponsor gets: same template, same brochure, same footer. Only the
    # opening paragraph is stand-in text.
    tier = event.tiers[1] if len(event.tiers) > 1 else (
        event.tiers[0] if event.tiers else None)
    offer = ("Our {} tier at {} {:,} looks like the closest fit, though that is "
             "only a suggestion: the other tiers are open and we are glad to "
             "shape the package around what is worth most to you.".format(
                 tier.name, tier.currency, tier.amount)) if tier else ""
    body = ("Dear {name},\n\n"
            "This is a test of the {channel} channel, addressed to you. A real "
            "email opens here with two or three sentences about the company, "
            "taken from pages the agent actually fetched, and why that made us "
            "write to them rather than to a general list.\n\n"
            "We would like to invite you to sponsor {event}, our hackathon on "
            "{date}.\n\n{offer}").format(
                name=voice.sender_name or "there",
                channel=cfg.resolved_mail_provider(), event=event.event_name,
                date=event.date, offer=offer)
    out = deliver(cfg, event, voice, target, "sponsy test email", body, "initial")
    out["to"] = to_addr
    if not to_addr:
        out["error"] = ("no address to send to: set reply_to in event.yml, or "
                        "GMAIL_SENDER / SMTP_USER in .env")
    return out
