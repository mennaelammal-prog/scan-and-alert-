"""Typed settings and the fail-closed paper-only environment guard.

This module is the single choke point for the safety boundary: the application
refuses to start when anything in the process environment, the ``.env`` file, or
the typed settings looks like a live brokerage endpoint, live credential, or
live-trading flag. There is deliberately no override switch.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PAPER = "paper"

# Hosts that route real orders or hold live brokerage accounts. Matching is done on the parsed
# hostname (exact or subdomain), never a substring of the whole URL.
LIVE_BROKER_HOSTS: frozenset[str] = frozenset(
    {
        "api.alpaca.markets",  # Alpaca LIVE trading (paper is paper-api.alpaca.markets)
        "broker-api.alpaca.markets",
        "api.tradestation.com",  # TradeStation LIVE (SIM is sim-api.tradestation.com)
        "api.ibkr.com",  # IBKR Client Portal / Web API
        "ndcdyn.interactivebrokers.com",
        "api.tradier.com",  # Tradier LIVE (sandbox.tradier.com)
        "api.schwabapi.com",
        "api-fxtrade.oanda.com",
        "api.robinhood.com",
        "trade.webull.com",
        "api.etrade.com",
        "api.coinbase.com",
        "api.binance.com",
    }
)

# Paper/sim hosts are named here only so diagnostics can say "paper endpoint recognised".
KNOWN_PAPER_HOSTS: frozenset[str] = frozenset(
    {"paper-api.alpaca.markets", "sim-api.tradestation.com", "sandbox.tradier.com"}
)

# Market-DATA hosts that this application may talk to. Anything else in a *_URL setting that is not
# loopback is rejected by ``validate_data_url``.
ALLOWED_DATA_HOSTS: frozenset[str] = frozenset(
    {"stream.data.alpaca.markets", "data.alpaca.markets", "api.massive.com", "localhost", "127.0.0.1", "::1"}
)

# IBKR TWS/Gateway live ports (paper: 7497 / 4002).
LIVE_BROKER_PORTS: frozenset[int] = frozenset({7496, 4001})

_FALSEY = {"", "0", "false", "no", "off", "none", "null"}
_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|ACCOUNT)", re.I)
_LIVE_SEGMENT = re.compile(r"(^|[^A-Za-z0-9])LIVE([^A-Za-z0-9]|$)", re.I)
_URL_RE = re.compile(r"(?:https?|wss?)://[^\s\"',;]+", re.I)


class PaperOnlyViolation(RuntimeError):
    """Raised when configuration would allow, or could be mistaken for, live trading."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("Refusing to start (paper-only guard): " + "; ".join(violations))


def _host_matches(host: str, blocked: frozenset[str]) -> str | None:
    host = host.lower().rstrip(".")
    for b in blocked:
        if host == b or host.endswith("." + b):
            return b
    return None


def inspect_url(url: str) -> list[str]:
    """Return violations for a single URL (empty list means acceptable)."""
    problems: list[str] = []
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return [f"unparseable URL {url[:40]!r}"]
    host = (parsed.hostname or "").lower()
    if not host:
        return problems
    hit = _host_matches(host, LIVE_BROKER_HOSTS)
    if hit:
        problems.append(f"live broker endpoint detected: {hit}")
    if parsed.port in LIVE_BROKER_PORTS and host in {"localhost", "127.0.0.1", "::1"}:
        problems.append(f"live broker gateway port detected: {parsed.port}")
    return problems


def scan_environment(env: Mapping[str, str | None]) -> list[str]:
    """Scan an environment mapping (process env or dotenv values) for live indicators."""
    violations: list[str] = []
    for name, raw in env.items():
        value = "" if raw is None else str(raw)
        upper = name.upper()
        stripped = value.strip()
        truthy = stripped.lower() not in _FALSEY

        if upper == "TRADING_MODE" and stripped.lower() != PAPER and stripped != "":
            violations.append(f"TRADING_MODE must be 'paper' (got {stripped!r})")
        if _LIVE_SEGMENT.search(name) and truthy:
            if _SECRET_NAME.search(name):
                violations.append(f"live credential variable present: {name}")
            else:
                violations.append(f"live-trading flag/variable enabled: {name}")
        if upper in {"ENABLE_LIVE_TRADING", "LIVE_TRADING", "ALLOW_LIVE", "ALLOW_LIVE_TRADING"} and truthy:
            violations.append(f"live-trading flag enabled: {name}")
        # Standard Alpaca SDK variable; a live base URL here is the classic accident.
        for url in _URL_RE.findall(value):
            for p in inspect_url(url):
                violations.append(f"{name}: {p}")
        if upper.endswith(("_URL", "_ENDPOINT", "_HOST", "_BASE_URL")) and "://" not in value and value:
            for p in inspect_url("//" + value):
                violations.append(f"{name}: {p}")
    return violations


def dotenv_path() -> Path:
    return Path(os.environ.get("SCANALERT_ENV_FILE", ".env"))


def collect_env(
    extra: Mapping[str, str | None] | None = None, env_file: Path | None = None
) -> dict[str, str | None]:
    merged: dict[str, str | None] = {}
    path = env_file if env_file is not None else dotenv_path()
    if path.is_file():
        merged.update({f".env:{k}": v for k, v in dotenv_values(path).items()})
    merged.update(os.environ)
    if extra:
        merged.update(extra)
    # Names from dotenv are prefixed for provenance; strip the prefix for pattern checks.
    return {k.removeprefix(".env:"): v for k, v in merged.items()}


class Settings(BaseSettings):
    """Typed application settings. ``trading_mode`` is a Literal so any other value fails validation."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    trading_mode: Literal["paper"] = "paper"
    data_provider: Literal["fixture", "alpaca"] = "fixture"
    database_url: str = "sqlite:///./data/scanalert.db"
    host: str = "127.0.0.1"
    port: int = 8000

    universe: str = "ORBX,GAPU,TRND,FLAT,WIDE,SPKE,DRFT,LOWV"
    fixture_replay_speed: float = 0.0
    fixture_seed: int = 42
    fixture_autostart: bool = True

    alpaca_data_key_id: str = ""
    alpaca_data_secret_key: str = ""
    alpaca_data_feed: Literal["iex", "sip", "test"] = "iex"
    alpaca_data_ws_url: str = "wss://stream.data.alpaca.markets/v2"
    alpaca_data_rest_url: str = "https://data.alpaca.markets"

    enable_premarket: bool = False
    enable_postmarket: bool = False

    stale_feed_seconds: float = Field(15.0, gt=0)
    delayed_delivery_ms: int = Field(2000, ge=0)
    top_list_refresh_seconds: float = Field(30.0, gt=0)
    top_list_max_rows: int = Field(100, ge=1, le=1000)
    eval_min_interval_ms: int = Field(250, ge=0)
    queue_maxsize: int = Field(10_000, ge=100)
    persist_raw_events: bool = True

    email_enabled: bool = False
    webhook_enabled: bool = False
    webhook_url: str = ""

    @field_validator("alpaca_data_ws_url", "alpaca_data_rest_url", "webhook_url")
    @classmethod
    def _no_live_urls(cls, v: str) -> str:
        if v:
            problems = inspect_url(v)
            if problems:
                raise ValueError("; ".join(problems))
        return v

    @property
    def universe_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.universe.split(",") if s.strip()]

    def redacted(self) -> dict[str, object]:
        """Diagnostics view that never contains secret values."""
        out: dict[str, object] = {}
        for k, v in self.model_dump().items():
            if _SECRET_NAME.search(k):
                out[k] = "<set>" if v else "<unset>"
            elif k.endswith("_url") and isinstance(v, str) and v:
                p = urlparse(v)
                # scheme://host/path only: never userinfo (passwords) or query strings
                out[k] = f"{p.scheme}://{p.hostname or ''}{f':{p.port}' if p.port else ''}{p.path}"
            else:
                out[k] = v
        out["paper_only"] = True
        return out


def validate_data_url(url: str) -> None:
    """Data adapters may only talk to known market-data hosts (or loopback for tests)."""
    problems = inspect_url(url)
    host = (urlparse(url).hostname or "").lower()
    if not problems and host not in ALLOWED_DATA_HOSTS:
        problems.append(f"host {host!r} is not an allowed market-data host")
    if problems:
        raise PaperOnlyViolation(problems)


def assert_paper_only(
    settings: Settings | None = None,
    env: Mapping[str, str | None] | None = None,
    env_file: Path | None = None,
) -> None:
    """Fail closed. Raises :class:`PaperOnlyViolation` listing every violation found."""
    violations: list[str] = []
    merged = collect_env(extra=None, env_file=env_file) if env is None else dict(env)
    violations.extend(scan_environment(merged))
    if settings is not None:
        if settings.trading_mode != PAPER:  # defence in depth; the Literal already blocks this
            violations.append("settings.trading_mode is not 'paper'")
        for name in ("alpaca_data_ws_url", "alpaca_data_rest_url", "webhook_url", "database_url"):
            value = getattr(settings, name)
            if value:
                violations.extend(f"{name}: {p}" for p in inspect_url(value))
        if settings.data_provider == "alpaca":
            for name in ("alpaca_data_ws_url", "alpaca_data_rest_url"):
                host = (urlparse(getattr(settings, name)).hostname or "").lower()
                if host not in ALLOWED_DATA_HOSTS:
                    violations.append(f"{name}: host {host!r} is not an allowed market-data host")
    if violations:
        raise PaperOnlyViolation(sorted(set(violations)))


def load_settings(env_file: str | None = None) -> Settings:
    """Load settings and run the guard. Every entry point must call this, never ``Settings()``."""
    try:
        settings = Settings(_env_file=env_file) if env_file is not None else Settings()
    except Exception as exc:  # pydantic ValidationError for TRADING_MODE=live etc.
        env_map = collect_env(env_file=Path(env_file) if env_file else None)
        problems = scan_environment(env_map)
        raise PaperOnlyViolation(problems or [f"invalid configuration: {exc}"]) from exc
    assert_paper_only(settings, env_file=Path(env_file) if env_file else None)
    return settings
