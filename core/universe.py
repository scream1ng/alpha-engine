from __future__ import annotations
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_TV_MARKET_MAP = {
    "th": ("https://scanner.tradingview.com/thailand/scan", ".BK", ["SET"]),
    "us": ("https://scanner.tradingview.com/america/scan", "", ["NYSE", "NASDAQ"]),
    "au": ("https://scanner.tradingview.com/australia/scan", ".AX", ["ASX"]),
}

_STOCK_TYPE_FILTER = {"left": "type", "operation": "equal", "right": "stock"}
_MIN_TURNOVER = {
    "th": 10_000_000,
    "us": 20_000_000,
    "au": 1_000_000,
}
_TV_FETCH_CAP = {
    "th": 3000,
    "us": 3000,
    "au": 2000,
}

# In-process cache: {(market_id, top_n): (fetched_at, symbols)}
_cache: dict[tuple[str, int | None], tuple[datetime, list[str]]] = {}
_CACHE_TTL_HOURS = 24


def _fetch_range_end(market_id: str, top_n: int | None) -> int:
    fetch_cap = _TV_FETCH_CAP.get(market_id, 300)
    if top_n is None:
        return fetch_cap
    return min(max(top_n * 20, 300), fetch_cap)


def _fetch_tradingview(market_id: str, top_n: int | None) -> list[str]:
    import requests

    url, suffix, exchanges = _TV_MARKET_MAP[market_id]
    fetch_range_end = _fetch_range_end(market_id, top_n)
    min_turnover = _MIN_TURNOVER.get(market_id, 0)
    payload: dict[str, Any] = {
        "columns": ["name", "market_cap_basic", "close", "average_volume_10d_calc"],
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, fetch_range_end],
    }
    filters = [_STOCK_TYPE_FILTER]
    if exchanges:
        filters.append({"left": "exchange", "operation": "in_range", "right": exchanges})
    payload["filter"] = filters

    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    seen: set[str] = set()
    symbols: list[str] = []
    for row in data.get("data", []):
        name, _market_cap, close, avg_vol = row["d"]
        # Exclude foreign-board duplicates, rights, and warrants that slip through.
        if any(token in name for token in (".F", ".R", "-W", "-R")):
            continue
        turnover = float(close or 0) * float(avg_vol or 0)
        if turnover <= min_turnover:
            continue
        symbol = name + suffix
        if symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
        if top_n is not None and len(symbols) >= top_n:
            break
    return symbols


def _fallback_stubs(market_id: str, top_n: int | None = None) -> list[str]:
    stubs: dict[str, list[str]] = {
        "th": [
            "PTT.BK", "ADVANC.BK", "AOT.BK", "CPALL.BK", "BDMS.BK",
            "SCB.BK", "KBANK.BK", "BBL.BK", "SCC.BK", "DELTA.BK",
            "PTTEP.BK", "TRUE.BK", "INTUCH.BK", "CPN.BK", "TU.BK",
            "MINT.BK", "HMPRO.BK", "BJC.BK", "BEM.BK", "GULF.BK",
        ],
        "us": [
            "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
            "META", "TSLA", "BRK-B", "UNH", "JNJ",
            "V", "PG", "MA", "HD", "CVX",
        ],
        "au": [
            "BHP.AX", "CBA.AX", "NAB.AX", "ANZ.AX", "WBC.AX",
            "CSL.AX", "WES.AX", "MQG.AX", "TLS.AX", "RIO.AX",
        ],
        "crypto": [
            "BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD", "ADA-USD",
            "XRP-USD", "DOGE-USD", "DOT-USD", "AVAX-USD", "MATIC-USD",
        ],
        "commodity": ["GC=F", "CL=F", "SI=F", "NG=F", "HG=F"],
    }
    symbols = list(stubs.get(market_id, []))
    return symbols if top_n is None else symbols[:top_n]


def get_universe(market_id: str, as_of: date, top_n: int | None = None) -> list[str]:
    """
    Return all symbols above the market turnover threshold for market_id.
    If top_n is provided, trim the result to that many symbols.
    Fetches from TradingView screener for th/us/au; falls back to stubs
    on error or for unsupported markets (crypto, commodity).
    Results are cached for 24 hours within the process.
    """
    if market_id not in _TV_MARKET_MAP:
        return _fallback_stubs(market_id, top_n=top_n)

    cache_key = (market_id, top_n)
    cached = _cache.get(cache_key)
    if cached:
        fetched_at, symbols = cached
        if datetime.now(timezone.utc) - fetched_at < timedelta(hours=_CACHE_TTL_HOURS):
            logger.debug("universe cache hit: %s top_%s", market_id, top_n if top_n is not None else "all")
            return symbols

    try:
        symbols = _fetch_tradingview(market_id, top_n)
        if not symbols:
            raise ValueError("empty response")
        _cache[cache_key] = (datetime.now(timezone.utc), symbols)
        logger.info(
            "universe: fetched %d valid symbols for %s from TradingView (requested=%s min_turnover=%s)",
            len(symbols), market_id, top_n if top_n is not None else "all", f"{_MIN_TURNOVER.get(market_id, 0):,.0f}",
        )
        return symbols
    except Exception as exc:
        logger.warning("universe: TradingView fetch failed (%s), using stubs", exc)
        return _fallback_stubs(market_id, top_n=top_n)
