from datetime import date

from custom_components.home_energy_intelligence.timeutils import cycle_boundaries, elapsed_seconds, local_day_bounds


def test_ljubljana_spring_day_has_23_elapsed_hours():
    start, end = local_day_bounds(date(2026, 3, 29), "Europe/Ljubljana")
    assert elapsed_seconds(start, end) == 23 * 3600


def test_ljubljana_autumn_day_has_25_elapsed_hours_and_seven_hour_cycle():
    start, end = local_day_bounds(date(2026, 10, 25), "Europe/Ljubljana")
    assert elapsed_seconds(start, end) == 25 * 3600
    boundaries = cycle_boundaries(date(2026, 10, 25), "Europe/Ljubljana")
    durations = [elapsed_seconds(a, b) for a, b in zip(boundaries, boundaries[1:])]
    assert durations == [7 * 3600, 6 * 3600, 6 * 3600, 6 * 3600]


def test_normal_day_has_24_hours():
    start, end = local_day_bounds(date(2026, 10, 24), "Europe/Ljubljana")
    assert elapsed_seconds(start, end) == 24 * 3600
