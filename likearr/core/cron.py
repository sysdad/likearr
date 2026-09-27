"""When a five-field cron line next fires. Pure: the caller supplies the clock and the timezone.

The line and its timezone live in `config.toml`'s `[schedule]` block (`cron` / `timezone`), read
by the in-service scheduler (`likearr.web.schedule`, issue #68 phase 2) to decide when to fire, and
by the web UI to show the next fire on Status.

The dialect is the one every Linux crontab speaks: minute, hour, day of month, month, day of
week; ``*``, numbers, ``a-b`` ranges, ``/n`` steps and comma lists; three-letter month and
weekday names; ``0`` or ``7`` for Sunday. When both day fields are restricted - neither starts
with ``*`` - a day matches either of them, which is cron's own long-standing rule. The
``@daily`` family is not supported, and is refused rather than misread.

The timezone matters: cron fires in the host's local time, and a zone such as
``America/New_York`` is easy to get wrong by an hour twice a year. A fire that falls in a
spring-forward gap is reported at the wall-clock time it names, and one in the repeated autumn
hour at its first pass, which is close enough for a page that says "next fire".
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

__all__ = ["CronError", "CronExpr", "min_interval_minutes", "next_fire", "next_fire_from", "parse_cron"]

_MONTHS = {
    name: i
    for i, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}
_WEEKDAYS = {name: i for i, name in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}

_SEARCH_DAYS = 366 * 5
"""How far ahead to look before deciding a line never fires (``0 0 30 2 *``). Five years covers
every leap-day line; a real schedule is found within the first day or two."""


class CronError(ValueError):
    """A cron line this module cannot read. The message says which field and why."""


@dataclass(frozen=True, slots=True)
class CronExpr:
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: tuple[int, ...]
    months: tuple[int, ...]
    weekdays: tuple[int, ...]
    """0 is Sunday, as in cron."""
    days_restricted: bool
    weekdays_restricted: bool

    def matches_day(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return dom or dow
        return dom and dow


def _value(token: str, lo: int, hi: int, names: dict[str, int], field: str) -> int:
    lowered = token.lower()
    if lowered in names:
        return names[lowered]
    if not (token.isascii() and token.isdigit()):  # isdigit() alone also takes "²" and "٣"
        raise CronError(f"cron {field} field: {token!r} is not a number")
    value = int(token)
    if not lo <= value <= hi:
        raise CronError(f"cron {field} field: {value} is outside {lo}-{hi}")
    return value


def _field(text: str, lo: int, hi: int, field: str, names: dict[str, int] | None = None) -> tuple[int, ...]:
    names = names or {}
    out: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"cron {field} field: empty list item in {text!r}")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not (step_text.isascii() and step_text.isdigit()) or int(step_text) == 0:
                raise CronError(f"cron {field} field: step {step_text!r} must be a positive number")
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            start, end = _value(a, lo, hi, names, field), _value(b, lo, hi, names, field)
            if start > end:
                raise CronError(f"cron {field} field: range {base!r} runs backwards")
        else:
            start = _value(base, lo, hi, names, field)
            end = hi if step_text else start
        out.update(range(start, end + 1, step))
    return tuple(sorted(out))


def parse_cron(text: str) -> CronExpr:
    """Parse a five-field cron line.

    Raises:
        CronError: the line is not five fields, or a field is malformed or out of range.
    """
    fields = text.split()
    if len(fields) != 5:
        raise CronError(f"a cron line has five fields (minute hour day month weekday), not {len(fields)}: {text!r}")
    minute, hour, day, month, weekday = fields
    weekdays = _field(weekday, 0, 7, "weekday", _WEEKDAYS)
    return CronExpr(
        minutes=_field(minute, 0, 59, "minute"),
        hours=_field(hour, 0, 23, "hour"),
        days=_field(day, 1, 31, "day-of-month"),
        months=_field(month, 1, 12, "month", _MONTHS),
        weekdays=tuple(sorted({d % 7 for d in weekdays})),
        # cronie/Vixie cron flag a day field as "star" when its first character is "*", so "*/2"
        # is unrestricted too, and the two day fields are then AND-ed rather than OR-ed.
        days_restricted=not day.startswith("*"),
        weekdays_restricted=not weekday.startswith("*"),
    )


def next_fire(text: str, after: datetime, tz: ZoneInfo) -> datetime | None:
    """The first time strictly after `after` that the line fires, in `tz`; ``None`` if it never does.

    Parses `text` once and delegates to `next_fire_from`. A caller that asks this repeatedly for
    the same line (`likearr.web.settings`'s schedule preview and fire-rate comparison) should parse
    once and call `next_fire_from` itself instead - `parse_cron` is not free, and re-running it on
    every step of a loop is wasted work `min_interval_minutes` already bounds to at most a few
    hundred iterations, but there is no reason to pay for it twice.
    """
    return next_fire_from(parse_cron(text), after, tz)


def next_fire_from(expr: CronExpr, after: datetime, tz: ZoneInfo) -> datetime | None:
    """`next_fire`, given an already-parsed `CronExpr`.

    Walks forward a day at a time and only then through that day's hours and minutes, so a line
    that fires a few times a day costs a handful of comparisons rather than a minute-by-minute
    scan.
    """
    after_utc = after.astimezone(UTC)
    day = after.astimezone(tz).date()
    for _ in range(_SEARCH_DAYS):
        if expr.matches_day(day):
            for hour in expr.hours:
                for minute in expr.minutes:
                    candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                    # Compared in UTC: two datetimes sharing a tzinfo compare by wall clock and
                    # ignore `fold`, which during the repeated autumn hour picks a time already gone.
                    if candidate.astimezone(UTC) > after_utc:
                        return candidate
        day += timedelta(days=1)
    return None


def min_interval_minutes(text: str) -> float:
    """The shortest possible gap, in minutes, between two consecutive fires of `text` - a lower
    bound, not the true minimum for a line restricted to particular days.

    Computed from the hour:minute pairs alone, as if every day fired: the gap between consecutive
    times within a day, and the wraparound from the last time of one day to the first of the next.
    A day-of-month or weekday restriction only ever *removes* candidate days, never adds a closer
    pair of times, so this every-day figure is always a safe lower bound on the real one - the
    schedule's minimum-interval guard (`config.MIN_SCHEDULE_INTERVAL_MINUTES`) can refuse a cron
    line on it without ever refusing one that is actually safer than it looks.

    At most one fire a day (or none) has no meaningful interval: returns ``inf``.
    """
    expr = parse_cron(text)
    times = sorted(h * 60 + m for h in expr.hours for m in expr.minutes)
    if len(times) <= 1:
        return float("inf")
    gaps = [b - a for a, b in itertools.pairwise(times)]
    gaps.append(times[0] + 24 * 60 - times[-1])  # wraparound: last fire of a day to the first of the next
    return float(min(gaps))
