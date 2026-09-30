"""Static checks for PowerShell scripts and required documentation (PowerShell cannot run on this Linux host)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [
    "setup",
    "install",
    "migrate",
    "test",
    "run-backend",
    "run-frontend",
    "run-dev",
    "stop-dev",
    "seed-fixtures",
    "run-smoke-test",
]
DOCS = [
    "README.md",
    "ARCHITECTURE.md",
    "API.md",
    "DATA_MODEL.md",
    "BACKTEST_METHODOLOGY.md",
    "PAPER_TRADING_SAFETY.md",
    "POWER_SHELL_SETUP.md",
    ".env.example",
    "CHANGELOG.md",
    "STATUS.md",
]


@pytest.mark.parametrize("name", SCRIPTS)
def test_powershell_script_contract(name):
    p = ROOT / "scripts" / f"{name}.ps1"
    assert p.exists(), p
    t = p.read_text()
    assert "Set-StrictMode -Version Latest" in t and '$ErrorActionPreference = "Stop"' in t
    assert "Write-Host" in t and "PAPER" in t.upper()
    assert re.search(r"Get-Location|\$PWD|\$Root", t)
    assert "TRADING_MODE" in t or name in ("stop-dev", "run-frontend")
    assert not re.search(r"(?i)(secret|password|token)\s*=\s*['\"][^'\"]+['\"]", t)
    assert (
        "live" not in t.lower().replace("deliver", "").replace("alive", "").replace("olive", "")
        or "no live" in t.lower()
        or "never live" in t.lower()
        or "not live" in t.lower()
    )


@pytest.mark.parametrize(
    "name",
    ["setup", "install", "migrate", "run-backend", "run-dev", "seed-fixtures", "run-smoke-test", "test"],
)
def test_scripts_offer_dry_run_and_fail_clearly_on_missing_dependencies(name):
    t = (ROOT / "scripts" / f"{name}.ps1").read_text()
    assert "-DryRun" in t or "-Status" in t
    assert "throw" in t or "exit 1" in t


@pytest.mark.parametrize("name", DOCS)
def test_required_docs_exist_and_nonempty(name):
    p = ROOT / name
    assert p.exists() and len(p.read_text()) > 300, name


def test_readme_covers_required_instructions():
    t = (ROOT / "README.md").read_text().lower()
    for k in ("setup", "migrat", "test", "run", "stop", "troubleshoot", "http://127.0.0.1:8000", "paper"):
        assert k in t, k


def test_backtest_methodology_discloses_required_limitations():
    t = (ROOT / "BACKTEST_METHODOLOGY.md").read_text().lower()
    for k in (
        "1-minute",
        "intrabar",
        "spread",
        "slippage",
        "commission",
        "coverage",
        "corporate action",
        "universe",
        "calendar",
        "survivorship",
        "look-ahead",
        "paper-fill",
    ):
        assert k in t, k


def test_docs_cite_required_sources():
    t = (
        (ROOT / "ARCHITECTURE.md").read_text()
        + (ROOT / "BACKTEST_METHODOLOGY.md").read_text()
        + (ROOT / "README.md").read_text()
    )
    for url in (
        "trade-ideas.com/guide/chapter/9/9Alert_Window.html",
        "trade-ideas.com/guide/chapter/10/10Top_List_Window.html",
        "trade-ideas.com/guide/chapter/22/22Backtesting_Oddsmaker.html",
        "docs.alpaca.markets/docs/real-time-stock-pricing-data",
        "nyse.com/markets/hours-calendars",
        "massive.com/docs",
    ):
        assert url in t, url


def test_env_example_has_only_safe_placeholders():
    t = (ROOT / ".env.example").read_text()
    assert "TRADING_MODE=paper" in t
    for line in t.splitlines():
        if re.match(r"^\s*[A-Z_]*(KEY|SECRET|TOKEN|PASSWORD)[A-Z_]*\s*=\s*\S+", line):
            pytest.fail(f"non-empty credential in .env.example: {line}")


def test_gitignore_excludes_secrets_and_data():
    t = (ROOT / ".gitignore").read_text()
    assert ".env" in t and "*.db" in t and ".venv" in t
