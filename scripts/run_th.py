"""
Thailand SET pipeline runner.
Cron: run at 17:30 ICT (SET close 16:30 + 1hr buffer).

Commands:
  scan      — generate today's signals from live params
  diagnose  — count signal fires per strategy (run before optimise)
  optimise  — run walk-forward on all enabled strategies
  paper     — run paper trading session
  validate  — full Phase 1+2 validation for all strategies
  live      — generate signals and write to DB for live execution
"""
from __future__ import annotations
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main(command: str = "scan", args: argparse.Namespace | None = None) -> None:
    from markets.th import THAdapter
    from scripts.pipeline import run, make_parser
    if args is None:
        args = make_parser("th").parse_args([command])
    run(THAdapter(), command, args)


if __name__ == "__main__":
    from db.models import init_db
    from scripts.pipeline import make_parser, run
    from markets.th import THAdapter
    init_db()
    a = make_parser("th").parse_args()
    run(THAdapter(), a.command, a)
