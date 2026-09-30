from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from scanalert.calendar import NyseCalendar, easter, nyse_holidays


@pytest.mark.parametrize(
    ("d", "name"),
    [
        (date(2026, 1, 1), "New Year's Day"),
        (date(2026, 1, 19), "Martin Luther King Jr. Day"),
        (date(2026, 2, 16), "Washington's Birthday"),
        (date(2026, 4, 3), "Good Friday"),
        (date(2026, 5, 25), "Memorial Day"),
        (date(2026, 6, 19), "Juneteenth"),
        (date(2026, 7, 3), "Independence Day"),  # July 4 is Saturday -> observed Friday
        (date(2026, 9, 7), "Labor Day"),
        (date(2026, 11, 26), "Thanksgiving Day"),
        (date(2026, 12, 25), "Christmas Day"),
        (date(2027, 6, 18), "Juneteenth"),  # June 19 2027 is Saturday -> Friday
    ],
)
def test_2026_holidays(cal, d, name):
    assert nyse_holidays(d.year)[d] == name
    assert not cal.is_trading_day(d)


def test_easter_known_dates():
    assert easter(2026) == date(2026, 4, 5)
    assert easter(2025) == date(2025, 4, 20)
    assert easter(2024) == date(2024, 3, 31)


def test_new_year_on_saturday_market_open_previous_friday(cal):
    assert date(2028, 1, 1).weekday() == 5
    assert cal.is_trading_day(date(2027, 12, 31))


def test_sunday_holiday_observed_monday(cal):
    assert date(2027, 7, 4).weekday() == 6
    assert not cal.is_trading_day(date(2027, 7, 5))


def test_juneteenth_not_holiday_before_2022(cal):
    assert cal.is_trading_day(date(2021, 6, 18)) and 6 not in {
        d.month for d in nyse_holidays(2021) if d.day == 19
    }


def test_special_closure(cal):
    assert not cal.is_trading_day(date(2025, 1, 9))


def test_weekends_closed(cal):
    assert not cal.is_trading_day(date(2026, 9, 26)) and not cal.is_trading_day(date(2026, 9, 27))


@pytest.mark.parametrize("d", [date(2026, 11, 27), date(2026, 12, 24), date(2025, 7, 3), date(2023, 7, 3)])
def test_early_closes(cal, d):
    assert cal.is_early_close(d)
    assert cal.close_time(d) == time(13, 0)
    assert len(cal.regular_minutes(d)) == 210


def test_no_early_close_when_july3_is_holiday(cal):
    assert not cal.is_trading_day(date(2026, 7, 3))
    assert not cal.is_early_close(date(2026, 7, 2))


def test_full_day_has_390_minutes(cal):
    assert len(cal.regular_minutes(date(2026, 9, 28))) == 390


def test_session_boundaries_edt(cal):
    d = date(2026, 9, 28)

    def et(h: int, m: int) -> datetime:
        return datetime.combine(d, time(h, m), tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)

    assert cal.session_at(et(3, 59)) == "closed"
    assert cal.session_at(et(4, 0)) == "premarket"
    assert cal.session_at(et(9, 29)) == "premarket"
    assert cal.session_at(et(9, 30)) == "regular"
    assert cal.session_at(et(15, 59)) == "regular"
    assert cal.session_at(et(16, 0)) == "postmarket"
    assert cal.session_at(et(19, 59)) == "postmarket"
    assert cal.session_at(et(20, 0)) == "closed"


def test_session_boundaries_early_close(cal):
    d = date(2026, 11, 27)

    def et(h: int, m: int) -> datetime:
        return datetime.combine(d, time(h, m), tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)

    assert cal.session_at(et(12, 59)) == "regular"
    assert cal.session_at(et(13, 0)) == "postmarket"


def test_dst_shift_open_utc(cal):
    assert cal.open_dt(date(2026, 10, 30)).hour == 13  # EDT
    assert cal.open_dt(date(2026, 11, 2)).hour == 14  # EST after Nov 1


def test_extended_hours_flags_default_off(cal):
    assert (
        cal.session_allowed("regular")
        and not cal.session_allowed("premarket")
        and not cal.session_allowed("postmarket")
    )
    ext = NyseCalendar(enable_premarket=True, enable_postmarket=True)
    assert (
        ext.session_allowed("premarket")
        and ext.session_allowed("postmarket")
        and not ext.session_allowed("closed")
    )


def test_next_prev_trading_day(cal):
    assert cal.next_trading_day(date(2026, 9, 4)) == date(2026, 9, 8)  # skips weekend + Labor Day
    assert cal.prev_trading_day(date(2026, 9, 8)) == date(2026, 9, 4)


def test_trading_days_range_excludes_holiday(cal):
    days = cal.trading_days(date(2026, 9, 1), date(2026, 9, 11))
    assert date(2026, 9, 7) not in days and len(days) == 8


def test_extra_holiday_patch():
    c = NyseCalendar(extra_holidays=frozenset({date(2026, 9, 29)}))
    assert not c.is_trading_day(date(2026, 9, 29))
