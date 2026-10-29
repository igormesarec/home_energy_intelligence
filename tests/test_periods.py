from datetime import UTC, date, datetime

from custom_components.home_energy_intelligence.periods import (
    period_bounds, previous_day_same_local_cutoff,
)


def test_calendar_month_is_not_rolling_thirty_days():
    today = date(2026, 10, 28)
    assert period_bounds("this_month", today)[:2] == (date(2026, 10, 1), today)
    assert period_bounds("previous_month", today)[:2] == (
        date(2026, 9, 1), date(2026, 9, 30),
    )


def test_weeks_are_monday_through_sunday():
    start, end, _ = period_bounds("previous_week", date(2026, 10, 28))
    assert start == date(2026, 10, 19) and start.weekday() == 0
    assert end == date(2026, 10, 25) and end.weekday() == 6


def test_all_day_offsets_are_exact_local_dates():
    today = date(2026, 10, 28)
    for offset in range(2, 8):
        start, end, _ = period_bounds(f"day_{offset}", today)
        assert start == end == date(2026, 10, 28 - offset)


def test_current_and_previous_july_to_june_energy_seasons():
    today = date(2026, 10, 28)
    assert period_bounds("current_season", today)[:2] == (
        date(2026, 7, 1), today,
    )
    assert period_bounds("previous_season", today)[:2] == (
        date(2025, 7, 1), date(2026, 6, 30),
    )


def test_custom_season_rolls_over_before_configured_start():
    assert period_bounds(
        "current_season", date(2026, 2, 1), season_month=4, season_day=15,
    )[:2] == (date(2025, 4, 15), date(2026, 2, 1))


def test_previous_calendar_year_is_complete():
    assert period_bounds("previous_year", date(2026, 10, 28))[:2] == (
        date(2025, 1, 1), date(2025, 12, 31),
    )


def test_same_local_cutoff_does_not_use_browser_or_dst_elapsed_hours():
    start, end = previous_day_same_local_cutoff(
        datetime(2026, 10, 25, 2, tzinfo=UTC), "Europe/Ljubljana"
    )
    assert (end - start).total_seconds() == 3 * 3600
    start, end = previous_day_same_local_cutoff(
        datetime(2026, 3, 29, 2, tzinfo=UTC), "Europe/Ljubljana"
    )
    assert (end - start).total_seconds() == 4 * 3600
