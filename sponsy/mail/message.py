"""The email itself: headers, threading, the .eml on disk, the mailto link
and the clipboard. Nothing in this file talks to a network.

The message is built once and handed to whichever provider deliver() picks,
so the headers here are the headers a reply will come back with. Message-ID
in particular is what `store.match_thread` looks for in In-Reply-To and
References -- get it wrong and replies can never be attributed.
"""

from __future__ import annotations

import mimetypes
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

# Anything not safe in a filename on every OS becomes an underscore.
SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Which touches carry the brochure. The first email gets it because that is
# when a sponsor decides whether to keep reading, and a reply that answers
# their question gets it because they asked. A nudge does not: re-sending the
# same five-megabyte PDF three times is how a thread starts looking like spam,
# and it costs deliverability on every later email from the same address.
ATTACH_KINDS = ("initial", "needs_info")

# Gmail refuses at 25 MB and counts base64 overhead, so the real ceiling is
# nearer 18. Stop well short: a brochure that big is the wrong file anyway.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


def sender_address(cfg, event) -> str:
    """The address the mail is sent FROM.

    It has to be the account that actually does the sending: Gmail rewrites a
    From that is not the authorised user (or one of its verified aliases), and
    an SMTP relay rejects it outright. So the provider decides, and event.reply_to
    is only the last resort for the manual .eml path.
    """
    if cfg.resolved_mail_provider() == "gmail_api" and cfg.gmail_sender:
        return cfg.gmail_sender
    return cfg.smtp_from or cfg.smtp_user or event.reply_to or ""


def _domain(addr: str) -> str:
    return addr.split("@")[-1] if "@" in (addr or "") else "localhost"


def _addr(name: str, address: str) -> str:
    """'Name <addr>' with the quoting the standard wants. A contact called
    "Sharma, Devi" must not turn into two recipients."""
    name, address = (name or "").strip(), (address or "").strip()
    if not address:
        return name
    return formataddr((name, address))


def attachment_for(cfg, kind: str) -> Path | None:
    """The brochure, when there is one and this touch should carry it."""
    if kind not in ATTACH_KINDS:
        return None
    return cfg.brochure_file


def attachment_problem(path) -> str:
    """Why this file cannot be attached, or "" if it can be.

    Called before the send so the reason reaches the card. An unattachable
    brochure never stops the email -- a pitch with no PDF is worth far more
    than no pitch at all.
    """
    if path is None:
        return ""
    path = Path(path)
    if not path.is_file():
        return "{} is not there".format(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        return "cannot read {}: {}".format(path.name, exc)
    if size == 0:
        return "{} is empty".format(path.name)
    if size > MAX_ATTACHMENT_BYTES:
        return "{} is {:.1f} MB; keep it under {} MB".format(
            path.name, size / 1048576, MAX_ATTACHMENT_BYTES // 1048576)
    return ""


def attach(msg: EmailMessage, path: Path) -> None:
    """Add one file to an already-built message, guessing its type."""
    path = Path(path)
    ctype, encoding = mimetypes.guess_type(path.name)
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, _, subtype = ctype.partition("/")
    msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                       filename=path.name)


def build_message(cfg, event, voice, prospect, subject: str, body: str,
                  in_reply_to: str | None = None,
                  attachment: Path | None = None,
                  kind: str = "initial") -> EmailMessage:
    """A plain-text EmailMessage ready for any provider.

    `in_reply_to` is the Message-ID of OUR previous email to this prospect
    (prospect.thread_id); setting In-Reply-To and References makes every
    follow-up land in the same conversation in their client and in Gmail.

    `attachment` turns the message into a multipart one. The caller checks
    `attachment_problem` first; a file that cannot be read is skipped here
    rather than allowed to raise, because the email matters more than the PDF.
    """
    msg = EmailMessage()
    sender = sender_address(cfg, event)
    sender_name = (voice.sender_name or event.sender_name or "").strip()
    msg["From"] = _addr(sender_name, sender)
    msg["To"] = _addr(prospect.contact_name, prospect.email)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=_domain(sender or event.reply_to))
    if event.reply_to:
        msg["Reply-To"] = event.reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    # multipart/alternative: the plain text first, the designed version second.
    # A client that cannot render HTML still gets a readable email, and a
    # message with no text part scores badly with spam filters.
    from .render import logo_files, render_html, render_text  # noqa: PLC0415

    full = kind == "initial"          # a follow-up does not repeat the whole deck
    has_brochure = attachment is not None
    try:
        company = (getattr(prospect, "company", "") or "").strip()
        text = render_text(event, voice, body, brochure=has_brochure, full=full)
        html_body = render_html(event, voice, body, brochure=has_brochure,
                                full=full, company=company)
    except Exception as exc:          # a template bug must not lose the email
        print("[mail] falling back to plain text: {}".format(exc))
        msg.set_content(body)
    else:
        msg.set_content(text)
        msg.add_alternative(html_body, subtype="html")
        # The logos travel inside the message as `related` parts. A URL would
        # be blocked by default in Gmail and Outlook and render as a broken
        # box, which is worse than no logo.
        html_part = msg.get_payload()[-1]
        for cid, path in logo_files(event, full=full).items():
            try:
                data = Path(path).read_bytes()
            except OSError as exc:
                print("[mail] skipping logo {}: {}".format(path, exc))
                continue
            subtype = Path(path).suffix.lstrip(".").lower() or "png"
            html_part.add_related(data, maintype="image",
                                  subtype="jpeg" if subtype == "jpg" else subtype,
                                  cid="<{}>".format(cid), filename=Path(path).name)

    if attachment is not None:
        try:
            attach(msg, attachment)
        except Exception as exc:                # unreadable, vanished, denied
            print("[mail] could not attach {}: {}".format(attachment, exc))
    return msg


def mailto_url(prospect, subject: str, body: str) -> str:
    """A link the sender can click to open the email in their own client."""
    query = urllib.parse.urlencode({"subject": subject, "body": body},
                                   quote_via=urllib.parse.quote)
    return "mailto:{}?{}".format(prospect.email, query)


def write_eml(cfg, msg: EmailMessage, prospect, kind: str, today: date | str) -> Path:
    """Write the message to the outbox as {pid}-{kind}-{date}.eml.

    This happens BEFORE any provider is tried, so an approved email always
    exists on disk whatever the mail server does next. A second send for the
    same prospect, kind and day (a reply to a second question, say) gets a
    numbered suffix rather than overwriting the first record.
    """
    day = today.isoformat() if hasattr(today, "isoformat") else str(today)
    stem = "{}-{}-{}".format(SAFE.sub("_", str(prospect.id)),
                             SAFE.sub("_", str(kind)), SAFE.sub("_", day))
    folder = Path(cfg.outbox)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (stem + ".eml")
    n = 2
    while path.exists():
        path = folder / "{}-{}.eml".format(stem, n)
        n += 1
    path.write_bytes(bytes(msg))
    return path


def to_clipboard(text: str) -> bool:
    """Best effort, never raises: pyperclip if installed, else the OS tool."""
    try:
        import pyperclip  # noqa: PLC0415
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    if sys.platform.startswith("win"):
        cmd = ["clip"]
    elif sys.platform == "darwin":
        cmd = ["pbcopy"]
    else:
        cmd = ["xclip", "-selection", "clipboard"]
    if not shutil.which(cmd[0]):
        return False
    try:
        subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=5)
        return True
    except Exception:
        return False
