"""US market pipeline. Cron: NYSE close + 1hr (21:00 UTC)."""
from __future__ import annotations
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main(command: str = "scan", args: argparse.Namespace | None = None) -> None:
    from markets.us import USAdapter
    from scripts.pipeline import run, make_parser
    if args is None:
        args = make_parser("us").parse_args([command])
    run(USAdapter(), command, args)


if __name__ == "__main__":
    from db.models import init_db
    from scripts.pipeline import make_parser, run
    from markets.us import USAdapter
    init_db()
    a = make_parser("us").parse_args()
    run(USAdapter(), a.command, a)
