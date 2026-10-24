from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.timeutil import (
    TZ,
    WINDOW_DAYS,
    ensure_utc,
    history_bounds_utc,
    local_day_start_utc,
    to_local_date,
    window_bounds_utc,
    window_start_date,
)

SAST_OFFSET = timedelta(hours=2)
MIN_INSTANT = datetime(1990, 1, 1, tzinfo=UTC)
MAX_INSTANT = datetime(2100, 12, 31, 23, 59, 59, tzinfo=UTC)

supported_instants = st.datetimes(
    min_value=MIN_INSTANT.replace(tzinfo=None),
    max_value=MAX_INSTANT.replace(tzinfo=None),
    timezones=st.just(UTC),
)
supported_dates = st.dates(min_value=date(1990, 1, 1), max_value=date(2100, 12, 31))


def utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)


class TestToLocalDate:
    @pytest.mark.parametrize(
        ("instant", "expected"),
        [
            pytest.param(
                utc(2026, 2, 28, 21, 59, 59), date(2026, 2, 28), id="1s before SAST midnight"
            ),
            pytest.param(utc(2026, 2, 28, 21, 59, 59, 999999), date(2026, 2, 28), id="1us before"),
            pytest.param(utc(2026, 2, 28, 22, 0, 0), date(2026, 3, 1), id="exactly SAST midnight"),
            pytest.param(utc(2026, 2, 28, 22, 30, 0), date(2026, 3, 1), id="00:30 SAST"),
            pytest.param(utc(2026, 3, 1, 0, 0, 0), date(2026, 3, 1), id="UTC midnight"),
            pytest.param(utc(2026, 3, 1, 21, 59, 59), date(2026, 3, 1), id="last second of day"),
            pytest.param(utc(2026, 3, 1, 22, 0, 0), date(2026, 3, 2), id="next SAST midnight"),
            pytest.param(utc(2025, 12, 31, 22, 0, 0), date(2026, 1, 1), id="new year"),
            pytest.param(utc(2025, 12, 31, 21, 59, 59), date(2025, 12, 31), id="before new year"),
            pytest.param(utc(2024, 2, 28, 22, 0, 0), date(2024, 2, 29), id="leap day"),
            pytest.param(utc(2024, 2, 29, 22, 0, 0), date(2024, 3, 1), id="after leap day"),
        ],
    )
    def test_utc_instant_maps_to_sast_calendar_date(
        self, instant: datetime, expected: date
    ) -> None:
        assert to_local_date(instant) == expected

    @pytest.mark.parametrize(
        "instant",
        [
            pytest.param(datetime(2026, 3, 1, 0, 30, tzinfo=timezone(SAST_OFFSET)), id="+02:00"),
            pytest.param(
                datetime(2026, 3, 1, 1, 0, tzinfo=timezone(timedelta(hours=3))), id="+03:00"
            ),
            pytest.param(
                datetime(2026, 2, 28, 18, 0, tzinfo=timezone(timedelta(hours=-4))), id="-04:00"
            ),
            pytest.param(
                datetime(2026, 2, 28, 17, 0, tzinfo=ZoneInfo("America/New_York")), id="New York"
            ),
        ],
    )
    def test_the_zone_the_instant_is_expressed_in_is_irrelevant(self, instant: datetime) -> None:
        # Each is between 22:00Z and 22:59Z on 28 Feb, i.e. already 1 March in Johannesburg.
        assert to_local_date(instant) == date(2026, 3, 1)

    def test_naive_datetime_is_rejected_instead_of_assuming_the_machine_zone(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            to_local_date(datetime(2026, 3, 1, 12, 0))

    @settings(deadline=None)
    @given(supported_instants)
    def test_matches_fixed_two_hour_offset(self, instant: datetime) -> None:
        # Johannesburg has had no daylight saving since 1944, so UTC+2 arithmetic is an
        # independent oracle for the zoneinfo based conversion.
        assert to_local_date(instant) == (instant + SAST_OFFSET).date()


class TestEnsureUtc:
    def test_converts_an_aware_instant_to_utc_without_changing_the_moment(self) -> None:
        local = datetime(2026, 3, 15, 12, 0, tzinfo=timezone(SAST_OFFSET))

        converted = ensure_utc(local)

        assert converted == local
        assert converted == utc(2026, 3, 15, 10, 0)
        assert converted.utcoffset() == timedelta(0)

    def test_utc_instant_is_returned_unchanged(self) -> None:
        instant = utc(2026, 3, 15, 10, 0, 0, 123456)

        assert ensure_utc(instant) == instant

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            ensure_utc(datetime(2026, 3, 15, 10, 0))


class TestLocalDayStartUtc:
    def test_sast_midnight_is_two_hours_before_utc_midnight(self) -> None:
        start = local_day_start_utc(date(2026, 3, 1))

        assert start == utc(2026, 2, 28, 22, 0)
        assert start.utcoffset() == timedelta(0)

    def test_the_instant_before_it_belongs_to_the_previous_business_day(self) -> None:
        start = local_day_start_utc(date(2026, 3, 1))

        assert to_local_date(start) == date(2026, 3, 1)
        assert to_local_date(start - timedelta(microseconds=1)) == date(2026, 2, 28)

    @settings(deadline=None)
    @given(supported_dates)
    def test_round_trips_through_to_local_date(self, day: date) -> None:
        start = local_day_start_utc(day)

        assert to_local_date(start) == day
        assert to_local_date(start - timedelta(microseconds=1)) == day - timedelta(days=1)
        assert start == datetime(day.year, day.month, day.day, tzinfo=UTC) - SAST_OFFSET

    @settings(deadline=None)
    @given(supported_instants)
    def test_an_instant_lies_inside_the_utc_span_of_its_business_day(
        self, instant: datetime
    ) -> None:
        day = to_local_date(instant)

        assert local_day_start_utc(day) <= instant < local_day_start_utc(day + timedelta(days=1))


class TestWindow:
    def test_window_is_thirty_inclusive_business_days(self) -> None:
        assert WINDOW_DAYS == 30
        assert window_start_date(date(2026, 1, 30)) == date(2026, 1, 1)
        assert window_start_date(date(2026, 1, 31)) == date(2026, 1, 2)

    def test_window_start_respects_month_length_and_leap_years(self) -> None:
        assert window_start_date(date(2026, 3, 1)) == date(2026, 1, 31)
        assert window_start_date(date(2024, 3, 1)) == date(2024, 2, 1)

    def test_window_bounds_are_a_half_open_utc_interval_of_whole_sast_days(self) -> None:
        start, end = window_bounds_utc(date(2026, 1, 30))

        assert start == utc(2025, 12, 31, 22, 0)  # 1 Jan 00:00 SAST
        assert end == utc(2026, 1, 30, 22, 0)  # 31 Jan 00:00 SAST

    @pytest.mark.parametrize(
        ("instant", "inside"),
        [
            pytest.param(utc(2025, 12, 31, 21, 59, 59), False, id="last second of 31 Dec SAST"),
            pytest.param(utc(2025, 12, 31, 22, 0, 0), True, id="first instant of 1 Jan SAST"),
            pytest.param(utc(2026, 1, 30, 21, 59, 59), True, id="last second of 30 Jan SAST"),
            pytest.param(utc(2026, 1, 30, 22, 0, 0), False, id="first instant of 31 Jan SAST"),
        ],
    )
    def test_window_bounds_edges_are_start_inclusive_end_exclusive(
        self, instant: datetime, inside: bool
    ) -> None:
        start, end = window_bounds_utc(date(2026, 1, 30))

        assert (start <= instant < end) is inside

    @settings(deadline=None)
    @given(supported_dates)
    def test_window_spans_exactly_thirty_business_days(self, day: date) -> None:
        start, end = window_bounds_utc(day)

        assert end - start == timedelta(days=30)
        assert to_local_date(start) == day - timedelta(days=29)
        assert to_local_date(end - timedelta(microseconds=1)) == day


class TestHistoryBounds:
    def test_reach_29_days_back_and_29_days_forward(self) -> None:
        start, end = history_bounds_utc(date(2026, 3, 10))

        assert start == utc(2026, 2, 8, 22, 0)  # 9 Feb 00:00 SAST
        assert end == utc(2026, 4, 8, 22, 0)  # 9 Apr 00:00 SAST: 8 Apr is the last day included

    @settings(deadline=None)
    @given(supported_dates)
    def test_are_the_union_of_the_windows_of_the_day_and_the_next_29_days(self, day: date) -> None:
        first_start, _ = window_bounds_utc(day)
        _, last_end = window_bounds_utc(day + timedelta(days=WINDOW_DAYS - 1))

        assert history_bounds_utc(day) == (first_start, last_end)


class TestSupportedRange:
    def test_extremes_of_the_accepted_input_range_do_not_overflow(self) -> None:
        assert to_local_date(MIN_INSTANT) == date(1990, 1, 1)
        assert to_local_date(MAX_INSTANT) == date(2101, 1, 1)
        for instant in (MIN_INSTANT, MAX_INSTANT):
            day = to_local_date(instant)
            window_bounds_utc(day)
            history_bounds_utc(day)

    def test_business_zone_is_johannesburg(self) -> None:
        assert TZ.key == "Africa/Johannesburg"
