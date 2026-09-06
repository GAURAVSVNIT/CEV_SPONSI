"""Gmail API: OAuth2 desktop flow, send as the user, thread-aware, and a
read-only listing of inbox replies.

One credential does both directions. Sends land in the user's Sent folder,
follow-ups thread under the first email, and replies are read with the same
token -- no IMAP password. Every google import happens inside the method that
needs it, so the rest of sponsy (and the whole test suite) runs without the
google packages installed.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.send",
          "https://www.googleapis.com/auth/gmail.readonly"]

PIP_HINT = "pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
AUTH_HINT = ("put the OAuth client JSON (Google Cloud console -> Credentials -> "
             "Desktop app) at {credentials} and run: python run.py gmail-auth")

MSGID = re.compile(r"<[^<>\s]+>")


class GmailNotConfigured(RuntimeError):
    """No usable token and no way to get one non-interactively."""


@dataclass
class RawMail:
    """One inbound message, transport-neutral. `refs` holds every Message-ID
    from In-Reply-To and References so store.match_thread can attribute it."""
    uid: str
    message_id: str
    from_addr: str
    subject: str
    body: str
    refs: list[str] = field(default_factory=list)
    thread_id: str | None = None
    received_at: str = ""


# ---------------------------------------------------------------------------
# parsing a users.messages.get(format="full") payload -- pure, testable
# ---------------------------------------------------------------------------

def _headers(payload: dict) -> dict[str, str]:
    """Header names lower-cased; the first occurrence wins."""
    out: dict[str, str] = {}
    for item in payload.get("headers") or []:
        name = str(item.get("name") or "").lower()
        if name and name not in out:
            out[name] = str(item.get("value") or "")
    return out


def _b64url(data: str) -> bytes:
    data = (data or "").strip()
    data += "=" * (-len(data) % 4)          # Gmail strips the padding
    try:
        return base64.urlsafe_b64decode(data)
    except (ValueError, TypeError):
        return b""


def _charset(part: dict) -> str:
    ctype = _headers(part).get("content-type", "")
    match = re.search(r'charset="?([\w.-]+)"?', ctype, re.I)
    return match.group(1) if match else "utf-8"


def text_body(payload: dict) -> str:
    """The first text/plain part, depth-first through nested multiparts.

    Gmail nests multipart/alternative inside multipart/mixed when there is an
    attachment, so the text part can sit two levels down. HTML-only mail
    yields "" and the caller falls back to Gmail's snippet.
    """
    if not payload:
        return ""
    mime = str(payload.get("mimeType") or "").lower()
    data = (payload.get("body") or {}).get("data")
    if mime == "text/plain" and data:
        raw = _b64url(data)
        try:
            text = raw.decode(_charset(payload), errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        return text.replace("\r\n", "\n").strip()
    for part in payload.get("parts") or []:
        text = text_body(part)
        if text:
            return text
    return ""


def _received_at(msg: dict, headers: dict[str, str]) -> str:
    stamp = msg.get("internalDate")
    if stamp:
        try:
            when = datetime.fromtimestamp(int(stamp) / 1000, tz=timezone.utc)
            return when.isoformat(timespec="seconds")
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    if headers.get("date"):
        try:
            return parsedate_to_datetime(headers["date"]).isoformat(timespec="seconds")
        except (ValueError, TypeError):
            pass
    return ""


def parse_gmail_message(msg: dict) -> RawMail:
    """users.messages.get(format="full") -> RawMail. No network, no google."""
    payload = msg.get("payload") or {}
    headers = _headers(payload)
    refs: list[str] = []
    for header in ("in-reply-to", "references"):
        for ref in MSGID.findall(headers.get(header, "")):
            if ref not in refs:
                refs.append(ref)
    return RawMail(
        uid=str(msg.get("id") or ""),
        message_id=headers.get("message-id", "").strip(),
        from_addr=parseaddr(headers.get("from", ""))[1],
        subject=headers.get("subject", ""),
        body=text_body(payload) or str(msg.get("snippet") or ""),
        refs=refs,
        thread_id=msg.get("threadId"),
        received_at=_received_at(msg, headers))


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------

def _google():
    """The three google imports, in one place, with one pip hint."""
    try:
        from google.auth.transport.requests import Request  # noqa: PLC0415
        from google.oauth2.credentials import Credentials  # noqa: PLC0415
    except ImportError as exc:
        raise GmailNotConfigured("Gmail API needs: " + PIP_HINT) from exc
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415
    except ImportError:
        InstalledAppFlow = None          # only needed for the interactive flow
    return Credentials, Request, InstalledAppFlow


class GmailClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.creds = None
        self._service = None

    @property
    def is_authorized(self) -> bool:
        return Path(self.cfg.gmail_token).exists()

    def _not_configured(self, why: str = "") -> GmailNotConfigured:
        hint = AUTH_HINT.format(credentials=self.cfg.gmail_credentials)
        return GmailNotConfigured((why + "; " if why else "") + hint)

    # -- auth --------------------------------------------------------------
    def authorize(self, interactive: bool = True) -> "GmailClient":
        """Load the cached token, refresh it if needed, or run the browser flow.

        Non-interactive callers (deliver, the reply poll, a cron) must never
        block on a browser, so without a token they fail fast -- and before
        the google packages are even imported, which keeps `doctor` and the
        tests honest on a machine that has neither.
        """
        token = Path(self.cfg.gmail_token)
        secrets = Path(self.cfg.gmail_credentials)
        can_run_flow = interactive and secrets.exists()
        if not token.exists() and not can_run_flow:
            raise self._not_configured(
                "no Gmail token at {}".format(token) if interactive
                else "Gmail is not authorised yet (no token at {})".format(token))

        Credentials, Request, InstalledAppFlow = _google()
        creds = None
        changed = False
        if token.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token), SCOPES)
            except (ValueError, OSError) as exc:
                if not can_run_flow:
                    raise self._not_configured(
                        "unreadable Gmail token {}: {}".format(token, exc)) from exc
                creds = None
        if creds is not None and not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    changed = True
                except Exception as exc:          # revoked, scope changed, offline
                    if not can_run_flow:
                        raise self._not_configured(
                            "Gmail token refresh failed ({}: {})".format(
                                type(exc).__name__, exc)) from exc
                    creds = None
            elif not can_run_flow:
                raise self._not_configured("Gmail token expired and has no refresh "
                                           "token")
            else:
                creds = None
        if creds is None:
            if InstalledAppFlow is None:
                raise GmailNotConfigured("the OAuth flow needs: " + PIP_HINT)
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
            creds = flow.run_local_server(port=0)
            changed = True
        if changed:
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_text(creds.to_json(), encoding="utf-8")
        self.creds = creds
        self._service = None
        return self

    @property
    def service(self):
        if self._service is None:
            if self.creds is None:
                self.authorize(interactive=False)
            try:
                from googleapiclient.discovery import build  # noqa: PLC0415
            except ImportError as exc:
                raise GmailNotConfigured("Gmail API needs: " + PIP_HINT) from exc
            # cache_discovery=False: the file cache needs oauth2client and only
            # prints a warning on every call without it.
            self._service = build("gmail", "v1", credentials=self.creds,
                                  cache_discovery=False)
        return self._service

    # -- calls -------------------------------------------------------------
    def profile(self) -> dict:
        return self.service.users().getProfile(userId="me").execute()

    def send(self, msg: EmailMessage, thread_id: str | None = None) -> dict:
        """Send as the authorised user. With `thread_id` (and the In-Reply-To /
        References headers build_message already set) Gmail files it under the
        first email instead of starting a new conversation."""
        raw = base64.urlsafe_b64encode(msg.as_bytes(policy=SMTP)).decode("ascii")
        body = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        resp = self.service.users().messages().send(userId="me", body=body).execute()
        return {"message_id": msg["Message-ID"], "gmail_id": resp.get("id"),
                "thread_id": resp.get("threadId")}

    def list_replies(self, since: date, limit: int = 40) -> list[RawMail]:
        """Inbox mail from other people since `since`, oldest first.

        Read-only: nothing is marked, labelled or moved. `-from:me` drops our
        own sent copies that Gmail also shows in threads.
        """
        query = "in:inbox after:{} -from:me".format(since.strftime("%Y/%m/%d"))
        users = self.service.users()
        listing = users.messages().list(userId="me", q=query,
                                        maxResults=limit).execute()
        out: list[RawMail] = []
        for ref in listing.get("messages") or []:
            full = users.messages().get(userId="me", id=ref["id"],
                                        format="full").execute()
            out.append(parse_gmail_message(full))
        out.reverse()                     # Gmail lists newest first
        return out
