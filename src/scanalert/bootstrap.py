"""Construction helpers shared by the API lifespan, the CLI and tests."""

from __future__ import annotations

from pathlib import Path

from .config import Settings, assert_paper_only
from .db import Store, make_engine, migrate
from .providers.base import MarketDataProvider
from .providers.fixtures import FaultPlan, FixtureData, FixtureProvider, generate_fixture, read_csv

FIXTURE_CSV = Path("data/fixtures/bars_1min.csv")


def load_fixture_data(settings: Settings) -> FixtureData:
    if FIXTURE_CSV.is_file():
        data = read_csv(FIXTURE_CSV)
        missing = [s for s in settings.universe_symbols if s not in data.bars]
        if not missing:
            return data
    return generate_fixture(settings.universe_symbols, seed=settings.fixture_seed)


def build_provider(settings: Settings, faults: FaultPlan | None = None) -> MarketDataProvider:
    assert_paper_only(settings)
    if settings.data_provider == "alpaca":
        from .providers.alpaca import AlpacaProvider

        return AlpacaProvider(settings)
    return FixtureProvider(load_fixture_data(settings), speed=settings.fixture_replay_speed, faults=faults)


def build_store(settings: Settings, provider_name: str, feed: str) -> Store:
    engine = make_engine(settings.database_url)
    migrate(engine)
    return Store(engine, provider_name, feed)
