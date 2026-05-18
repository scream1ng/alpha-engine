from __future__ import annotations

import json
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yfinance as yf
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.scheduler.runner import shutdown_scheduler, start_scheduler
from core.active_roster import sync_active_roster_file_to_db
ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
SNAPSHOT_PATH = DOCS_DIR / "chart_data.json"
CACHE_DIR = Path(os.getenv("OHLCV_CACHE_DIR", ROOT / ".cache" / "ohlcv"))
CACHE_TTL_SECONDS = int(os.getenv("OHLCV_CACHE_TTL_SECONDS", "43200"))
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.^=_-]{1,32}$")

from db.models import (
    init_db, SessionLocal,
    ActiveStrategyModel, ScanSignalModel,
    PaperPositionModel, PaperTradeModel, PipelineLog,
)

_PAPER_CAPITAL = 100_000.0

# Create tables on startup (no-op if already exist)
init_db()

app = FastAPI(title="Alpha Engine API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.on_event("startup")
def sync_active_roster_on_startup() -> None:
    sync_active_roster_file_to_db(source="startup", record_noop=False)
    start_scheduler()


@app.on_event("shutdown")
def stop_scheduler_on_shutdown() -> None:
    shutdown_scheduler()


def _read_json(path: Path) -> Any:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Missing file: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_symbol(symbol: str) -> str:
    symbol = symbol.strip()
    if not SYMBOL_RE.match(symbol):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    return symbol


def _cache_path(market: str, symbol: str, period: str, start: str | None, end: str | None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._=-]+", "_", f"{market}_{symbol}_{period}_{start or ''}_{end or ''}")
    return CACHE_DIR / f"{safe}.json"


def _cache_fresh(path: Path) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS


def _download_ohlcv(symbol: str, period: str, start: str | None, end: str | None) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "auto_adjust": False,
        "progress": False,
        "threads": False,
    }
    if start or end:
        kwargs["start"] = start
        kwargs["end"] = end
    else:
        kwargs["period"] = period

    df = yf.download(symbol, **kwargs)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No OHLCV found for {symbol}")
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)

    rows: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        if not all(col in row for col in ("Open", "High", "Low", "Close")):
            continue
        rows.append(
            {
                "time": idx.strftime("%Y-%m-%d"),
                "open": _round_float(row["Open"]),
                "high": _round_float(row["High"]),
                "low": _round_float(row["Low"]),
                "close": _round_float(row["Close"]),
                "volume": int(row["Volume"]) if "Volume" in row and row["Volume"] == row["Volume"] else 0,
            }
        )
    if not rows:
        raise HTTPException(status_code=404, detail=f"No OHLCV rows found for {symbol}")
    return rows


def _round_float(value: Any) -> float:
    return round(float(value), 6)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/snapshot")
def snapshot() -> Any:
    return _read_json(SNAPSHOT_PATH)


@app.get("/api/ohlcv")
def ohlcv(
    market: str = Query(..., min_length=2, max_length=16),
    symbol: str = Query(..., min_length=1, max_length=32),
    period: str = Query("5y", pattern=r"^(1mo|3mo|6mo|1y|2y|5y|10y|max)$"),
    start: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    refresh: bool = False,
) -> dict[str, Any]:
    market_key = market.strip().upper()
    ticker = _safe_symbol(symbol)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(market_key, ticker, period, start, end)

    if not refresh and _cache_fresh(path):
        return {**_read_json(path), "source": "cache"}

    rows = _download_ohlcv(ticker, period, start, end)
    payload = {
        "market": market_key,
        "symbol": ticker,
        "period": period,
        "start": start,
        "end": end,
        "ohlcv": rows,
        "source": "yfinance",
    }
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return payload


@app.get("/api/active")
def active_roster(market: str = Query(..., min_length=1, max_length=16)) -> list[dict]:
    """Current active strategy roster for a market."""
    m = market.strip().lower()
    db = SessionLocal()
    try:
        rows = (
            db.query(ActiveStrategyModel)
            .filter_by(market=m, status="active")
            .order_by(ActiveStrategyModel.strategy, ActiveStrategyModel.regime)
            .all()
        )
        return [
            {
                "id":           r.id,
                "market":       r.market,
                "strategy":     r.strategy,
                "regime":       r.regime,
                "promoted_at":  str(r.promoted_at),
                "source_combo": r.source_combo,
                "status":       r.status,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.get("/api/signals")
def signals(
    market: str = Query(..., min_length=1, max_length=16),
    days:   int = Query(7, ge=1, le=90),
) -> list[dict]:
    """Recent scan signals for a market."""
    m        = market.strip().lower()
    cutoff   = date.today() - timedelta(days=days)
    db       = SessionLocal()
    try:
        rows = (
            db.query(ScanSignalModel)
            .filter(
                ScanSignalModel.market    == m,
                ScanSignalModel.scan_date >= cutoff,
            )
            .order_by(ScanSignalModel.scan_date.desc(), ScanSignalModel.strategy)
            .all()
        )
        return [
            {
                "id":               r.id,
                "scan_date":        str(r.scan_date),
                "symbol":           r.symbol,
                "strategy":         r.strategy,
                "regime":           r.regime_at_signal,
                "direction":        r.direction,
                "entry_price":      r.entry_price,
                "sl_price":         r.sl_price,
                "tp1_price":        r.tp1_price,
                "tp2_price":        r.tp2_price,
                "atr":              r.atr_at_entry,
                "rvol":             r.rvol_at_entry,
                "status":           r.status,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.get("/api/active-history")
def active_history(
    market: str = Query(..., min_length=1, max_length=16),
    limit: int = Query(10, ge=1, le=50),
) -> list[dict]:
    """Recent active-roster sync history for a market."""
    m = market.strip().lower()
    db = SessionLocal()
    try:
        rows = (
            db.query(PipelineLog)
            .filter_by(market=m, stage="active-sync")
            .order_by(PipelineLog.logged_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "logged_at": str(r.logged_at),
                "outcome": r.outcome,
                "details": r.details or {},
            }
            for r in rows
        ]
    finally:
        db.close()


@app.get("/api/paper")
def paper_portfolio(market: str = Query(..., min_length=1, max_length=16)) -> dict:
    """Paper portfolio: open positions, recent trades, summary."""
    m  = market.strip().lower()
    db = SessionLocal()
    try:
        open_pos = (
            db.query(PaperPositionModel)
            .filter_by(market=m, status="open")
            .order_by(PaperPositionModel.entry_date.desc())
            .all()
        )
        recent_trades = (
            db.query(PaperTradeModel)
            .join(PaperPositionModel, PaperTradeModel.position_id == PaperPositionModel.id)
            .filter(PaperPositionModel.market == m)
            .order_by(PaperTradeModel.exit_date.desc())
            .limit(50)
            .all()
        )
        all_trades = (
            db.query(PaperTradeModel)
            .join(PaperPositionModel, PaperTradeModel.position_id == PaperPositionModel.id)
            .filter(PaperPositionModel.market == m)
            .all()
        )

        total_pnl  = sum(t.pnl or 0.0 for t in all_trades)
        equity     = _PAPER_CAPITAL + total_pnl
        closed     = [t for t in all_trades if not (t.exit_reason or "").startswith("tp")]
        wins       = [t for t in all_trades if (t.pnl or 0) > 0]
        win_rate   = len(wins) / len(all_trades) if all_trades else 0.0

        return {
            "summary": {
                "equity":          round(equity, 2),
                "initial_capital": _PAPER_CAPITAL,
                "total_pnl":       round(total_pnl, 2),
                "total_pnl_pct":   round(total_pnl / _PAPER_CAPITAL * 100, 2),
                "open_count":      len(open_pos),
                "closed_trades":   len(all_trades),
                "win_rate":        round(win_rate, 4),
            },
            "open_positions": [
                {
                    "id":               p.id,
                    "symbol":           p.symbol,
                    "strategy":         p.strategy,
                    "regime":           p.regime,
                    "entry_date":       str(p.entry_date),
                    "entry_price":      p.entry_price,
                    "sl_current":       p.sl_current,
                    "tp1_price":        p.tp1_price,
                    "tp2_price":        p.tp2_price,
                    "shares":           p.shares,
                    "remaining_shares": p.remaining_shares,
                    "bars_held":        p.bars_held,
                    "tp1_hit":          p.tp1_hit,
                    "unrealized_pnl":   None,  # Phase 5 will add live price
                }
                for p in open_pos
            ],
            "recent_trades": [
                {
                    "position_id": t.position_id,
                    "exit_date":   str(t.exit_date),
                    "exit_price":  t.exit_price,
                    "exit_reason": t.exit_reason,
                    "shares":      t.shares,
                    "pnl":         round(t.pnl or 0.0, 2),
                }
                for t in recent_trades
            ],
        }
    finally:
        db.close()


if DOCS_DIR.exists():
    app.mount("/", StaticFiles(directory=DOCS_DIR, html=True), name="docs")
