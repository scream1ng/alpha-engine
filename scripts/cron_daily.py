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
from datetime import date

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


def _log_pipeline_event(market: str, stage: str, outcome: str, details: dict | None = None) -> None:
    from db.models import PipelineLog, SessionLocal

    db = SessionLocal()
    try:
        db.add(PipelineLog(
            market=market,
            stage=stage,
            outcome=outcome,
            details=details or {},
        ))
        db.commit()
    finally:
        db.close()


def _scan_summary(market: str, as_of: date) -> dict:
    from db.models import ScanSignalModel, SessionLocal

    db = SessionLocal()
    try:
        rows = db.query(ScanSignalModel).filter_by(market=market, scan_date=as_of).all()
        by_status: dict[str, int] = {}
        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        active_pairs = len({(row.strategy, row.regime_at_signal) for row in rows})
        return {
            "scan_date": str(as_of),
            "signals": len(rows),
            "active_pairs": active_pairs,
            "by_status": by_status,
            "summary": (
                f"scan {as_of.isoformat()} sig={len(rows)} active={active_pairs} "
                f"new={by_status.get('new', 0)} opened={by_status.get('opened', 0)}"
            ),
        }
    finally:
        db.close()


def _paper_summary(market: str, as_of: date) -> dict:
    from sqlalchemy import func
    from db.models import PaperPositionModel, PaperTradeModel, SessionLocal

    db = SessionLocal()
    try:
        open_count = db.query(PaperPositionModel).filter_by(market=market, status="open").count()
        opened_today = db.query(PaperPositionModel).filter_by(market=market, entry_date=as_of).count()
        exits_today = (
            db.query(PaperTradeModel)
            .join(PaperPositionModel, PaperTradeModel.position_id == PaperPositionModel.id)
            .filter(PaperPositionModel.market == market, PaperTradeModel.exit_date == as_of)
            .count()
        )
        realized_today = (
            db.query(func.coalesce(func.sum(PaperTradeModel.pnl), 0.0))
            .join(PaperPositionModel, PaperTradeModel.position_id == PaperPositionModel.id)
            .filter(PaperPositionModel.market == market, PaperTradeModel.exit_date == as_of)
            .scalar()
        ) or 0.0
        return {
            "as_of": str(as_of),
            "open_positions": open_count,
            "opened_today": opened_today,
            "exits_today": exits_today,
            "realized_today": round(float(realized_today), 2),
            "summary": (
                f"paper {as_of.isoformat()} open={open_count} opened={opened_today} "
                f"exits={exits_today} realized={round(float(realized_today), 2):.2f}"
            ),
        }
    finally:
        db.close()


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
    today = date.today()

    for market in markets:
        if market not in _ADAPTERS:
            logger.warning("Unknown market %s — skipping.", market)
            continue

        logger.info("=== %s: scan ===", market.upper())
        try:
            adapter = _get_adapter(market)
            cmd_scan(adapter, args)
            _log_pipeline_event(market, "scan", "ok", _scan_summary(market, today))
        except Exception as _scan_exc:
            import traceback as _tb
            _log_pipeline_event(market, "scan", "failed", {
                "summary": f"scan {today.isoformat()} failed",
                "error": type(_scan_exc).__name__,
                "detail": str(_scan_exc)[:400],
                "traceback": _tb.format_exc()[-800:],
            })
            logger.exception("scan failed for %s", market)
            continue

        logger.info("=== %s: paper-update ===", market.upper())
        try:
            adapter = _get_adapter(market)
            cmd_paper_update(adapter, args)
            _log_pipeline_event(market, "paper-update", "ok", _paper_summary(market, today))
        except Exception:
            _log_pipeline_event(market, "paper-update", "failed", {"summary": f"paper {today.isoformat()} failed"})
            logger.exception("paper-update failed for %s", market)

    logger.info("cron_daily done.")
    return {"markets": markets, "ran": len(markets)}


def main() -> None:
    raw     = os.getenv("LIVE_MARKETS", ",".join(_DEFAULT_MARKETS))
    markets = [m.strip().lower() for m in raw.split(",") if m.strip()]
    run_markets(markets)


if __name__ == "__main__":
    main()
