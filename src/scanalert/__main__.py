"""CLI: ``python -m scanalert <command>``."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scanalert", description="Paper-only stock scanner/alert platform")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-config", help="run the paper-only guard and print redacted settings")
    sub.add_parser("migrate", help="apply database migrations")
    sf = sub.add_parser("seed-fixtures", help="write deterministic synthetic 1-minute bars to data/fixtures")
    sf.add_argument("--days", type=int, default=20)
    sv = sub.add_parser("serve", help="run the API + UI")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sub.add_parser("smoke", help="start the server and exercise the main flow")
    args = p.parse_args(argv)

    from .config import PaperOnlyViolation, load_settings

    try:
        settings = load_settings()
    except PaperOnlyViolation as exc:
        print(f"REFUSING TO START: {exc}", file=sys.stderr)
        for v in exc.violations:
            print(f"  - {v}", file=sys.stderr)
        return 2

    print(
        f"[scanalert] mode={settings.trading_mode} provider={settings.data_provider} PAPER-ONLY (no live trading exists)"
    )
    if args.cmd == "check-config":
        print(json.dumps(settings.redacted(), indent=2))
        return 0
    if args.cmd == "migrate":
        from .db import make_engine, migrate

        applied = migrate(make_engine(settings.database_url))
        print(f"[scanalert] migrations applied: {applied or 'none (up to date)'}")
        return 0
    if args.cmd == "seed-fixtures":
        from .bootstrap import FIXTURE_CSV
        from .providers.fixtures import generate_fixture, write_csv

        data = generate_fixture(settings.universe_symbols, n_days=args.days, seed=settings.fixture_seed)
        n = write_csv(data, FIXTURE_CSV)
        print(
            f"[scanalert] wrote {n} bars for {len(data.bars)} symbols, {data.days[0]}..{data.days[-1]} -> {FIXTURE_CSV}"
        )
        return 0
    if args.cmd == "smoke":
        from .smoke import run_smoke

        return run_smoke()
    import uvicorn

    uvicorn.run(
        "scanalert.api:create_app",
        factory=True,
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
