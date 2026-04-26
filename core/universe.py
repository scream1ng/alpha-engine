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

# Only fetch common stocks — excludes DRs, ETFs, warrants, preferred shares
_COMMON_STOCK_FILTER = {"left": "typespecs", "operation": "has", "right": ["common"]}

# In-process cache: {(market_id, top_n): (fetched_at, symbols)}
_cache: dict[tuple[str, int], tuple[datetime, list[str]]] = {}
_CACHE_TTL_HOURS = 24


def _fetch_tradingview(market_id: str, top_n: int) -> list[str]:
    import requests

    url, suffix, exchanges = _TV_MARKET_MAP[market_id]
    payload: dict[str, Any] = {
        "columns": ["name", "market_cap_basic"],
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, top_n],
    }
    filters = [_COMMON_STOCK_FILTER]
    if exchanges:
        filters.append({"left": "exchange", "operation": "in_range", "right": exchanges})
    payload["filter"] = filters

    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    symbols = []
    for row in data.get("data", []):
        name = row["d"][0]
        # Exclude futures/warrants that slip through (e.g. ADVANC.F.BK on TFEX)
        if ".F" in name or "-W" in name:
            continue
        symbols.append(name + suffix)
    return symbols


def _fallback_stubs(market_id: str) -> list[str]:
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
    return list(stubs.get(market_id, []))


def get_universe(market_id: str, as_of: date, top_n: int = 50) -> list[str]:
    """
    Return top_n symbols by market cap for market_id.
    Fetches from TradingView screener for th/us/au; falls back to stubs
    on error or for unsupported markets (crypto, commodity).
    Results are cached for 24 hours within the process.
    """
    if market_id not in _TV_MARKET_MAP:
        return _fallback_stubs(market_id)

    cache_key = (market_id, top_n)
    cached = _cache.get(cache_key)
    if cached:
        fetched_at, symbols = cached
        if datetime.now(timezone.utc) - fetched_at < timedelta(hours=_CACHE_TTL_HOURS):
            logger.debug("universe cache hit: %s top_%d", market_id, top_n)
            return symbols

    try:
        symbols = _fetch_tradingview(market_id, top_n)
        if not symbols:
            raise ValueError("empty response")
        _cache[cache_key] = (datetime.now(timezone.utc), symbols)
        logger.info("universe: fetched %d symbols for %s from TradingView", len(symbols), market_id)
        return symbols
    except Exception as exc:
        logger.warning("universe: TradingView fetch failed (%s), using stubs", exc)
        return _fallback_stubs(market_id)
