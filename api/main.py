from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import yfinance as yf
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
SNAPSHOT_PATH = DOCS_DIR / "chart_data.json"
CACHE_DIR = Path(os.getenv("OHLCV_CACHE_DIR", ROOT / ".cache" / "ohlcv"))
CACHE_TTL_SECONDS = int(os.getenv("OHLCV_CACHE_TTL_SECONDS", "43200"))
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.^=_-]{1,32}$")

app = FastAPI(title="Alpha Engine API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_methods=["GET"],
    allow_headers=["*"],
)


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


if DOCS_DIR.exists():
    app.mount("/", StaticFiles(directory=DOCS_DIR, html=True), name="docs")
