"""When an approved email may leave: the send-rate policy, as pure functions.

Google's sender guidelines say it plainly: send at a consistent rate, avoid
bursts, start low and ramp up gradually. Tapping [Send] on fifteen cards in a
row is exactly the burst they mean, so an approval no longer means "now"; it
means "as soon as the policy allows", and that is usually now.

  daily cap      at most SEND_DAILY_CAP per local calendar day
  warm-up        a new sender starts at SEND_WARMUP_START a day and adds
                 SEND_WARMUP_STEP per day since its first real send
  hourly cap     at most SEND_HOURLY_CAP in any rolling hour
  spacing        at least SEND_MIN_GAP seconds between two sends, plus up to
                 SEND_JITTER more so queued mail never leaves on a fixed beat
  window         only on SEND_DAYS between SEND_HOURS, in SEND_TZ

Everything here is a pure function of (policy, past send times, now), so the
whole schedule is testable without a clock, a database or a mail server.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, tzinfo

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAYS = frozenset(range(5))
EVERY_DAY = frozenset(range(7))

# Far below Gmail's own limits (500 a day for a free account, 2,000 for
# Workspace). The point is not to stay under the hard limit, it is to look
# like a person writing to people.
DEFAULT_DAILY_CAP = 20
DEFAULT_HOURLY_CAP = 8
DEFAULT_MIN_GAP = 90
DEFAULT_JITTER = 60
DEFAULT_WARMUP_START = 8
DEFAULT_WARMUP_STEP = 4


def parse_tz(value: str) -> tzinfo:
    """'Asia/Kolkata', '+05:30', 'UTC+5:30', or '' for this machine's zone.

    Raises ValueError on a name zoneinfo does not know (Windows without the
    tzdata package knows none), so config can say so instead of quietly
    sending at 3 a.m.
    """
    value = (value or "").strip()
    if not value:
        return datetime.now().astimezone().tzinfo or timezone.utc
    if value.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    match = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", value, re.I)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))
        return timezone(sign * delta, value)
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        return ZoneInfo(value)
    except Exception as exc:                       # noqa: BLE001
        raise ValueError("unknown time zone {!r} (use a name like Asia/Kolkata, "
                         "or an offset like +05:30)".format(value)) from exc


def parse_hours(value: str) -> tuple[int, int]:
    """'09:00-18:00' -> (540, 1080) minutes since midnight. '' or 'any' is
    the whole day."""
    value = (value or "").strip().lower()
    if value in ("", "any", "all", "*"):
        return (0, 24 * 60)
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?", value)
    if not match:
        raise ValueError("SEND_HOURS should look like 09:00-18:00, not {!r}"
                         .format(value))
    start = int(match.group(1)) * 60 + int(match.group(2) or 0)
    end = int(match.group(3)) * 60 + int(match.group(4) or 0)
    if not (0 <= start < end <= 24 * 60):
        raise ValueError("SEND_HOURS {!r} is not a range within one day".format(value))
    return (start, end)


def parse_days(value: str) -> frozenset[int]:
    """'mon-fri', 'mon,wed,fri', 'all' -> weekday numbers (Monday is 0)."""
    value = (value or "").strip().lower()
    if value in ("", "all", "any", "*", "mon-sun"):
        return EVERY_DAY
    days: set[int] = set()
    for part in value.replace(" ", "").split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        if lo[:3] not in DAY_NAMES or (hi and hi[:3] not in DAY_NAMES):
            raise ValueError("SEND_DAYS should look like mon-fri, not {!r}".format(value))
        a = DAY_NAMES.index(lo[:3])
        b = DAY_NAMES.index(hi[:3]) if hi else a
        day = a
        while True:
            days.add(day)
            if day == b:
                break
            day = (day + 1) % 7
    return frozenset(days)


@dataclass
class Policy:
    daily_cap: int = DEFAULT_DAILY_CAP
    hourly_cap: int = DEFAULT_HOURLY_CAP
    min_gap_s: int = DEFAULT_MIN_GAP
    jitter_s: int = DEFAULT_JITTER
    window: tuple[int, int] = (9 * 60, 18 * 60)
    days: frozenset = field(default_factory=lambda: WEEKDAYS)
    tz: tzinfo = timezone.utc
    warmup: bool = True
    warmup_start: int = DEFAULT_WARMUP_START
    warmup_step: int = DEFAULT_WARMUP_STEP

    @classmethod
    def from_cfg(cls, cfg) -> "Policy":
        """Config -> Policy. A bad SEND_TZ/HOURS/DAYS value falls back to the
        default rather than stopping mail; `doctor` reports the bad value."""
        def safe(fn, value, default):
            try:
                return fn(value)
            except ValueError:
                return default
        return cls(
            daily_cap=max(1, int(getattr(cfg, "send_daily_cap", DEFAULT_DAILY_CAP))),
            hourly_cap=max(1, int(getattr(cfg, "send_hourly_cap", DEFAULT_HOURLY_CAP))),
            min_gap_s=max(0, int(getattr(cfg, "send_min_gap", DEFAULT_MIN_GAP))),
            jitter_s=max(0, int(getattr(cfg, "send_jitter", DEFAULT_JITTER))),
            window=safe(parse_hours, getattr(cfg, "send_hours", ""), (9 * 60, 18 * 60)),
            days=safe(parse_days, getattr(cfg, "send_days", ""), WEEKDAYS),
            tz=safe(parse_tz, getattr(cfg, "send_tz", ""), parse_tz("")),
            warmup=bool(getattr(cfg, "send_warmup", True)),
            warmup_start=max(1, int(getattr(cfg, "send_warmup_start",
                                            DEFAULT_WARMUP_START))),
            warmup_step=max(0, int(getattr(cfg, "send_warmup_step",
                                           DEFAULT_WARMUP_STEP))))

    # -- the pieces ----------------------------------------------------------
    def local(self, when: datetime) -> datetime:
        return when.astimezone(self.tz)

    def cap_on(self, day, first_send: datetime | None) -> int:
        """Today's ceiling: the warm-up ramp, never above the daily cap."""
        if not self.warmup:
            return self.daily_cap
        if first_send is None:
            age = 0
        else:
            age = max(0, (day - self.local(first_send).date()).days)
        return min(self.daily_cap, self.warmup_start + self.warmup_step * age)

    def in_window(self, when: datetime) -> bool:
        local = self.local(when)
        minute = local.hour * 60 + local.minute
        return local.weekday() in self.days and self.window[0] <= minute < self.window[1]

    def window_open_after(self, when: datetime) -> datetime:
        """The first moment at or after `when` that is inside the window."""
        if self.in_window(when):
            return when
        local = self.local(when)
        for offset in range(0, 8):
            day = local.date() + timedelta(days=offset)
            if day.weekday() not in self.days:
                continue
            opening = self.opening_on(day)
            if opening > local:
                return opening.astimezone(timezone.utc)
        return when + timedelta(days=1)            # pragma: no cover -- no days at all

    def opening_on(self, day) -> datetime:
        """The window's first minute on `day`, in the policy's zone."""
        start = time(self.window[0] // 60, self.window[0] % 60)
        return datetime.combine(day, start, tzinfo=self.tz)

    def describe(self) -> str:
        hours = "any time" if self.window == (0, 24 * 60) else "{:02d}:{:02d}-{:02d}:{:02d}".format(
            self.window[0] // 60, self.window[0] % 60,
            self.window[1] // 60, self.window[1] % 60)
        days = ("every day" if self.days == EVERY_DAY else "Mon-Fri"
                if self.days == WEEKDAYS else ",".join(
                    DAY_NAMES[d].title() for d in sorted(self.days)))
        ramp = (", warm-up from {}/day +{} a day".format(self.warmup_start,
                                                        self.warmup_step)
                if self.warmup else "")
        return ("max {}/day{}, {}/hour, {}s+ apart, {} {} ({})".format(
            self.daily_cap, ramp, self.hourly_cap, self.min_gap_s, days, hours,
            getattr(self.tz, "key", None) or str(self.tz)))


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def next_slot(policy: Policy, sent: list[datetime], now: datetime,
              first_send: datetime | None = None,
              rng: random.Random | None = None) -> tuple[datetime, str]:
    """(when the next email may leave, why it has to wait -- "" if now).

    `sent` is every real send that counts against the limits, including the
    ones already promised to emails waiting ahead in the queue. The checks
    push `when` forward until none of them objects; each push only moves
    later, so this always terminates.
    """
    rng = rng or random.Random()
    now = _aware(now)
    times = sorted(_aware(t) for t in sent)
    if first_send is None and times:
        first_send = times[0]
    when, reason = now, ""
    for _ in range(64):
        moved = False
        opening = policy.window_open_after(when)
        if opening > when:
            when, moved = opening, True
            reason = reason or "outside sending hours"

        day = policy.local(when).date()
        today = [t for t in times if policy.local(t).date() == day]
        cap = policy.cap_on(day, first_send)
        if len(today) >= cap:
            tomorrow = policy.opening_on(day + timedelta(days=1))
            when, moved = tomorrow.astimezone(timezone.utc), True
            reason = reason or ("daily limit reached ({} today{})".format(
                cap, ", warming up" if cap < policy.daily_cap else ""))
            continue

        hour = [t for t in times if when - timedelta(hours=1) < t <= when]
        if len(hour) >= policy.hourly_cap:
            when, moved = hour[0] + timedelta(hours=1), True
            reason = reason or "hourly limit reached ({}/hour)".format(policy.hourly_cap)
            continue

        # Both directions: a send already promised for 10:05 rules out 10:04
        # just as surely as one that left at 10:03 does.
        gap = timedelta(seconds=policy.min_gap_s)
        close = [t for t in times if abs(t - when) < gap]
        if close:
            when = max(close) + gap + timedelta(seconds=rng.uniform(0, policy.jitter_s))
            moved = True
            reason = reason or "spacing sends {}s+ apart".format(policy.min_gap_s)
            continue
        if not moved:
            break
    return when, ("" if when <= now else reason)


def sends_today(policy: Policy, sent: list[datetime], now: datetime) -> int:
    day = policy.local(_aware(now)).date()
    return sum(1 for t in sent if policy.local(_aware(t)).date() == day)
