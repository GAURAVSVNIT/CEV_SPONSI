"""The follow-up ladder. Timing lives here, in code; the model only writes copy.

  touch 1  day  0  initial     the researched hook
  touch 2  day  3  nudge       three lines, one new detail
  touch 3  day  7  new_angle   different tier or different value prop
  touch 4  day 14  last_touch  short, no pressure
  --       day 21  cold        auto-mark, stop

Hard cap: 4 touches, ever. Enforced here, in the status machine, and by a
trigger on the drafts table. Three layers because the agent's judgment is not
one of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

MAX_TOUCHES = 4
COLD_AFTER_DAYS = 21
SKIP_PUSH_DAYS = 3          # a skipped draft comes back in 3 days, not tomorrow
NO_SOURCES_PUSH_DAYS = 1    # research found nothing: try again tomorrow


@dataclass(frozen=True)
class Step:
    touch: int
    day: int
    kind: str
    shift: str


LADDER: tuple[Step, ...] = (
    Step(1, 0, "initial",
         "Lead with the researched hook. This is the first contact."),
    Step(2, 3, "nudge",
         "Three or four lines maximum. Add exactly one new detail they did not "
         "have in the first email (registration count, another confirmed "
         "sponsor). Do not restate the first email. Sound like someone who "
         "genuinely forgot they hadn't heard back, not like a sequence."),
    Step(3, 7, "new_angle",
         "Change the frame: if the first two sold brand visibility, sell the "
         "recruiting funnel (or the reverse). You may suggest a different, "
         "usually smaller, tier."),
    Step(4, 14, "last_touch",
         "Short and final. No pressure, no guilt, no fake deadline. Leave the "
         "door open and say plainly that you will not write again."),
)

BY_TOUCH = {s.touch: s for s in LADDER}
BY_KIND = {s.kind: s for s in LADDER}


def next_step(touches: int) -> Step | None:
    """The step to draft next for a prospect that has been touched `touches` times."""
    return BY_TOUCH.get(touches + 1)


def _as_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def schedule_after_send(first_touch: str | date | None, touches_after_send: int,
                        today: date) -> tuple[str | None, str | None]:
    """(next_touch_date, next_kind) after a send. next_kind None means 'cold sweep'."""
    anchor = _as_date(first_touch) or today
    step = BY_TOUCH.get(touches_after_send + 1)
    if step is None or touches_after_send >= MAX_TOUCHES:
        return (anchor + timedelta(days=COLD_AFTER_DAYS)).isoformat(), None
    due = anchor + timedelta(days=step.day)
    if due <= today:                      # ladder anchored in the past
        due = today + timedelta(days=1)
    return due.isoformat(), step.kind


def cold_on(first_touch: str | date | None) -> str | None:
    anchor = _as_date(first_touch)
    if anchor is None:
        return None
    return (anchor + timedelta(days=COLD_AFTER_DAYS)).isoformat()


def is_cold(first_touch: str | date | None, today: date) -> bool:
    anchor = _as_date(first_touch)
    if anchor is None:
        return False
    return (today - anchor).days >= COLD_AFTER_DAYS


def push(days: int, today: date) -> str:
    return (today + timedelta(days=days)).isoformat()
