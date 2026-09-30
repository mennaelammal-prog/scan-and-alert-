"""Paper-only guard: live endpoints, credentials and flags must make startup fail closed."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scanalert.config import (
    PaperOnlyViolation,
    Settings,
    assert_paper_only,
    inspect_url,
    load_settings,
    scan_environment,
    validate_data_url,
)

ROOT = Path(__file__).resolve().parents[1]


def test_default_is_paper():
    assert Settings().trading_mode == "paper"


def test_trading_mode_live_is_rejected_by_type():
    with pytest.raises(ValueError):
        Settings(trading_mode="live")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets/v2/orders",
        "https://broker-api.alpaca.markets",
        "https://api.tradestation.com/v3",
        "https://api.ibkr.com/v1/api",
        "https://api.tradier.com/v1",
        "https://sub.api.alpaca.markets",
        "http://127.0.0.1:7496",
        "http://localhost:4001",
    ],
)
def test_live_broker_urls_detected(url):
    assert inspect_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://paper-api.alpaca.markets/v2",
        "https://sim-api.tradestation.com/v3",
        "https://data.alpaca.markets",
        "wss://stream.data.alpaca.markets/v2/iex",
        "http://127.0.0.1:8000",
        "http://localhost:7497",
    ],
)
def test_paper_and_data_urls_allowed(url):
    assert inspect_url(url) == []


def test_host_match_is_hostname_not_substring():
    assert inspect_url("https://example.com/?next=api.alpaca.markets") == []
    assert inspect_url("https://notapi.alpaca.markets.evil.example") == []


@pytest.mark.parametrize(
    "env",
    [
        {"TRADING_MODE": "live"},
        {"TRADING_MODE": "Live"},
        {"ENABLE_LIVE_TRADING": "true"},
        {"LIVE_TRADING": "1"},
        {"ALPACA_LIVE_KEY_ID": "AKxxxx"},
        {"LIVE_API_SECRET": "s3cr3t"},
        {"APCA_API_BASE_URL": "https://api.alpaca.markets"},
        {"BROKER_ENDPOINT": "wss://api.tradestation.com/stream"},
        {"SOMETHING": "see https://api.alpaca.markets/v2/orders"},
        {"IB_GATEWAY_URL": "http://localhost:4001"},
    ],
)
def test_environment_scan_flags_live_indicators(env):
    assert scan_environment(env), env


def test_clean_environment_passes():
    assert (
        scan_environment(
            {"TRADING_MODE": "paper", "PATH": "/usr/bin", "ALPACA_DATA_KEY_ID": "x", "LIVE_TRADING": "false"}
        )
        == []
    )


def test_assert_paper_only_raises_with_all_violations(monkeypatch):
    with pytest.raises(PaperOnlyViolation) as ei:
        assert_paper_only(Settings(), env={"TRADING_MODE": "live", "ALPACA_LIVE_SECRET_KEY": "x"})
    assert len(ei.value.violations) >= 2


def test_settings_reject_live_urls_in_fields():
    with pytest.raises(ValueError):
        Settings(alpaca_data_rest_url="https://api.alpaca.markets")
    with pytest.raises(ValueError):
        Settings(webhook_url="https://api.tradestation.com/hook")


def test_data_url_allowlist():
    validate_data_url("https://data.alpaca.markets")
    with pytest.raises(PaperOnlyViolation):
        validate_data_url("https://evil.example.com")
    with pytest.raises(PaperOnlyViolation):
        validate_data_url("https://api.alpaca.markets")


def test_load_settings_refuses_live_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TRADING_MODE=paper\nALPACA_LIVE_KEY_ID=AKlive123\n")
    monkeypatch.delenv("TRADING_MODE", raising=False)
    with pytest.raises(PaperOnlyViolation) as ei:
        load_settings(str(env))
    assert any("live credential" in v for v in ei.value.violations)


def test_load_settings_refuses_live_process_env(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(PaperOnlyViolation):
        load_settings()


def test_load_settings_ok_in_clean_env(monkeypatch, tmp_path):
    for k in list(__import__("os").environ):
        if "LIVE" in k.upper() or k.upper().startswith(("APCA", "TRADING")):
            monkeypatch.delenv(k, raising=False)
    s = load_settings(str(tmp_path / "missing.env"))
    assert s.trading_mode == "paper"


def test_redacted_settings_hide_secrets():
    s = Settings(alpaca_data_key_id="AKIDEXAMPLE", alpaca_data_secret_key="topsecret")
    out = s.redacted()
    assert "topsecret" not in str(out) and "AKIDEXAMPLE" not in str(out)
    assert out["alpaca_data_secret_key"] == "<set>" and out["paper_only"] is True


def test_repo_contains_no_live_broker_url_or_credential():
    """Fails if any live broker URL or live credential name appears anywhere in tracked project files."""
    from scanalert.config import LIVE_BROKER_HOSTS

    # Files that name live hosts on purpose: the guard itself, its docs, and negative tests asserting rejection.
    allowed_files = {
        "config.py",
        "test_safety.py",
        "PAPER_TRADING_SAFETY.md",
        "STATUS.md",
        "test_integration.py",
        "test_api_e2e.py",
        "test_stream.py",
    }
    offenders = []
    for path in list(ROOT.rglob("*")):
        if not path.is_file() or any(
            p in path.parts
            for p in (
                ".venv",
                ".git",
                "node_modules",
                ".run",
                "data",
                ".mypy_cache",
                ".ruff_cache",
                ".pytest_cache",
                "__pycache__",
                "scanalert.egg-info",
            )
        ):
            continue
        if (
            path.suffix
            not in {
                ".py",
                ".md",
                ".ps1",
                ".js",
                ".html",
                ".sql",
                ".toml",
                ".example",
                ".txt",
                ".json",
                ".yml",
                ".yaml",
            }
            and path.name != ".env.example"
        ):
            continue
        if path.name in allowed_files:
            continue
        text = path.read_text(errors="ignore")
        for host in LIVE_BROKER_HOSTS:
            for m in re.finditer(re.escape(host) + r"(/[^\s)\"'>]*)?", text):
                pre = text[max(0, m.start() - 8) : m.start()]
                path_part = m.group(1) or ""
                if "paper-" in pre or "sim-" in pre or path_part.startswith("/docs"):
                    continue  # paper hosts and vendor documentation links are not endpoints
                offenders.append(f"{path.relative_to(ROOT)}: {host}{path_part}")
        if re.search(r"^\s*(APCA_API_KEY_ID|ALPACA_LIVE\w*|LIVE_\w*(KEY|SECRET))\s*=\s*\S+", text, re.M):
            offenders.append(f"{path.relative_to(ROOT)}: live credential assignment")
    assert not offenders, offenders


def test_source_has_no_order_submission_code():
    """No broker SDK imports and no order-placing HTTP routes in the application package."""
    src = ROOT / "src" / "scanalert"
    banned = [
        r"alpaca_trade_api",
        r"alpaca\.trading",
        r"import\s+ib_insync",
        r"ibapi",
        r"tradestation",
        r"/v2/orders",
        r"place_order",
        r"submit_order",
        r"createOrder",
    ]
    hits = []
    for f in src.rglob("*.py"):
        if f.name == "config.py":
            continue
        text = f.read_text()
        for pat in banned:
            if re.search(pat, text, re.I):
                hits.append(f"{f.name}: {pat}")
    assert not hits, hits


def test_api_exposes_no_order_route():
    from scanalert.api import create_app

    app = create_app(Settings(database_url="sqlite://", fixture_autostart=False))
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not [
        p for p in paths if re.search(r"order|broker|execute|route-order", p, re.I) and "paper" not in p
    ]
    methods = set()
    for r in app.routes:
        if getattr(r, "path", "") == "/api/paper-intents":
            methods |= set(r.methods)
    assert methods == {"GET", "POST"}


def test_redacted_urls_hide_credentials_and_render_sqlite_sanely():
    out = Settings(
        database_url="postgresql+psycopg://user:hunter2@db.example.com:5432/scan?sslmode=require"
    ).redacted()
    assert out["database_url"] == "postgresql+psycopg://db.example.com:5432/scan"
    assert "hunter2" not in str(out)
    assert (
        Settings(database_url="sqlite:///./data/x.db").redacted()["database_url"] == "sqlite:///./data/x.db"
    )
