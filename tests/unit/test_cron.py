from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from likearr.core.cron import CronError, next_fire, parse_cron

NY = ZoneInfo("America/New_York")
UTC_ZONE = ZoneInfo("UTC")


def test_every_six_hours_at_twenty_past_fires_next_at_the_following_slot() -> None:
    # 13:05 in New York is after the 12:20 fire, so the next one is 18:20 the same day.
    after = datetime(2026, 9, 23, 13, 5, tzinfo=NY)

    assert next_fire("20 */6 * * *", after, NY) == datetime(2026, 9, 23, 18, 20, tzinfo=NY)


def test_the_next_fire_is_strictly_after_the_reference_time() -> None:
    after = datetime(2026, 9, 23, 18, 20, tzinfo=NY)

    assert next_fire("20 */6 * * *", after, NY) == datetime(2026, 9, 24, 0, 20, tzinfo=NY)


def test_the_answer_is_in_the_cron_timezone_whatever_the_reference_is_in() -> None:
    # 17:05 UTC is 13:05 in New York (EDT, UTC-4).
    after = datetime(2026, 9, 23, 17, 5, tzinfo=UTC)

    fire = next_fire("20 */6 * * *", after, NY)

    assert fire is not None
    assert fire == datetime(2026, 9, 23, 18, 20, tzinfo=NY)
    assert fire.astimezone(UTC) == datetime(2026, 9, 23, 22, 20, tzinfo=UTC)


def test_lists_ranges_and_steps() -> None:
    expr = parse_cron("0,30 9-17/4 * * *")

    assert expr.minutes == (0, 30)
    assert expr.hours == (9, 13, 17)


def test_names_for_months_and_weekdays() -> None:
    expr = parse_cron("0 12 * jan,Jul mon-fri")

    assert expr.months == (1, 7)
    assert expr.weekdays == (1, 2, 3, 4, 5)


def test_sunday_may_be_written_as_seven() -> None:
    assert parse_cron("0 0 * * 7").weekdays == (0,)


def test_day_of_month_and_weekday_are_or_ed_when_both_are_restricted() -> None:
    # Standard cron: "the 1st, or any Monday". 2026-09-23 is a Wednesday; the next Monday is the 28th,
    # which comes before October 1st.
    after = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    assert next_fire("0 6 1 * mon", after, UTC_ZONE) == datetime(2026, 9, 28, 6, 0, tzinfo=UTC)


def test_a_restricted_weekday_alone_is_not_or_ed_with_the_star_day_of_month() -> None:
    after = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)  # Wednesday

    assert next_fire("0 6 * * fri", after, UTC_ZONE) == datetime(2026, 9, 25, 6, 0, tzinfo=UTC)


def test_a_date_that_never_comes_has_no_next_fire() -> None:
    assert next_fire("0 0 31 2 *", datetime(2026, 1, 1, tzinfo=UTC), UTC_ZONE) is None


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "5-1 * * * *",
        "a * * * *",
        "@daily",
    ],
)
def test_malformed_expressions_are_refused_with_a_reason(expr: str) -> None:
    with pytest.raises(CronError):
        parse_cron(expr)


def test_a_day_field_starting_with_a_star_counts_as_unrestricted_as_in_vixie_cron() -> None:
    # cronie/Vixie set DOM_STAR for any field whose first character is "*", so "*/1" plus a weekday
    # is AND-ed: Mondays only. 2026-09-23 is a Wednesday; the next Monday is the 28th.
    after = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

    assert next_fire("0 6 */1 * mon", after, UTC_ZONE) == datetime(2026, 9, 28, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize("expr", ["\u00b2 * * * *", "\u0663\u0660 */6 * * *", "*/\u00b2 * * * *", "\uff15 * * * *"])
def test_only_ascii_digits_are_numbers(expr: str) -> None:
    with pytest.raises(CronError):
        parse_cron(expr)


def test_the_repeated_autumn_hour_never_yields_a_fire_in_the_past() -> None:
    # 2026-11-01 06:10 UTC is 01:10 EST, the second pass through 01:xx in New York. 01:20 EDT
    # (05:20 UTC) has already happened, so the next "20 1 * * *" fire is tomorrow's.
    after = datetime(2026, 11, 1, 6, 10, tzinfo=UTC)

    fire = next_fire("20 1 * * *", after, NY)

    assert fire is not None
    assert fire.astimezone(UTC) > after


def test_the_default_schedule_survives_the_autumn_repeated_hour() -> None:
    # 2026-11-01 is the fall-back Sunday in America/New_York; "20 */6 * * *" fires at 00:20, 06:20,
    # 12:20 and 18:20 local. None of those lands in the repeated 01:xx hour, but the answer must
    # still be strictly after `after` and a real, resolvable local time.
    after = datetime(2026, 11, 1, 5, 0, tzinfo=NY)

    fire = next_fire("20 */6 * * *", after, NY)

    assert fire is not None
    assert fire > after
    assert fire.astimezone(UTC) > after.astimezone(UTC)


def test_the_spring_forward_gap_never_yields_a_time_that_does_not_exist() -> None:
    # 2027-03-14 is the spring-forward Sunday in America/New_York: 02:00-02:59 does not exist
    # (clocks jump from 01:59 EST to 03:00 EDT). A fire nominally inside the gap must still come
    # back as *some* real, strictly-later moment - not silently before `after`.
    after = datetime(2027, 3, 14, 1, 0, tzinfo=NY)

    fire = next_fire("30 2 * * *", after, NY)

    assert fire is not None
    assert fire.astimezone(UTC) > after.astimezone(UTC)


def test_the_default_schedule_survives_the_spring_forward_gap() -> None:
    after = datetime(2027, 3, 14, 1, 0, tzinfo=NY)

    fire = next_fire("20 */6 * * *", after, NY)

    assert fire is not None
    assert fire.astimezone(UTC) > after.astimezone(UTC)
