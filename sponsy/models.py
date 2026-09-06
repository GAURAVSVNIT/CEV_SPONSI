"""Types and the status machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- status machine --------------------------------------------------------

STATUSES = ("new", "researched", "queued", "sent", "replied",
            "interested", "declined", "cold")

# declined is absorbing, and the database enforces it with a trigger as well as
# this table. A reply is ground truth about the world, so `interested`,
# `declined` and `replied` are reachable from every state that is not already
# absorbing -- people answer a first email while a follow-up is still queued.
# A *send* is ground truth too: the daily job walks new -> researched -> queued
# -> sent, but if a draft is ever approved straight off a `new` prospect the
# mail has still left, and leaving that prospect marked `new` with touches=1
# would be a lie about the world.
TRANSITIONS: dict[str, set[str]] = {
    "new":        {"researched", "queued", "sent", "replied", "interested",
                   "declined", "cold"},
    "researched": {"queued", "replied", "interested", "declined", "cold"},
    "queued":     {"sent", "researched", "replied", "interested", "declined", "cold"},
    "sent":       {"queued", "replied", "interested", "declined", "cold"},
    "replied":    {"queued", "sent", "interested", "declined", "cold"},
    "interested": {"declined", "cold"},
    "declined":   set(),
    "cold":       {"replied", "interested", "declined"},
}

CLOSED = ("declined", "cold", "interested")
KINDS = ("initial", "nudge", "new_angle", "last_touch", "needs_info")
COLD_KINDS = ("initial", "nudge", "new_angle", "last_touch")
REPLY_LABELS = ("interested", "declined", "needs_info", "out_of_office", "other")
VERDICTS = ("approved", "edited", "skipped", "rejected", "superseded")


class IllegalTransition(RuntimeError):
    pass


def check_transition(old: str, new: str) -> None:
    if old == new:
        return
    if new not in TRANSITIONS.get(old, set()):
        raise IllegalTransition("{} -> {} is not allowed".format(old, new))


# --- rows ------------------------------------------------------------------

@dataclass
class Prospect:
    id: str
    company: str
    contact_name: str = ""
    email: str = ""
    segment: str = ""
    status: str = "new"
    last_touch: str | None = None
    first_touch: str | None = None
    next_touch: str | None = None
    thread_id: str | None = None          # RFC Message-ID of our first email
    gmail_thread_id: str | None = None    # Gmail API thread, when sent that way
    touches: int = 0
    notes: str = ""
    website: str = ""
    source: str = ""                      # csv | discover | telegram | manual

    @classmethod
    def from_row(cls, row) -> "Prospect":
        data = {k: v for k, v in dict(row).items() if k in cls.__dataclass_fields__}
        for key in ("company", "contact_name", "email", "segment", "notes",
                    "website", "source"):
            data[key] = data.get(key) or ""
        data["touches"] = data.get("touches") or 0
        return cls(**data)

    @property
    def first_name(self) -> str:
        return self.contact_name.split()[0] if self.contact_name else ""


@dataclass
class Excerpt:
    url: str
    text: str
    title: str = ""

    def block(self, index: int) -> str:
        return "[S{}] {}\nURL: {}\n{}".format(index, self.title or "(untitled)",
                                              self.url, self.text)


@dataclass
class Research:
    prospect_id: str
    one_liner: str = ""
    student_relevance: str = ""
    prior_campus_sponsorship: str | None = None
    recent_signal: str | None = None      # something they did lately worth mentioning
    hook: str = ""
    company_domain: str = ""
    sources_used: list[str] = field(default_factory=list)
    confidence: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def thin(self) -> bool:
        """Not enough to write a specific pitch from."""
        return self.confidence < 0.45 or not self.hook or len(self.sources_used) == 0

    def to_json(self) -> dict:
        return {
            "one_liner": self.one_liner,
            "student_relevance": self.student_relevance,
            "prior_campus_sponsorship": self.prior_campus_sponsorship,
            "recent_signal": self.recent_signal,
            "hook": self.hook,
            "company_domain": self.company_domain,
            "sources_used": self.sources_used,
            "confidence": self.confidence,
        }

    @classmethod
    def from_json(cls, prospect_id: str, payload: dict) -> "Research":
        return cls(
            prospect_id=prospect_id, raw=dict(payload),
            one_liner=payload.get("one_liner") or "",
            student_relevance=payload.get("student_relevance") or "",
            prior_campus_sponsorship=payload.get("prior_campus_sponsorship"),
            recent_signal=payload.get("recent_signal"),
            hook=payload.get("hook") or "",
            company_domain=payload.get("company_domain") or "",
            sources_used=list(payload.get("sources_used") or []),
            confidence=float(payload.get("confidence") or 0))


@dataclass
class Draft:
    prospect_id: str
    kind: str
    subject: str
    body: str
    claims: list[str] = field(default_factory=list)
    suggested_tier: str = ""
    id: int | None = None
    checks: dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    confidence: float = 0.0
    humanized: bool = False               # went through a humanize rewrite pass
    ai_tell_score: float = 1.0            # 1.0 = no AI tells found by the lint
    supersedes: int | None = None         # draft id this one replaced (redo)

    @property
    def words(self) -> int:
        return len(self.body.split())


@dataclass
class ReplyEvent:
    prospect_id: str
    message_id: str
    from_addr: str
    subject: str
    snippet: str
    label: str
    confidence: float = 0.0
    extracted_ask: str | None = None
    action: str = ""
