"""Recognising a bounce, so a dead address stops getting follow-ups.

Before this, a "Delivery Status Notification (Failure)" from mailer-daemon
was threaded to the prospect like any reply, classified "other", and left
for a human -- while the ladder went on mailing the same dead address three
more times. Every one of those is another hard bounce, and a bounce rate is
one of the first things a receiver looks at when deciding whether a sender is
a spammer.

Pure text in, a verdict out. The reply poll calls `parse` before the model
ever sees the message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DAEMON = re.compile(r"^(mailer-daemon|mail-daemon|mailerdaemon|postmaster|"
                    r"mail-delivery-subsystem|bounce[s]?|no-?reply-dsn)@", re.I)
SUBJECT = re.compile(
    r"(delivery status notification|undeliver(?:able|ed)|delivery (?:has )?failed|"
    r"failure notice|returned mail|mail delivery (?:failed|failure|subsystem)|"
    r"could not be delivered|delivery failure|message not delivered|"
    r"address not found|non[- ]?delivery|returned to sender)", re.I)
DELAY = re.compile(r"(\(delay\)|delayed|still trying|will (?:keep )?(?:re)?try|"
                   r"has not yet been delivered|temporar(?:y|ily))", re.I)
# Enhanced status codes (RFC 3463): 5.1.1, 4.2.2 ...
ENHANCED = re.compile(r"(?<![\d.])([245])\.(\d{1,3})\.(\d{1,3})(?![\d.])")
HARD_HINT = re.compile(
    r"(address not found|couldn'?t be found|could not be found|user unknown|"
    r"unknown user|no such user|does not exist|doesn'?t exist|mailbox unavailable|"
    r"mailbox not found|invalid recipient|recipient (?:address )?rejected|"
    r"account (?:has been |is )?disabled|account (?:is )?inactive|"
    r"domain (?:name )?not found|no mx|unrouteable|host or domain name not found|"
    r"not a valid (?:email|mailbox))", re.I)
SOFT_HINT = re.compile(r"(mailbox (?:is )?full|over quota|quota exceeded|"
                       r"insufficient storage|try again later|temporar)", re.I)
FINAL_RECIPIENT = re.compile(r"^(?:final|original)-recipient:\s*(?:rfc822;)?\s*"
                             r"<?([^\s<>;]+@[^\s<>;]+)>?", re.I | re.M)
FAILED_HEADER = re.compile(r"^x-failed-recipients:\s*(.+)$", re.I | re.M)
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass
class Bounce:
    hard: bool
    recipients: list[str] = field(default_factory=list)
    status: str = ""             # the enhanced code, "5.1.1"
    diagnostic: str = ""         # one line a human can read

    @property
    def label(self) -> str:
        return "bounced" if self.hard else "bounce_soft"


def looks_like_bounce(from_addr: str, subject: str, body: str = "") -> bool:
    """From a mail daemon, or a bounce-shaped subject backed by a status code
    or bounce wording in the text. The subject alone is not enough: a person
    can reply "Re: delivery failed?" about a parcel."""
    if DAEMON.match((from_addr or "").strip()):
        return True
    if not SUBJECT.search(subject or ""):
        return False
    text = body or ""
    return bool(ENHANCED.search(text) or HARD_HINT.search(text)
                or "diagnostic-code" in text.lower())


def _diagnostic(body: str, status: str) -> str:
    """The most informative line: the one carrying the status code, else the
    first line that sounds like a reason."""
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    for line in lines:
        if status and status in line:
            return line[:200]
    for line in lines:
        if HARD_HINT.search(line) or SOFT_HINT.search(line):
            return line[:200]
    return (lines[0] if lines else "")[:200]


def recipients_in(body: str, exclude: set[str]) -> list[str]:
    """Who the failed message was for. Structured DSN fields first; failing
    those, every address in the text that is not ours and not a daemon."""
    found: list[str] = []
    for match in FINAL_RECIPIENT.finditer(body or ""):
        found.append(match.group(1))
    for match in FAILED_HEADER.finditer(body or ""):
        found.extend(EMAIL.findall(match.group(1)))
    if not found:
        found = EMAIL.findall(body or "")
    out: list[str] = []
    for addr in found:
        addr = addr.strip(".,;:<>()[]'\"").lower()
        if not addr or addr in exclude or DAEMON.match(addr) or addr in out:
            continue
        out.append(addr)
    return out


def parse(from_addr: str, subject: str, body: str,
          ours: set[str] | None = None) -> Bounce | None:
    """A Bounce if this message is a delivery failure report, else None.

    Hard means permanent: a 5.x.x status, or wording like "address not
    found" with no 4.x.x status to contradict it. A delay notice ("still
    trying") is soft whatever else it says -- the provider has not given up.
    """
    if not looks_like_bounce(from_addr, subject, body):
        return None
    text = "{}\n{}".format(subject or "", body or "")
    codes = ["{}.{}.{}".format(*m.groups()) for m in ENHANCED.finditer(text)]
    permanent = [c for c in codes if c.startswith("5.")]
    transient = [c for c in codes if c.startswith("4.")]
    delayed = bool(DELAY.search(subject or "")) and not permanent
    if delayed:
        hard = False
    elif permanent:
        hard = True
    elif transient:
        hard = False
    else:
        hard = bool(HARD_HINT.search(text)) and not SOFT_HINT.search(text)
    status = (permanent or transient or [""])[0]
    exclude = {a.strip().lower() for a in (ours or set()) if a}
    exclude.add((from_addr or "").strip().lower())
    return Bounce(hard=hard, recipients=recipients_in(body, exclude), status=status,
                  diagnostic=_diagnostic(body, status))
