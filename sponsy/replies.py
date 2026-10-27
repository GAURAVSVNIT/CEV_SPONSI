"""Reply detection: a read-only look at the inbox, one cheap classification
call, then a status transition that is decided in code.

The transport is separable from the decision. `classify_and_apply` takes a
sender, a subject and a body and knows nothing about mailboxes; `poll` knows
about mailboxes (Gmail API or IMAP, whichever `cfg.resolved_replies_provider()`
picks) and nothing about labels. `simulate-reply` on the command line uses the
first without the second, and the tests do the same.

One rule never reaches the model: a message that asks us to stop is declined,
permanently, and the address goes on the do-not-contact list. A classifier can
be wrong about tone; it must not be able to be wrong about that.

Bounces never reach it either. A delivery failure report is recognised in
code (sponsy/mail/bounces.py) and a hard one blocks the address and stops its
ladder at once: mailing a dead address again is the fastest way to teach
Gmail that this sender does not keep its list clean.
"""

from __future__ import annotations

import email
import imaplib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from . import ladder, prompts, store
from .agent import structured
from .models import REPLY_LABELS, ReplyEvent

JOB = "sponsorship"
SEEN_KEY = "gmail_seen"          # memory: JSON list of Gmail uids already looked at
SEEN_CAP = 500                   # enough for a dozen polls; replies.message_id
                                 # catches anything older
LAST_UID_KEY = "imap_last_uid"   # memory: highest IMAP uid already looked at
OOO_PUSH_DAYS = 7
SOFT_BOUNCE_LIMIT = 3            # this many soft bounces in a fortnight = hard

STOP = re.compile(
    r"\b(unsubscribe|stop contacting|stop emailing|remove me|do not contact|"
    r"take me off|opt out|opt-out)\b", re.I)
QUOTE_MARKER = re.compile(
    r"^(on .{5,80}wrote:|-{2,}\s*original message|from:\s)", re.I | re.M)
OOO_HINT = re.compile(
    r"\b(out of office|on leave|annual leave|auto[- ]?reply|automatic reply|"
    r"currently away|maternity|paternity)\b", re.I)

# RFC 3501 wants English month names; strftime("%b") follows the locale.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
          "Nov", "Dec")


# ---------------------------------------------------------------------------
# the inbound message shape
# ---------------------------------------------------------------------------

@dataclass
class _RawMail:
    """Stand-in for sponsy.mail.gmail_api.RawMail, same fields, so the IMAP
    path works before (or without) the mail package."""
    uid: str
    message_id: str
    from_addr: str
    subject: str
    body: str
    refs: list[str] = field(default_factory=list)
    thread_id: str | None = None
    received_at: str = ""


class _MailPackageMissing(RuntimeError):
    """Never raised. Stands in for GmailNotConfigured in an except clause when
    sponsy.mail cannot be imported, so that the real ImportError from the
    client factory is what the user sees."""


def _rawmail_class():
    try:
        from .mail.gmail_api import RawMail  # noqa: PLC0415
        return RawMail
    except ImportError:
        return _RawMail


def _not_configured_error():
    try:
        from .mail.gmail_api import GmailNotConfigured  # noqa: PLC0415
        return GmailNotConfigured
    except ImportError:
        return _MailPackageMissing


def _gmail_client(cfg):
    """Factory for the Gmail client. Module-level so tests can monkeypatch it;
    imported lazily so `google-*` is only needed when Gmail is actually used."""
    from .mail.gmail_api import GmailClient  # noqa: PLC0415
    return GmailClient(cfg)


# ---------------------------------------------------------------------------
# message parsing (RFC 822 messages: IMAP, and tests)
# ---------------------------------------------------------------------------

def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def body_text(message) -> str:
    """The text the human typed: text/plain parts only, quoted history and
    `>` lines stripped, capped so a forwarded thread cannot swamp the
    classifier's context (and its bill)."""
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and \
                    "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True) or b""
                parts.append(payload.decode(part.get_content_charset() or "utf-8",
                                            errors="replace"))
    else:
        payload = message.get_payload(decode=True) or b""
        parts.append(payload.decode(message.get_content_charset() or "utf-8",
                                    errors="replace"))
    return strip_quoted("\n".join(parts))


def strip_quoted(text: str) -> str:
    """What the person typed, without the quoted history under it.

    Applied to every transport. It matters more now that our own emails end
    with an opt-out line: a reply that quotes it back must not read as the
    person asking us to stop.
    """
    text = (text or "").replace("\r\n", "\n")
    cut = QUOTE_MARKER.search(text)
    if cut and cut.start() > 40:          # a reply that is ONLY a quote keeps it
        text = text[:cut.start()]
    text = "\n".join(line for line in text.split("\n") if not line.startswith(">"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:2000]


def dsn_text(message) -> str:
    """The machine-readable half of a bounce (message/delivery-status, and
    X-Failed-Recipients), which body_text skips because it is not text/plain.
    It names the failed recipient and the status code exactly."""
    out: list[str] = []
    failed = message.get("X-Failed-Recipients")
    if failed:
        out.append("X-Failed-Recipients: " + str(failed))
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() != "message/delivery-status":
                continue
            payload = part.get_payload()
            blocks = payload if isinstance(payload, list) else [payload]
            for block in blocks:
                out.append(str(block))
    return "\n".join(out)


def references_of(message) -> list[str]:
    """Every Message-ID this message points back at, In-Reply-To first."""
    refs: list[str] = []
    for header in ("In-Reply-To", "References"):
        value = message.get(header, "") or ""
        for ref in re.findall(r"<[^>]+>", value):
            if ref not in refs:
                refs.append(ref)
    return refs


def _received_at(message) -> str:
    try:
        return parsedate_to_datetime(message.get("Date", "")).isoformat()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def classify(llm, cfg, db, from_addr: str, subject: str, body: str,
             run_id: int | None = None) -> dict:
    """{label, confidence, extracted_ask, rule}. Two rules run before the
    model and short-circuit it; everything else is one cheap-model call."""
    if STOP.search(body) or STOP.search(subject):
        return {"label": "declined", "confidence": 1.0, "extracted_ask": None,
                "rule": "explicit opt-out"}
    if OOO_HINT.search(subject):
        return {"label": "out_of_office", "confidence": 0.9, "extracted_ask": None,
                "rule": "auto-reply subject"}
    data, _cost = structured(
        llm, system=prompts.CLASSIFY_SYSTEM,
        user=prompts.CLASSIFY_USER.format(from_addr=from_addr, subject=subject,
                                          body=body[:1500]),
        model=cfg.resolved_models()[1], temperature=0.0,
        db=db, run_id=run_id, kind="classify_reply")
    label = str(data.get("label", "other")).strip().lower()
    if label not in REPLY_LABELS:
        label = "other"
    ask = data.get("extracted_ask")
    return {"label": label, "confidence": _as_float(data.get("confidence")),
            "extracted_ask": str(ask).strip() if ask else None, "rule": "model"}


def apply_label(db, prospect, label: str, today: date) -> str:
    """Status transitions live here, not in the prompt. Returns a one-line
    description of what was done, for the audit log and the card."""
    pid = prospect.id
    if prospect.status == "declined" and label != "declined":
        # They said no once. A later "sounds interesting" from an auto-forward
        # or a colleague does not reopen them; the reply is still recorded and
        # a human still sees it.
        return "declined is permanent; left alone"
    if label == "interested":
        store.set_next_touch(db, pid, None)
        cancelled = store.cancel_pending(db, pid, "replied interested")
        store.set_status(db, pid, "interested")
        return "ladder cancelled ({} queued draft(s) dropped); a human takes over".format(
            cancelled)
    if label == "declined":
        store.set_next_touch(db, pid, None)
        store.cancel_pending(db, pid, "replied declined")
        store.block_email(db, prospect.email, "replied declined")
        store.set_status(db, pid, "declined")
        return "ladder cancelled; declined is permanent"
    if label == "out_of_office":
        store.set_next_touch(db, pid, ladder.push(OOO_PUSH_DAYS, today))
        return "pushed {} days; not counted as a touch".format(OOO_PUSH_DAYS)
    if label == "needs_info":
        if prospect.status == "interested":
            return "question noted; a human already owns this one"
        if prospect.status != "replied":
            store.set_status(db, pid, "replied")
        store.set_next_touch(db, pid, None)
        return "reply drafted for approval"
    return "left alone for a human to read"


def classify_and_apply(llm, cfg, db, prospect, from_addr: str, subject: str,
                       body: str, message_id: str = "", uid: str = "",
                       today: date | None = None,
                       run_id: int | None = None) -> ReplyEvent:
    today = today or date.today()
    verdict = classify(llm, cfg, db, from_addr, subject, body, run_id=run_id)
    event = ReplyEvent(prospect_id=prospect.id, message_id=message_id,
                       from_addr=from_addr, subject=subject, snippet=body[:400],
                       label=verdict["label"], confidence=verdict["confidence"],
                       extracted_ask=verdict["extracted_ask"])
    event.action = apply_label(db, prospect, event.label, today)
    store.record_reply(db, event, uid, event.action)
    db.log_action(run_id, "reply_" + event.label, prospect.id,
                  {"from": from_addr, "subject": subject, "rule": verdict["rule"],
                   "action": event.action}, confidence=event.confidence,
                  escalated=event.label in ("interested", "other"))
    return event


# ---------------------------------------------------------------------------
# bounces
# ---------------------------------------------------------------------------

def _our_addresses(cfg) -> set[str]:
    return {a.strip().lower() for a in (
        getattr(cfg, "gmail_sender", ""), getattr(cfg, "smtp_user", ""),
        getattr(cfg, "smtp_from", ""), getattr(cfg, "imap_user", "")) if a and "@" in a}


def _bounced_prospect(db, mail, bounce) -> tuple[str | None, str]:
    """(prospect id, the address that failed). The thread first, then any
    failed recipient that is one of our prospects."""
    pid = store.match_thread(db, list(mail.refs or []), "",
                             gmail_thread_id=getattr(mail, "thread_id", None))
    for addr in bounce.recipients:
        row = db.one("SELECT id FROM prospects WHERE lower(trim(email))=?", (addr,))
        if row and (pid is None or row["id"] == pid):
            return row["id"], addr
    if pid:
        prospect = store.get(db, pid)
        return pid, (prospect.email if prospect else "")
    return None, ""


def apply_bounce(db, mail, bounce, pid: str, address: str, uid: str,
                 run_id: int | None = None) -> ReplyEvent:
    """Record a delivery failure against its prospect and act on it."""
    prospect = store.get(db, pid)
    label = bounce.label
    if not bounce.hard:
        since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(
            timespec="seconds")
        recent = db.one("SELECT COUNT(*) n FROM replies WHERE prospect_id=? AND "
                        "label='bounce_soft' AND received_at >= ?", (pid, since))["n"]
        if recent + 1 >= SOFT_BOUNCE_LIMIT:
            label = "bounced"              # a mailbox that is always full is gone
    if label == "bounced":
        status = bounce.status if bounce.status not in bounce.diagnostic else ""
        why = " ".join(p for p in ("bounce:", status, bounce.diagnostic) if p)
        out = store.bounced(db, pid, address, why[:300])
        action = "hard bounce: {} blocked, ladder stopped".format(address or "address")
        if out.get("reset"):
            action += "; the touch was given back, so a new address starts at touch 1"
    else:
        action = "delivery delayed or refused for now; the provider keeps trying"
    event = ReplyEvent(prospect_id=pid, message_id=(mail.message_id or "").strip(),
                       from_addr=mail.from_addr or "", subject=mail.subject or "",
                       snippet=(bounce.diagnostic or (mail.body or ""))[:400],
                       label=label, confidence=1.0, extracted_ask=None, action=action)
    store.record_reply(db, event, uid, action)
    db.log_action(run_id, "reply_" + label, pid,
                  {"from": mail.from_addr, "address": address, "status": bounce.status,
                   "diagnostic": bounce.diagnostic, "action": action},
                  confidence=1.0, escalated=label == "bounced")
    return event


# ---------------------------------------------------------------------------
# the per-mail loop, shared by both transports
# ---------------------------------------------------------------------------

def _handle_mails(llm, cfg, db, mails, today: date, run_id: int | None,
                  is_seen, remember) -> list[ReplyEvent]:
    """Route each fetched message to its prospect and act on it.

    `is_seen(uid)` / `remember(uid)` are the transport's own memory (a uid
    list for Gmail, a high-water mark for IMAP). Every uid is remembered as
    soon as it has been looked at, whatever happens next: a message that
    fails to classify is logged and left for a human, not retried every day.
    """
    from .mail import bounces  # noqa: PLC0415

    ours = _our_addresses(cfg)
    events: list[ReplyEvent] = []
    for mail in mails:
        uid = str(mail.uid or "")
        if uid and is_seen(uid):
            continue
        if uid:
            remember(uid)
        message_id = (mail.message_id or "").strip()
        if store.seen_message(db, message_id):
            continue
        bounce = bounces.parse(mail.from_addr or "", mail.subject or "",
                               mail.body or "", ours=ours)
        if bounce is not None:
            pid, address = _bounced_prospect(db, mail, bounce)
            if pid:
                try:
                    events.append(apply_bounce(db, mail, bounce, pid, address, uid,
                                               run_id=run_id))
                except Exception as exc:  # noqa: BLE001
                    print("[sponsorship] bounce {} failed: {}".format(
                        message_id or uid, exc))
            continue                       # never classified by the model
        pid = store.match_thread(db, list(mail.refs or []), mail.from_addr or "",
                                 gmail_thread_id=getattr(mail, "thread_id", None))
        if not pid:
            continue                       # not a reply to us
        prospect = store.get(db, pid)
        if prospect is None:
            continue
        try:
            events.append(classify_and_apply(
                llm, cfg, db, prospect, mail.from_addr or "", mail.subject or "",
                strip_quoted(mail.body or ""), message_id=message_id, uid=uid,
                today=today, run_id=run_id))
        except Exception as exc:          # one bad message must not eat the rest
            print("[sponsorship] reply {} failed: {}".format(message_id or uid, exc))
            db.log_action(run_id, "reply_error", pid,
                          {"message_id": message_id, "uid": uid,
                           "error": str(exc)[:300]}, escalated=True)
    return events


# ---------------------------------------------------------------------------
# transport: Gmail API
# ---------------------------------------------------------------------------

def _seen_uids(db) -> list[str]:
    raw = db.memory_get(JOB, SEEN_KEY)
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        data = []
    return [str(x) for x in data] if isinstance(data, list) else []


def _poll_gmail(llm, cfg, db, since: date, limit: int, today: date,
                run_id: int | None) -> list[ReplyEvent]:
    not_configured = _not_configured_error()
    try:
        client = _gmail_client(cfg)
        client = client.authorize(interactive=False) or client
        mails = client.list_replies(since=since, limit=limit)
    except not_configured as exc:
        print("[sponsorship] Gmail reply polling is not set up ({}); run "
              "`python run.py gmail-auth`, or set REPLIES_PROVIDER=imap|none"
              .format(exc))
        return []

    seen = _seen_uids(db)
    seen_set = set(seen)

    def remember(uid: str) -> None:
        if uid in seen_set:
            return
        seen.append(uid)
        seen_set.add(uid)
        del seen[:-SEEN_CAP]
        db.memory_set(JOB, SEEN_KEY, json.dumps(seen))

    return _handle_mails(llm, cfg, db, mails, today, run_id,
                         is_seen=seen_set.__contains__, remember=remember)


# ---------------------------------------------------------------------------
# transport: IMAP (read-only)
# ---------------------------------------------------------------------------

def imap_fetch(cfg, since: date, limit: int = 40, last_uid: int = 0) -> list:
    """Messages newer than `last_uid` received since `since`, as RawMail.
    The mailbox is opened read-only: polling must never mark, move or delete."""
    RawMail = _rawmail_class()
    criterion = "(SINCE {:02d}-{}-{})".format(since.day, MONTHS[since.month - 1],
                                              since.year)
    mails: list = []
    conn = imaplib.IMAP4_SSL(cfg.imap_host)
    try:
        conn.login(cfg.imap_user, cfg.imap_password)
        conn.select(cfg.imap_folder, readonly=True)
        typ, data = conn.uid("SEARCH", None, criterion)
        if typ != "OK":
            return []
        uids = (data[0] or b"").split()
        for raw_uid in uids[-limit:]:
            uid = int(raw_uid)
            if uid <= last_uid:
                continue
            typ, payload = conn.uid("FETCH", raw_uid, "(RFC822)")
            if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            message = email.message_from_bytes(payload[0][1])
            mails.append(RawMail(
                uid=str(uid),
                message_id=(message.get("Message-ID") or "").strip(),
                from_addr=parseaddr(message.get("From", ""))[1],
                subject=_decode(message.get("Subject")),
                body="\n".join(t for t in (body_text(message), dsn_text(message)) if t),
                refs=references_of(message),
                thread_id=None,
                received_at=_received_at(message)))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return mails


def _poll_imap(llm, cfg, db, since: date, limit: int, today: date,
               run_id: int | None) -> list[ReplyEvent]:
    last_uid = int(db.memory_get(JOB, LAST_UID_KEY) or 0)
    mails = imap_fetch(cfg, since, limit=limit, last_uid=last_uid)
    high = {"uid": last_uid}

    def remember(uid: str) -> None:
        # uids only grow within a mailbox, so a high-water mark is enough
        high["uid"] = max(high["uid"], int(uid))
        db.memory_set(JOB, LAST_UID_KEY, str(high["uid"]))

    return _handle_mails(llm, cfg, db, mails, today, run_id,
                         is_seen=lambda uid: int(uid) <= last_uid,
                         remember=remember)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def poll(llm, cfg, db, today: date | None = None, run_id: int | None = None,
         days: int = 30, limit: int = 40) -> list[ReplyEvent]:
    """Look at the inbox and act on replies to our emails. Returns the replies
    it acted on; never sends anything and never mutates the mailbox."""
    provider = cfg.resolved_replies_provider()
    if provider == "none":
        return []
    today = today or date.today()
    since = today - timedelta(days=days)
    if provider == "gmail_api":
        return _poll_gmail(llm, cfg, db, since, limit, today, run_id)
    if provider == "imap":
        return _poll_imap(llm, cfg, db, since, limit, today, run_id)
    return []
