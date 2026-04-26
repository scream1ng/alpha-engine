"""Crypto pipeline. Markets never close — run daily at 00:05 UTC."""
from __future__ import annotations
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main(command: str = "scan", args: argparse.Namespace | None = None) -> None:
    from markets.crypto import CryptoAdapter
    from scripts.pipeline import run, make_parser
    if args is None:
        args = make_parser("crypto").parse_args([command])
    run(CryptoAdapter(), command, args)


if __name__ == "__main__":
    from db.models import init_db
    from scripts.pipeline import make_parser, run
    from markets.crypto import CryptoAdapter
    init_db()
    a = make_parser("crypto").parse_args()
    run(CryptoAdapter(), a.command, a)
