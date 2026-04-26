"""Commodity pipeline. Cron: US futures close + 1hr (22:00 UTC)."""
from __future__ import annotations
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main(command: str = "scan", args: argparse.Namespace | None = None) -> None:
    from markets.commodity import CommodityAdapter
    from scripts.pipeline import run, make_parser
    if args is None:
        args = make_parser("commodity").parse_args([command])
    run(CommodityAdapter(), command, args)


if __name__ == "__main__":
    from db.models import init_db
    from scripts.pipeline import make_parser, run
    from markets.commodity import CommodityAdapter
    init_db()
    a = make_parser("commodity").parse_args()
    run(CommodityAdapter(), a.command, a)
