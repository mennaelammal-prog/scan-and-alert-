from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from scanalert.calendar import NyseCalendar
from scanalert.config import Settings
from scanalert.models import Bar
from scanalert.providers.fixtures import FixtureData, generate_fixture

UTC_OPEN_SEP28 = datetime.fromisoformat("2026-09-28T13:30:00+00:00")  # 09:30 ET (EDT)


@pytest.fixture(scope="session")
def cal() -> NyseCalendar:
    return NyseCalendar()


@pytest.fixture(scope="session")
def fx() -> FixtureData:
    return generate_fixture()


@pytest.fixture()
def settings() -> Settings:
    return Settings(database_url="sqlite://", fixture_autostart=False)


def mkbar(sym: str, ts: datetime, o: float, h: float, low: float, c: float, v: float = 1000, **kw) -> Bar:
    return Bar(sym, ts, o, h, low, c, v, **kw)


def flat_day(
    sym: str, d: date, price: float = 10.0, vol: float = 1000, cal: NyseCalendar | None = None
) -> list[Bar]:
    cal = cal or NyseCalendar()
    return [mkbar(sym, m, price, price, price, price, vol) for m in cal.regular_minutes(d)]


def minute(d: date, hh: int, mm: int, cal: NyseCalendar | None = None) -> datetime:
    cal = cal or NyseCalendar()
    return cal.open_dt(d) + timedelta(minutes=(hh - 9) * 60 + mm - 30)
