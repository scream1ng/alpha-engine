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
    PaperPositionModel, PaperTradeModel, PipelineLog, RegimeLabelModel,
)

_PAPER_CAPITAL = 100_000.0

# Create tables on startup (no-op if already exist)
init_db()

app = FastAPI(title="Alpha Engine API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_methods=["GET", "POST"],
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


def _latest_close_for_symbol(market: str, symbol: str) -> float | None:
    market_key = market.strip().upper()
    ticker = _safe_symbol(symbol)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(market_key, ticker, "1mo", None, None)
    try:
        if _cache_fresh(path):
            payload = _read_json(path)
            rows = payload.get("ohlcv") or []
        else:
            rows = _download_ohlcv(ticker, "1mo", None, None)
            payload = {
                "market": market_key,
                "symbol": ticker,
                "period": "1mo",
                "start": None,
                "end": None,
                "ohlcv": rows,
                "source": "yfinance",
            }
            path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        if not rows:
            return None
        return _round_float(rows[-1]["close"])
    except Exception:
        return None


def _regime_periods(rows: list[RegimeLabelModel]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: row.date)
    if not ordered:
        return []

    periods: list[dict[str, Any]] = []
    current = ordered[0].regime
    start = ordered[0].date
    end = ordered[0].date
    bars = 1
    counts: dict[str, int] = {}

    def flush(regime: str, start_date: date, end_date: date, bar_count: int) -> None:
        idx = counts.get(regime, 0) + 1
        counts[regime] = idx
        periods.append(
            {
                "label": f"{regime[:2]}{idx}",
                "regime": regime,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "bars": bar_count,
            }
        )

    for row in ordered[1:]:
        if row.regime == current:
            end = row.date
            bars += 1
            continue
        flush(current, start, end, bars)
        current = row.regime
        start = row.date
        end = row.date
        bars = 1

    flush(current, start, end, bars)
    return periods


def _pipeline_log_message(row: PipelineLog) -> str:
    details = row.details or {}
    summary = str(details.get("summary") or "").strip()
    if summary:
        return summary
    if row.stage == "active-sync":
        return (
            f"active-sync active={details.get('active_count', 0)} "
            f"+{len(details.get('added') or [])} "
            f"~{len(details.get('changed') or [])} "
            f"-{len(details.get('retired') or [])}"
        )
    if row.stage.startswith("scheduler:"):
        duration = details.get("duration_s")
        suffix = f" in {duration}s" if duration is not None else ""
        return f"{row.stage.split(':', 1)[1]} {row.outcome}{suffix}"
    return f"{row.stage} {row.outcome}"


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


@app.get("/api/regime-periods")
def regime_periods(market: str = Query(..., min_length=1, max_length=16)) -> dict[str, Any]:
    m = market.strip().lower()
    db = SessionLocal()
    try:
        rows = db.query(RegimeLabelModel).filter_by(market=m).all()
        return {
            "market": market.strip().upper(),
            "regime_periods": _regime_periods(rows),
        }
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


@app.get("/api/logs")
def pipeline_logs(
    market: str = Query(..., min_length=1, max_length=16),
    limit: int = Query(15, ge=1, le=15),
    include_global: bool = True,
) -> list[dict]:
    """Recent compact pipeline/scheduler logs for a market."""
    m = market.strip().lower()
    db = SessionLocal()
    try:
        q = db.query(PipelineLog)
        if include_global:
            q = q.filter((PipelineLog.market == m) | (PipelineLog.market == "all"))
        else:
            q = q.filter(PipelineLog.market == m)
        rows = q.order_by(PipelineLog.logged_at.desc()).limit(limit).all()
        return [
            {
                "logged_at": str(r.logged_at),
                "market": r.market,
                "stage": r.stage,
                "outcome": r.outcome,
                "message": _pipeline_log_message(r),
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
        closed_positions = (
            db.query(PaperPositionModel)
            .filter_by(market=m, status="closed")
            .all()
        )

        total_pnl  = sum(t.pnl or 0.0 for t in all_trades)
        equity     = _PAPER_CAPITAL + total_pnl
        wins       = [p for p in closed_positions if (p.pnl or 0.0) > 0]
        win_rate   = len(wins) / len(closed_positions) if closed_positions else 0.0

        latest_close_map = {
            p.symbol: _latest_close_for_symbol(m, p.symbol)
            for p in open_pos
        }
        open_pnls: list[float] = []
        open_positions_payload: list[dict[str, Any]] = []
        for p in open_pos:
            latest_close = latest_close_map.get(p.symbol)
            unrealized_pnl = None
            if latest_close is not None:
                unrealized_pnl = round((latest_close - p.entry_price) * p.remaining_shares, 2)
                open_pnls.append(unrealized_pnl)
            open_positions_payload.append(
                {
                    "id":               p.id,
                    "symbol":           p.symbol,
                    "strategy":         p.strategy,
                    "regime":           p.regime,
                    "entry_date":       str(p.entry_date),
                    "entry_price":      p.entry_price,
                    "latest_close":     latest_close,
                    "sl_current":       p.sl_current,
                    "tp1_price":        p.tp1_price,
                    "tp2_price":        p.tp2_price,
                    "shares":           p.shares,
                    "remaining_shares": p.remaining_shares,
                    "bars_held":        p.bars_held,
                    "tp1_hit":          p.tp1_hit,
                    "unrealized_pnl":   unrealized_pnl,
                }
            )
        open_pnl_total = round(sum(open_pnls), 2) if len(open_pnls) == len(open_pos) else None

        return {
            "summary": {
                "equity":                round(equity, 2),
                "equity_including_open": round(equity + open_pnl_total, 2) if open_pnl_total is not None else None,
                "initial_capital":       _PAPER_CAPITAL,
                "total_pnl":             round(total_pnl, 2),
                "total_pnl_pct":         round(total_pnl / _PAPER_CAPITAL * 100, 2),
                "open_pnl":              open_pnl_total,
                "open_count":            len(open_pos),
                "closed_trades":         len(closed_positions),
                "win_rate":              round(win_rate, 4),
            },
            "open_positions": open_positions_payload,
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


_SCAN_MARKETS = {"th", "us", "au", "crypto", "commodity"}


@app.post("/api/scan/run")
async def trigger_scan(market: str = Query(..., min_length=1, max_length=16)) -> dict[str, Any]:
    """Manually trigger a scan + paper-update for one market."""
    import asyncio
    from scripts.cron_daily import run_markets

    m = market.strip().lower()
    if m not in _SCAN_MARKETS:
        raise HTTPException(status_code=400, detail=f"Unknown market: {m}")

    loop = asyncio.get_event_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: run_markets([m])),
            timeout=120.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Scan timed out after 120s")
    except Exception as exc:
        import traceback
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}")

    db = SessionLocal()
    try:
        log = (
            db.query(PipelineLog)
            .filter_by(market=m, stage="scan")
            .order_by(PipelineLog.id.desc())
            .first()
        )
        return {
            "market": m,
            "outcome": log.outcome if log else "unknown",
            "details": log.details if log else {},
        }
    finally:
        db.close()


if DOCS_DIR.exists():
    app.mount("/", StaticFiles(directory=DOCS_DIR, html=True), name="docs")
