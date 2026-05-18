"""
Daily cron entry point for Railway.

Configure in Railway dashboard:
  Command:  python -m scripts.cron_daily
  Schedule: 0 10 * * 1-5   (10:00 UTC = 17:00 Bangkok, after SET close)

Override markets via env var:
  LIVE_MARKETS=th,us   (comma-separated, lowercase market IDs)

If LIVE_MARKETS is not set, all supported markets are scanned.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys

from core.active_roster import sync_active_roster_file_to_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_ADAPTERS = {
    "th":        ("markets.th",        "THAdapter"),
    "us":        ("markets.us",        "USAdapter"),
    "au":        ("markets.au",        "AUAdapter"),
    "crypto":    ("markets.crypto",    "CryptoAdapter"),
    "commodity": ("markets.commodity", "CommodityAdapter"),
}

_DEFAULT_MARKETS = tuple(_ADAPTERS)


def _get_adapter(market: str):
    mod_path, cls_name = _ADAPTERS[market]
    mod = importlib.import_module(mod_path)
    return getattr(mod, cls_name)()


def run_markets(markets: list[str]) -> dict:
    from db.models import init_db
    init_db()

    if not markets:
        logger.error("LIVE_MARKETS is empty — nothing to do.")
        return {"markets": [], "ran": 0}

    sync_results = sync_active_roster_file_to_db(
        source="cron",
        record_noop=False,
        market_filter=markets,
    )
    if sync_results:
        for market, result in sorted(sync_results.items()):
            logger.info(
                "active-sync %s: active=%d +%d ~%d -%d",
                market.upper(),
                result["active_count"],
                len(result["added"]),
                len(result["changed"]),
                len(result["retired"]),
            )

    from scripts.pipeline import cmd_scan, cmd_paper_update
    import argparse
    args = argparse.Namespace(capital=100_000, symbols=None, dry_run=False, strategy_filter=None)

    for market in markets:
        if market not in _ADAPTERS:
            logger.warning("Unknown market %s — skipping.", market)
            continue

        logger.info("=== %s: scan ===", market.upper())
        try:
            adapter = _get_adapter(market)
            cmd_scan(adapter, args)
        except Exception:
            logger.exception("scan failed for %s", market)
            continue

        logger.info("=== %s: paper-update ===", market.upper())
        try:
            adapter = _get_adapter(market)
            cmd_paper_update(adapter, args)
        except Exception:
            logger.exception("paper-update failed for %s", market)

    logger.info("cron_daily done.")
    return {"markets": markets, "ran": len(markets)}


def main() -> None:
    raw     = os.getenv("LIVE_MARKETS", ",".join(_DEFAULT_MARKETS))
    markets = [m.strip().lower() for m in raw.split(",") if m.strip()]
    run_markets(markets)


if __name__ == "__main__":
    main()
