"""What a failed send means, and therefore what to do next.

The old answer to every failure was "fall back to manual". That is right for
a laptop and wrong for a server: a 421 "try again later" from Gmail is not a
reason to give up on an email, and a 550 "this looks like spam" is a reason to
stop sending ANYTHING until a human has looked, not to try the next card.

  transient   the network or the server hiccuped     -> retry this one later
  throttle    the provider says slow down / quota      -> pause the whole queue
  recipient   this address does not exist               -> bounce: block it
  reputation  rejected as spam / failed authentication  -> pause until a human
                                                           resumes it
  auth        our credentials are wrong or expired      -> pause until fixed
  permanent   anything else the server refused for good -> give up on this one

Classified from the exception type and the SMTP / HTTP code and text, with
no import of smtplib internals or googleapiclient, so it is testable with
plain exceptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

ENHANCED = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")

RECIPIENT_CODES = ("5.1.1", "5.1.2", "5.1.3", "5.1.6", "5.1.10", "5.2.1", "5.4.1")
RECIPIENT_TEXT = re.compile(
    r"(user unknown|no such user|does not exist|address not found|mailbox "
    r"unavailable|invalid recipient|recipient address rejected|account (?:has been )?"
    r"disabled|unknown user|mailbox not found)", re.I)
THROTTLE_TEXT = re.compile(
    r"(rate ?limit|too many|try again later|temporarily (?:deferred|rejected|"
    r"blocked)|unusual rate|quota|sending limit|daily (?:user )?sending|"
    r"user-rate|ratelimitexceeded|limit exceeded|4\.7\.28|4\.7\.0)", re.I)
DAILY_TEXT = re.compile(r"(daily|5\.4\.5|sending limit|quota exceeded)", re.I)
REPUTATION_TEXT = re.compile(
    r"(spam|unsolicited|blocked|blacklist|blocklist|reputation|policy|"
    r"not authenticated|unauthenticated|5\.7\.26|5\.7\.27|5\.7\.25|dmarc|spf|dkim|"
    r"suspicious)", re.I)
AUTH_TEXT = re.compile(r"(authenticat|username and password|invalid_grant|"
                       r"invalid credentials|token (?:has been )?(?:expired|revoked)|"
                       r"unauthorized|login)", re.I)


@dataclass
class Failure:
    kind: str                    # transient | throttle | recipient | reputation | auth | permanent
    detail: str
    code: int = 0
    status: str = ""             # enhanced status, "5.1.1"
    pause: timedelta | None = None   # how long the whole queue waits (throttle)

    @property
    def stops_queue(self) -> bool:
        return self.kind in ("throttle", "reputation", "auth")


def _text(exc: BaseException) -> str:
    parts = [type(exc).__name__, str(exc)]
    for attr in ("smtp_error", "content", "reason", "error_details"):
        value = getattr(exc, attr, None)
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value:
            parts.append(str(value))
    return " ".join(parts)


def _smtp_code(exc: BaseException) -> int:
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        return code
    recipients = getattr(exc, "recipients", None)
    if isinstance(recipients, dict) and recipients:
        first = next(iter(recipients.values()))
        if isinstance(first, tuple) and first and isinstance(first[0], int):
            return first[0]
    return 0


def _http_status(exc: BaseException) -> int:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None) or getattr(exc, "status_code", None)
    try:
        return int(status or 0)
    except (TypeError, ValueError):
        return 0


def classify(exc: BaseException) -> Failure:
    text = _text(exc)
    # SMTPRecipientsRefused keeps the server's words per recipient.
    refused = getattr(exc, "recipients", None)
    for value in (refused.values() if isinstance(refused, dict) else ()):
        if isinstance(value, tuple) and len(value) > 1:
            said = value[1]
            text += " " + (said.decode("utf-8", "replace") if isinstance(said, bytes)
                           else str(said))
    detail = re.sub(r"\s+", " ", text).strip()[:300]
    name = type(exc).__name__
    match = ENHANCED.search(text)
    status = ".".join(match.groups()) if match else ""
    code = _smtp_code(exc)
    http = _http_status(exc)

    # -- ours to fix ---------------------------------------------------------
    if name in ("GmailNotConfigured", "SmtpNotConfigured", "SMTPAuthenticationError",
                "RefreshError") or http == 401:
        return Failure("auth", detail, code or http, status)
    if name == "ValueError" and "no email address" in text:
        return Failure("permanent", detail)

    # -- the Gmail API (HttpError) -------------------------------------------
    if http:
        if http == 429 or (http == 403 and THROTTLE_TEXT.search(text)):
            pause = (timedelta(hours=24) if DAILY_TEXT.search(text)
                     else timedelta(minutes=30))
            return Failure("throttle", detail, http, status, pause=pause)
        if http == 403 and AUTH_TEXT.search(text):
            return Failure("auth", detail, http, status)
        if http >= 500:
            return Failure("transient", detail, http, status)
        if http == 400 and re.search(r"invalid to header|invalid recipient", text, re.I):
            return Failure("recipient", detail, http, status)
        if http == 400 and REPUTATION_TEXT.search(text):
            return Failure("reputation", detail, http, status)
        return Failure("permanent", detail, http, status)

    # -- SMTP ----------------------------------------------------------------
    if code or status:
        first = str(code)[:1] if code else status[:1]
        if status in RECIPIENT_CODES or (first == "5" and RECIPIENT_TEXT.search(text)):
            return Failure("recipient", detail, code, status)
        if status == "5.4.5" or (first == "5" and DAILY_TEXT.search(text)
                                 and THROTTLE_TEXT.search(text)):
            return Failure("throttle", detail, code, status, pause=timedelta(hours=24))
        if first == "5" and (status.startswith("5.7") or REPUTATION_TEXT.search(text)):
            return Failure("reputation", detail, code, status)
        if code == 421 or status in ("4.7.0", "4.7.28") or (
                first == "4" and THROTTLE_TEXT.search(text)):
            # Google: on 4.7.28 stop for at least ten minutes. An hour is kinder.
            return Failure("throttle", detail, code, status,
                           pause=timedelta(minutes=60 if status == "4.7.28" else 30))
        if first == "4":
            return Failure("transient", detail, code, status)
        if first == "5":
            return Failure("permanent", detail, code, status)

    # -- everything else: the connection, DNS, a timeout ----------------------
    if name in ("SMTPServerDisconnected", "SMTPConnectError", "TimeoutError",
                "timeout", "ConnectionError", "ConnectionResetError",
                "ConnectionRefusedError", "ConnectionAbortedError", "gaierror",
                "OSError", "SSLError", "SSLEOFError", "ServerNotFoundError",
                "TransportError", "HttpLib2Error", "BrokenPipeError",
                "RemoteDisconnected", "IncompleteRead", "ReadTimeout",
                "ConnectTimeout"):
        return Failure("transient", detail)
    if isinstance(exc, (OSError, TimeoutError)):
        return Failure("transient", detail)
    return Failure("permanent", detail)


def retry_after(attempts: int) -> timedelta:
    """2, 4, 8, 16, 32 ... minutes, capped at three hours."""
    return timedelta(minutes=min(180, 2 ** max(1, attempts)))
