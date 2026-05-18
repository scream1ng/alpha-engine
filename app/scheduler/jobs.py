from __future__ import annotations

from scripts.cron_daily import run_markets


def run_equities_scan_paper() -> dict:
    """Run weekday equity markets."""
    return run_markets(["th", "us", "au"])


def run_commodity_scan_paper() -> dict:
    """Run commodity markets, including the Sunday futures session."""
    return run_markets(["commodity"])


def run_crypto_scan_paper() -> dict:
    """Run crypto markets every day."""
    return run_markets(["crypto"])
