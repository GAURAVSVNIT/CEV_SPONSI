"""The outbox: approved emails, sent at a pace a receiver trusts.

A tap on [Send] used to mean "send now". With a real mail provider it now
means "send as soon as the policy in mail/pacing.py allows" -- usually that
is now, and the card says so; when it is not (the fourth tap in two minutes,
a Sunday, the day's cap), the card says when instead.

    approve._send   verdict recorded -> enqueue() -> dispatch() right away
    serve / web     dispatch() every few seconds
    run.py outbox   the same, from a cron or a terminal

dispatch() is the only code that hands an outbox row to a provider. It holds
a lease in the database, so `serve` and `web` sharing one file never send at
the same moment, and it reads every failure for what it means:

    transient   retry this email later (2, 4, 8 ... minutes, then give up
                to the manual path the way a failed send always has)
    throttle    the provider asked us to slow down: pause everything
    recipient   the address does not exist: block it, stop its ladder
    reputation  rejected as spam or unauthenticated: pause until a human
    auth        credentials broke: pause until a human
    permanent   refused for good: fall back to manual, as before

Sending also pauses itself when too many of the week's emails bounce -- the
moment a bad address list starts costing the account its reputation.

A crash between "handed to Gmail" and "recorded" is the one gap a queue
opens. A row stuck in `sending` is recovered on the next pass: Gmail is asked
whether a message with that Message-ID is in Sent; if not, the very same
bytes go out again under the same id.
"""

from __future__ import annotations

import email
import email.policy
import os
import random
import socket
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store
from .mail import errors as mail_errors
from .mail import pacing
from .models import CLOSED
from .state import utcnow
from .telegram import esc

JOB = "outbox"
LEASE = "outbox-dispatch"
LEASE_TTL = timedelta(minutes=5)
STALE_SENDING = timedelta(minutes=15)
MAX_ATTEMPTS = 6
PROVIDERS = ("gmail_api", "smtp")
FOLLOW_UPS = ("nudge", "new_angle", "last_touch")
HISTORY = timedelta(days=2)          # enough for a daily cap and an hourly one

# How many sends are between "handed to the provider" and "recorded" right
# now, in this process. runtime.Stopper reads it so SIGTERM never lands there.
_flight = {"n": 0}
_flight_lock = threading.Lock()


@contextmanager
def _in_flight():
    with _flight_lock:
        _flight["n"] += 1
    try:
        yield
    finally:
        with _flight_lock:
            _flight["n"] -= 1


def sending() -> bool:
    return _flight["n"] > 0


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# history the policy is computed from
# ---------------------------------------------------------------------------

def sent_times(db, since: datetime) -> list[datetime]:
    """Every real send (not .eml, not dry run) since `since`."""
    rows = db.query("SELECT sent_at FROM drafts WHERE sent_at >= ? AND channel IN "
                    "('gmail_api','smtp') ORDER BY sent_at", (_iso(since),))
    return [t for t in (_parse(r["sent_at"]) for r in rows) if t]


def first_send(db) -> datetime | None:
    """When this sender first really sent: day 0 of the warm-up."""
    row = db.one("SELECT MIN(sent_at) t FROM drafts WHERE channel IN "
                 "('gmail_api','smtp') AND sent_at IS NOT NULL")
    return _parse(row["t"]) if row else None


def planned_times(db, exclude: int | None = None) -> list[datetime]:
    rows = db.query("SELECT id, not_before FROM outbox WHERE state='queued'")
    return [t for t in (_parse(r["not_before"]) for r in rows
                        if r["id"] != exclude) if t]


# ---------------------------------------------------------------------------
# pause
# ---------------------------------------------------------------------------

def paused(db, now: datetime | None = None) -> dict | None:
    """{until, reason} while sending is paused; None otherwise. `until` ""
    means until someone resumes it."""
    until = db.memory_get(JOB, "paused_until")
    if until is None:
        return None
    reason = db.memory_get(JOB, "paused_reason") or ""
    if until:
        when = _parse(until)
        if when is None or when <= (now or _now()):
            resume(db, auto=True)
            return None
    return {"until": until, "reason": reason}


def pause(db, reason: str, until: datetime | None = None) -> None:
    db.memory_set(JOB, "paused_until", _iso(until) if until else "")
    db.memory_set(JOB, "paused_reason", reason[:300])
    db.log_action(None, "outbox_pause", "", {"reason": reason,
                                             "until": _iso(until) if until else None})


def resume(db, auto: bool = False) -> None:
    db.memory_delete(JOB, "paused_until")
    db.memory_delete(JOB, "paused_reason")
    if not auto:
        # A human looked at the bounce numbers; count only new ones from here.
        db.memory_set(JOB, "breaker_ack", utcnow())
    db.log_action(None, "outbox_resume", "", {"auto": auto})


# ---------------------------------------------------------------------------
# lease
# ---------------------------------------------------------------------------

def _owner() -> str:
    return "{}:{}:{}".format(socket.gethostname(), os.getpid(), threading.get_ident())


def _acquire(db, owner: str, now: datetime) -> bool:
    db.execute("INSERT OR IGNORE INTO locks (name, owner, expires_at) VALUES (?,?,?)",
               (LEASE, "", _iso(now - timedelta(seconds=1))))
    cur = db.execute("UPDATE locks SET owner=?, expires_at=? WHERE name=? "
                     "AND (owner=? OR expires_at < ?)",
                     (owner, _iso(now + LEASE_TTL), LEASE, owner, _iso(now)))
    return cur.rowcount == 1


def _release(db, owner: str) -> None:
    db.execute("UPDATE locks SET expires_at=? WHERE name=? AND owner=?",
               (_iso(_now() - timedelta(seconds=1)), LEASE, owner))


# ---------------------------------------------------------------------------
# threading the conversation
# ---------------------------------------------------------------------------

def threading_for(cfg, db, prospect, kind: str) -> tuple[str | None, list[str]]:
    """(In-Reply-To, References) for this email.

    An answer to their question replies to the thread they answered. A cold
    follow-up replies to our own last email when FOLLOWUPS_IN_THREAD is on,
    so it lands under the first one instead of arriving as a second cold
    email -- and its "Re:" subject is then the truth, not a trick.
    """
    if kind == "needs_info":
        return prospect.thread_id, []
    if kind in FOLLOW_UPS and getattr(cfg, "followups_in_thread", True):
        chain = [r["message_id"] for r in db.query(
            "SELECT message_id FROM drafts WHERE prospect_id=? AND sent_at IS NOT NULL "
            "AND message_id IS NOT NULL ORDER BY sent_at, id", (prospect.id,))]
        if chain:
            return chain[-1], chain[-12:]
    return None, []


# ---------------------------------------------------------------------------
# recording a send (shared with approve.py's immediate path)
# ---------------------------------------------------------------------------

def _edit_card(notifier, row, text: str) -> None:
    """Rewrite the draft's card; a Telegram hiccup never undoes a send."""
    if notifier is None:
        return
    try:
        message_id = row["tg_message_id"]
        if message_id and notifier.edit(int(message_id), text, buttons=None):
            return
        notifier.send(text)
    except Exception as exc:                       # noqa: BLE001
        print("[outbox] could not update the card: {}".format(exc))


def _say(notifier, text: str) -> None:
    if notifier is None:
        return
    try:
        notifier.send(text)
    except Exception as exc:                       # noqa: BLE001
        print("[outbox] could not notify: {}".format(exc))


def record_send(db, notifier, row, out: dict, today, edited: bool = False) -> dict:
    """mark_sent + the audit line + the card, for an email that has left (or
    been written as an .eml). Returns mark_sent's result."""
    from . import approve  # noqa: PLC0415  (approve imports this module)

    result = store.mark_sent(db, int(row["id"]), today,
                             message_id=out.get("message_id"),
                             verdict="edited" if edited else "approved",
                             channel=out.get("channel", ""),
                             gmail_thread_id=out.get("thread_id"))
    if result.get("already_sent"):
        return result
    db.log_action(None, "send_email", row["prospect_id"],
                  {"draft": int(row["id"]), "kind": row["kind"],
                   "channel": out.get("channel"), "to": out.get("to"),
                   "message_id": out.get("message_id"), "eml": out.get("eml"),
                   "edited": edited, "error": out.get("error"),
                   "attempts": out.get("attempts")}, confidence=1.0)
    _edit_card(notifier, row, approve.sent_message(row, out, result))
    return result


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------

def enqueue(cfg, db, draft_id: int, now: datetime | None = None,
            rng: random.Random | None = None) -> sqlite3.Row:
    """Put an approved draft in the outbox with its planned slot. The draft
    must already carry its verdict; a trigger refuses the row otherwise."""
    existing = store.queued_send(db, draft_id)
    if existing is not None:
        return existing
    now = now or _now()
    row = store.get_draft(db, draft_id)
    policy = pacing.Policy.from_cfg(cfg)
    times = sent_times(db, now - HISTORY) + planned_times(db)
    when, reason = pacing.next_slot(policy, times, now, first_send(db), rng=rng)
    hold = paused(db, now)
    if hold:
        reason = "sending is paused: " + hold["reason"]
    db.execute(
        "INSERT INTO outbox (draft_id, prospect_id, to_addr, state, queued_at, "
        "not_before, reason, updated_at) VALUES (?,?,?,'queued',?,?,?,?)",
        (draft_id, row["prospect_id"], (row["email"] or "").strip().lower(),
         _iso(now), _iso(when), reason, _iso(now)))
    return store.queued_send(db, draft_id)


def cancel(db, outbox_id: int, reason: str = "cancelled by a human") -> bool:
    cur = db.execute("UPDATE outbox SET state='cancelled', reason=?, updated_at=? "
                     "WHERE id=? AND state='queued'", (reason, utcnow(), outbox_id))
    if cur.rowcount:
        row = db.one("SELECT draft_id, prospect_id FROM outbox WHERE id=?", (outbox_id,))
        db.log_action(None, "cancel_send", row["prospect_id"],
                      {"draft": row["draft_id"], "reason": reason})
    return bool(cur.rowcount)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def _set(db, item_id: int, **fields) -> None:
    fields["updated_at"] = utcnow()
    cols = ", ".join("{}=?".format(k) for k in fields)
    db.execute("UPDATE outbox SET {} WHERE id=?".format(cols),
               tuple(fields.values()) + (item_id,))


def _bounce_breaker(cfg, db, now: datetime) -> str:
    """A reason to stop, when hard bounces pass BOUNCE_PAUSE_RATE of the
    week's real sends (and there are at least BOUNCE_PAUSE_MIN of them)."""
    since = now - timedelta(days=7)
    ack = _parse(db.memory_get(JOB, "breaker_ack"))
    if ack and ack > since:
        since = ack
    # Strictly after: a resume stamped in the same second as the bounces it
    # acknowledges must not count them again.
    bounces = db.one("SELECT COUNT(*) n FROM do_not_contact WHERE reason LIKE 'bounce:%' "
                     "AND created_at > ?", (_iso(since),))["n"]
    sends = len(sent_times(db, since))
    floor = max(1, int(getattr(cfg, "bounce_pause_min", 3)))
    rate = float(getattr(cfg, "bounce_pause_rate", 0.05))
    if bounces >= floor and sends and bounces / sends > rate:
        return ("{} of the {} emails sent since {} bounced ({:.0%}). A list with "
                "that many dead addresses is how a sender gets marked as a "
                "spammer; check the addresses, then resume".format(
                    bounces, sends, since.date().isoformat(), bounces / sends))
    return ""


def _recover(cfg, db, now: datetime) -> None:
    """Rows left in `sending` by a process that died mid-send."""
    stale = db.query("SELECT * FROM outbox WHERE state='sending' AND updated_at < ?",
                     (_iso(now - STALE_SENDING),))
    for item in stale:
        found = None
        if cfg.resolved_mail_provider() == "gmail_api" and item["message_id"]:
            try:
                from . import mail  # noqa: PLC0415
                found = mail.GmailClient(cfg).authorize(interactive=False).find_sent(
                    item["message_id"])
            except Exception as exc:               # noqa: BLE001
                print("[outbox] cannot check Sent for {} yet: {}".format(
                    item["message_id"], exc))
                continue                           # try again next pass
        row = store.get_draft(db, item["draft_id"])
        if found and row is not None and not row["sent_at"]:
            policy = pacing.Policy.from_cfg(cfg)
            record_send(db, None, row, {"message_id": item["message_id"],
                                        "channel": "gmail_api", "eml": item["eml"],
                                        "thread_id": found.get("threadId"),
                                        "to": item["to_addr"]},
                        policy.local(now).date(), edited=row["verdict"] == "edited")
            _set(db, item["id"], state="sent", sent_at=_iso(now), channel="gmail_api",
                 reason="found in Sent after an interrupted send")
            continue
        _set(db, item["id"], state="queued", not_before=_iso(now),
             reason="interrupted mid-send; retrying with the same Message-ID")


def dispatch(cfg, db, notifier, event, voice, now: datetime | None = None,
             rng: random.Random | None = None, limit: int = 5) -> list[dict]:
    """Send what is due, one policy check at a time. Never raises: this runs
    inside long-lived loops, and one bad row must not stop the next."""
    now = now or _now()
    owner = _owner()
    try:
        if not _acquire(db, owner, now):
            return []
    except sqlite3.Error as exc:
        print("[outbox] lease: {}".format(exc))
        return []
    try:
        return _dispatch(cfg, db, notifier, event, voice, now, rng, limit)
    except Exception as exc:                       # noqa: BLE001
        print("[outbox] dispatch failed: {}: {}".format(type(exc).__name__, exc))
        return []
    finally:
        try:
            _release(db, owner)
        except sqlite3.Error:
            pass


def _dispatch(cfg, db, notifier, event, voice, now, rng, limit) -> list[dict]:
    _recover(cfg, db, now)
    if paused(db, now):
        return []
    due = db.query("SELECT * FROM outbox WHERE state='queued' AND not_before <= ? "
                   "ORDER BY not_before, id LIMIT 25", (_iso(now),))
    if not due:
        return []
    policy = pacing.Policy.from_cfg(cfg)
    results: list[dict] = []
    for item in due:
        if len([r for r in results if r.get("sent")]) >= limit:
            break
        if cfg.paced:
            trip = _bounce_breaker(cfg, db, now)
            if trip:
                pause(db, trip)
                _say(notifier, "<b>Sending paused.</b> " + esc(trip) +
                     ". Resume with /resume, the web page, or "
                     "<code>python run.py outbox --resume</code>.")
                break
            when, reason = pacing.next_slot(policy, sent_times(db, now - HISTORY),
                                            now, first_send(db), rng=rng)
            if when > now:
                _set(db, item["id"], not_before=_iso(when), reason=reason)
                break
        out = _send_one(cfg, db, notifier, event, voice, item, now, policy)
        results.append(out)
        if out.get("stop"):
            break
    return results


def _send_one(cfg, db, notifier, event, voice, item, now, policy) -> dict:
    from . import mail  # noqa: PLC0415

    item_id = item["id"]
    row = store.get_draft(db, item["draft_id"])
    if row is None:
        _set(db, item_id, state="failed", reason="the draft is gone")
        return {"id": item_id, "state": "failed"}
    if row["sent_at"]:
        _set(db, item_id, state="sent", reason="already sent")
        return {"id": item_id, "state": "sent"}
    prospect = store.get(db, row["prospect_id"])
    to_addr = (getattr(prospect, "email", "") or "").strip().lower()
    stop = ""
    if prospect is None:
        stop = "the prospect is gone"
    elif prospect.status in CLOSED:
        stop = "{} is {} now".format(prospect.id, prospect.status)
    elif not to_addr:
        stop = "{} has no address any more".format(prospect.id)
    elif store.is_blocked(db, to_addr):
        stop = "{} is on the do-not-contact list".format(to_addr)
    if stop:
        _set(db, item_id, state="cancelled", reason=stop)
        _edit_card(notifier, row, "<b>{}</b> ({})\n<i>not sent: {}</i>".format(
            esc(row["company"]), esc(row["prospect_id"]), esc(stop)))
        return {"id": item_id, "state": "cancelled", "reason": stop}

    today = policy.local(now).date()
    edited = row["verdict"] == "edited"
    in_reply_to, references = threading_for(cfg, db, prospect, row["kind"])

    if not cfg.paced or not cfg.may_send_to(to_addr):
        # The provider went away (ALLOW_SEND=0, manual) or the test allowlist
        # now holds this address: the immediate path's rules apply.
        out = mail.deliver(cfg, event, voice, prospect, row["subject"], row["body"],
                           row["kind"], today=today, in_reply_to=in_reply_to,
                           references=references, thread_id=prospect.gmail_thread_id)
        out["to"] = to_addr
        record_send(db, notifier, row, out, today, edited=edited)
        _set(db, item_id, state="sent", sent_at=_iso(now), channel=out.get("channel"),
             message_id=out.get("message_id"), eml=out.get("eml"),
             reason=out.get("blocked") or "delivered on the immediate path")
        return {"id": item_id, "state": "sent", "sent": False, "out": out}

    if getattr(cfg, "check_mx", True) and not cfg.offline:
        from .mail import dns  # noqa: PLC0415
        status, why = dns.cached_mail_domain_status(db, dns.domain_of(to_addr), now=now)
        if status == "none":
            reason = "bounce-risk: {} takes no mail ({})".format(
                dns.domain_of(to_addr), why)
            store.block_email(db, to_addr, reason)
            store.cancel_pending(db, prospect.id, "address cannot receive mail")
            store.set_next_touch(db, prospect.id, None)
            _set(db, item_id, state="failed", reason=reason)
            _edit_card(notifier, row, "<b>{}</b> ({})\n<i>not sent: {}. The address "
                       "is blocked; add another one to continue.</i>".format(
                           esc(row["company"]), esc(prospect.id), esc(reason)))
            return {"id": item_id, "state": "failed", "reason": reason}

    try:
        cur = db.execute("UPDATE outbox SET state='sending', attempts=attempts+1, "
                         "to_addr=?, updated_at=? WHERE id=? AND state='queued'",
                         (to_addr, utcnow(), item_id))
    except sqlite3.IntegrityError as exc:           # trg_outbox_do_not_contact
        _set(db, item_id, state="cancelled", reason=str(exc))
        return {"id": item_id, "state": "cancelled", "reason": str(exc)}
    if cur.rowcount != 1:
        return {"id": item_id, "state": "skipped"}  # someone else took it
    attempts = int(item["attempts"] or 0) + 1

    with _in_flight():
        try:
            msg, out = _message_for(cfg, db, event, voice, item, row, prospect, today,
                                    in_reply_to, references)
            out.update(mail.transmit(cfg, msg, to_addr,
                                     thread_id=prospect.gmail_thread_id))
        except Exception as exc:                   # noqa: BLE001
            return _failed(cfg, db, notifier, event, voice, item, row, prospect,
                           today, exc, attempts, now)

        out.update(to=to_addr, attempts=attempts)
        record_send(db, notifier, row, out, today, edited=edited)
        _set(db, item_id, state="sent", sent_at=_iso(now), channel=out.get("channel"),
             reason="", last_error=None)
    return {"id": item_id, "state": "sent", "sent": True, "out": out}


def _message_for(cfg, db, event, voice, item, row, prospect, today, in_reply_to,
                 references):
    """The message to transmit: the exact bytes of an earlier attempt when
    there was one, so a retry is the same email under the same Message-ID."""
    from . import mail  # noqa: PLC0415

    eml = Path(item["eml"]) if item["eml"] else None
    if item["message_id"] and eml is not None and eml.is_file():
        msg = email.message_from_bytes(eml.read_bytes(), policy=email.policy.default)
        attached = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
        return msg, {"message_id": item["message_id"], "eml": str(eml),
                     "channel": "manual", "thread_id": None,
                     "attached": attached[0] if attached else None}
    msg, path, out = mail.prepare(cfg, event, voice, prospect, row["subject"],
                                  row["body"], row["kind"], today=today,
                                  in_reply_to=in_reply_to, references=references)
    _set(db, item["id"], message_id=out["message_id"], eml=str(path))
    return msg, out


def _failed(cfg, db, notifier, event, voice, item, row, prospect, today, exc,
            attempts: int, now: datetime) -> dict:
    failure = mail_errors.classify(exc)
    item_id = item["id"]
    print("[outbox] {} for {} (attempt {}): {}".format(
        failure.kind, prospect.id, attempts, failure.detail))
    db.log_action(None, "send_failed", prospect.id,
                  {"draft": int(row["id"]), "kind": failure.kind,
                   "detail": failure.detail, "attempt": attempts})
    fresh = db.one("SELECT eml, message_id FROM outbox WHERE id=?", (item_id,))
    company = esc(row["company"])

    if failure.kind == "recipient":
        reason = "bounce: {} {}".format(failure.status or failure.code, failure.detail)[:300]
        # Refused before it left, so no touch was spent: nothing to give back.
        store.bounced(db, prospect.id, prospect.email, reason, return_touch=False)
        _set(db, item_id, state="failed", reason=reason, last_error=failure.detail)
        _edit_card(notifier, row, "<b>{}</b> ({})\n<i>not sent: the server says {} "
                   "does not exist. Address blocked; add another to continue.</i>"
                   .format(company, esc(prospect.id), esc(prospect.email)))
        return {"id": item_id, "state": "failed", "kind": failure.kind}

    if failure.stops_queue:
        until = now + failure.pause if failure.pause else None
        what = {"throttle": "the provider asked us to slow down",
                "reputation": "the provider refused it as spam or unauthenticated",
                "auth": "the mail login failed"}[failure.kind]
        pause(db, "{}: {}".format(what, failure.detail), until=until)
        _set(db, item_id, state="queued",
             not_before=_iso(until or now), reason="waiting: " + what,
             last_error=failure.detail)
        hint = {"throttle": "It resumes on its own at {}.".format(
                    _iso(until)[:16].replace("T", " ") + " UTC" if until else "?"),
                "reputation": "Run <code>python run.py doctor</code> (SPF, DKIM, "
                              "DMARC), fix what it reports, then /resume.",
                "auth": "Fix the credentials (<code>python run.py gmail-auth</code> "
                        "or the SMTP app password), then /resume."}[failure.kind]
        _say(notifier, "<b>Sending paused</b>: {}.\n<code>{}</code>\n{} Nothing is "
             "lost; {} queued email(s) wait.".format(
                 esc(what), esc(failure.detail[:240]), hint,
                 db.one("SELECT COUNT(*) n FROM outbox WHERE state='queued'")["n"]))
        return {"id": item_id, "state": "queued", "kind": failure.kind, "stop": True}

    if failure.kind == "transient" and attempts < MAX_ATTEMPTS:
        later = now + mail_errors.retry_after(attempts)
        _set(db, item_id, state="queued", not_before=_iso(later),
             reason="retrying after: " + failure.detail[:160],
             last_error=failure.detail)
        return {"id": item_id, "state": "queued", "kind": failure.kind}

    # Permanent, or transient too many times: what a failed send always did.
    # The .eml is on disk; record it as a manual send so the ladder and the
    # card tell the truth, and say plainly what went wrong.
    out = {"message_id": fresh["message_id"], "eml": fresh["eml"], "channel": "manual",
           "thread_id": None, "to": prospect.email, "attempts": attempts,
           "error": "{} after {} attempt(s): {}".format(failure.kind, attempts,
                                                       failure.detail)[:300]}
    record_send(db, notifier, row, out, today, edited=row["verdict"] == "edited")
    _set(db, item_id, state="failed", reason="gave up; saved as .eml to send by hand",
         last_error=failure.detail)
    return {"id": item_id, "state": "failed", "kind": failure.kind}


# ---------------------------------------------------------------------------
# what the page and /outbox show
# ---------------------------------------------------------------------------

def status(cfg, db, now: datetime | None = None) -> dict:
    now = now or _now()
    policy = pacing.Policy.from_cfg(cfg)
    sent = sent_times(db, now - HISTORY)
    first = first_send(db)
    today = policy.local(now).date()
    cap = policy.cap_on(today, first)
    queued = db.query(
        "SELECT o.*, d.kind, d.subject, p.company FROM outbox o "
        "JOIN drafts d ON d.id = o.draft_id LEFT JOIN prospects p ON p.id = o.prospect_id "
        "WHERE o.state IN ('queued','sending') ORDER BY o.not_before, o.id")
    recent = db.query(
        "SELECT o.*, d.kind, d.subject, p.company FROM outbox o "
        "JOIN drafts d ON d.id = o.draft_id LEFT JOIN prospects p ON p.id = o.prospect_id "
        "WHERE o.state IN ('failed','cancelled') ORDER BY o.updated_at DESC LIMIT 10")
    week = now - timedelta(days=7)
    bounces = db.one("SELECT COUNT(*) n FROM do_not_contact WHERE reason LIKE 'bounce%' "
                     "AND created_at >= ?", (_iso(week),))["n"]
    return {
        "paced": cfg.paced,
        "policy": policy.describe(),
        "paused": paused(db, now),
        "sent_today": pacing.sends_today(policy, sent, now),
        "cap_today": cap,
        "daily_cap": policy.daily_cap,
        "warmup_day": (today - policy.local(first).date()).days if first else 0,
        "sends_7d": len(sent_times(db, week)),
        "bounces_7d": bounces,
        "in_window": policy.in_window(now),
        "queued": [dict(r) for r in queued],
        "recent": [dict(r) for r in recent],
        "unsubscribe": {"one_click": bool(getattr(cfg, "public_url", "")),
                        "public_url": getattr(cfg, "public_url", "")},
    }


def describe_wait(cfg, item, now: datetime | None = None) -> str:
    """'goes out about Mon 14:32 (spacing sends 90s+ apart)' for a card, in
    the sending time zone."""
    when = _parse(item["not_before"])
    reason = item["reason"] or ""
    tail = " ({})".format(reason) if reason else ""
    if when is None or when <= (now or _now()) + timedelta(seconds=60):
        return "goes out within a minute" + tail
    policy = pacing.Policy.from_cfg(cfg)
    local = policy.local(when)
    zone = getattr(policy.tz, "key", None) or local.strftime("%Z") or "local"
    return "goes out about {} {}{}".format(local.strftime("%a %d %b %H:%M"), zone, tail)
