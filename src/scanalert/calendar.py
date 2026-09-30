"""US equity exchange calendar (NYSE rules) and session classification.

Weekday-only logic is *not* used: holidays, observed holidays, early closes and one-off closures are
modelled. ``ExchangeCalendar`` is a Protocol so a vendor/official calendar can be substituted.
Reference: https://www.nyse.com/markets/hours-calendars (rules re-implemented; verify yearly).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
PRE_OPEN = time(4, 0)
POST_CLOSE = time(20, 0)

Session = Literal["premarket", "regular", "postmarket", "closed"]

# One-off full-day closures outside the normal holiday rules.
SPECIAL_CLOSURES: frozenset[date] = frozenset(
    {
        date(2018, 12, 5),  # National Day of Mourning (G.H.W. Bush)
        date(2025, 1, 9),  # National Day of Mourning (J. Carter)
    }
)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date, *, saturday_to_friday: bool = True) -> date | None:
    if d.weekday() == 5:
        return d - timedelta(days=1) if saturday_to_friday else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> dict[date, str]:
    """Full-day NYSE holidays whose *observed* date falls in ``year``."""
    hol: dict[date, str] = {}

    def add(d: date | None, name: str) -> None:
        if d is not None and d.year == year:
            hol[d] = name

    # New Year's Day: if on Saturday, NYSE stays open the preceding Friday.
    add(_observed(date(year, 1, 1), saturday_to_friday=False), "New Year's Day")
    add(_observed(date(year + 1, 1, 1), saturday_to_friday=False), "New Year's Day")  # never lands in `year`
    add(_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day")
    add(_nth_weekday(year, 2, 0, 3), "Washington's Birthday")
    add(easter(year) - timedelta(days=2), "Good Friday")
    add(_last_weekday(year, 5, 0), "Memorial Day")
    if year >= 2022:
        add(_observed(date(year, 6, 19)), "Juneteenth")
    add(_observed(date(year, 7, 4)), "Independence Day")
    add(_nth_weekday(year, 9, 0, 1), "Labor Day")
    add(_nth_weekday(year, 11, 3, 4), "Thanksgiving Day")
    add(_observed(date(year, 12, 25)), "Christmas Day")
    return hol


def nyse_early_closes(year: int) -> dict[date, str]:
    """Days closing at 13:00 ET."""
    out: dict[date, str] = {}
    hol = nyse_holidays(year)
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    out[thanksgiving + timedelta(days=1)] = "Day after Thanksgiving"
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 4 and jul3 not in hol:  # Mon-Thu; Friday Jul 3 is the observed holiday
        out[jul3] = "Independence Day eve"
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and dec24 not in hol:
        out[dec24] = "Christmas Eve"
    return {d: n for d, n in out.items() if d not in hol and d.weekday() < 5}


class ExchangeCalendar(Protocol):
    def is_trading_day(self, d: date) -> bool: ...
    def close_time(self, d: date) -> time | None: ...
    def session_at(self, ts: datetime) -> Session: ...
    def trading_days(self, start: date, end: date) -> list[date]: ...
    def next_trading_day(self, d: date) -> date: ...


@dataclass
class NyseCalendar:
    """NYSE calendar with configurable extended-hours acceptance.

    ``extra_holidays`` / ``extra_early_closes`` allow operators to patch the calendar without code
    changes (e.g. a newly announced closure).
    """

    enable_premarket: bool = False
    enable_postmarket: bool = False
    extra_holidays: frozenset[date] = frozenset()
    extra_early_closes: frozenset[date] = frozenset()

    def is_trading_day(self, d: date) -> bool:
        if d.weekday() >= 5 or d in SPECIAL_CLOSURES or d in self.extra_holidays:
            return False
        return d not in nyse_holidays(d.year)

    def holiday_name(self, d: date) -> str | None:
        if d in SPECIAL_CLOSURES:
            return "Special closure"
        return nyse_holidays(d.year).get(d)

    def is_early_close(self, d: date) -> bool:
        return self.is_trading_day(d) and (d in nyse_early_closes(d.year) or d in self.extra_early_closes)

    def close_time(self, d: date) -> time | None:
        if not self.is_trading_day(d):
            return None
        return EARLY_CLOSE if self.is_early_close(d) else CLOSE

    def open_dt(self, d: date) -> datetime:
        return datetime.combine(d, OPEN, ET).astimezone(UTC)

    def close_dt(self, d: date) -> datetime:
        ct = self.close_time(d)
        if ct is None:
            raise ValueError(f"{d} is not a trading day")
        return datetime.combine(d, ct, ET).astimezone(UTC)

    def session_at(self, ts: datetime) -> Session:
        local = ts.astimezone(ET)
        d = local.date()
        if not self.is_trading_day(d):
            return "closed"
        ct = self.close_time(d)
        assert ct is not None
        t = local.time()
        if OPEN <= t < ct:
            return "regular"
        if PRE_OPEN <= t < OPEN:
            return "premarket"
        if ct <= t < POST_CLOSE:
            return "postmarket"
        return "closed"

    def session_allowed(self, session: Session) -> bool:
        return session == "regular" or (
            (session == "premarket" and self.enable_premarket)
            or (session == "postmarket" and self.enable_postmarket)
        )

    def trading_days(self, start: date, end: date) -> list[date]:
        out, d = [], start
        while d <= end:
            if self.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def next_trading_day(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def prev_trading_day(self, d: date) -> date:
        d -= timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def regular_minutes(self, d: date) -> list[datetime]:
        """UTC start times of every 1-minute regular-session bar for ``d`` (empty if closed)."""
        if not self.is_trading_day(d):
            return []
        start, end = self.open_dt(d), self.close_dt(d)
        n = int((end - start).total_seconds() // 60)
        return [start + timedelta(minutes=i) for i in range(n)]

    def minutes_since_open(self, ts: datetime) -> float:
        local = ts.astimezone(ET)
        return (local - datetime.combine(local.date(), OPEN, ET)).total_seconds() / 60.0


def to_et(ts: datetime) -> datetime:
    return ts.astimezone(ET)


def et_date(ts: datetime) -> date:
    return ts.astimezone(ET).date()
