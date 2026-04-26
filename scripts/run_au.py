"""AU market pipeline. Cron: ASX close + 1hr (07:30 UTC)."""
from __future__ import annotations
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main(command: str = "scan", args: argparse.Namespace | None = None) -> None:
    from markets.au import AUAdapter
    from scripts.pipeline import run, make_parser
    if args is None:
        args = make_parser("au").parse_args([command])
    run(AUAdapter(), command, args)


if __name__ == "__main__":
    from db.models import init_db
    from scripts.pipeline import make_parser, run
    from markets.au import AUAdapter
    init_db()
    a = make_parser("au").parse_args()
    run(AUAdapter(), a.command, a)
