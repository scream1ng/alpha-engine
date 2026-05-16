from __future__ import annotations
from datetime import date
import pandas as pd
import yfinance as yf
from markets.base import MarketAdapter
from core.tx_cost import TX_COSTS
from core.universe import get_universe


class THAdapter(MarketAdapter):
    market_id = "th"
    benchmark = "^SET.BK"
    currency = "THB"
    min_adv = 500_000
    lot_size = 100

    def universe(self, as_of: date, top_n: int | None = None) -> list[str]:
        return get_universe("th", as_of, top_n=top_n)

    def ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=str(start), end=str(end), auto_adjust=True)
        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df[["open", "high", "low", "close", "volume"]].dropna()

    def ohlcv_bulk(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {}
        chunk_size = 200
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i : i + chunk_size]
            try:
                raw = yf.download(
                    chunk, start=str(start), end=str(end),
                    auto_adjust=True, group_by="ticker", progress=False,
                )
                for sym in chunk:
                    try:
                        df = raw[sym]
                        df = df.rename(columns={
                            "Open": "open", "High": "high", "Low": "low",
                            "Close": "close", "Volume": "volume",
                        })
                        idx = pd.to_datetime(df.index)
                        df.index = idx.tz_localize(None) if idx.tz is not None else idx
                        result[sym] = df[["open", "high", "low", "close", "volume"]].dropna()
                    except Exception:
                        result[sym] = self.ohlcv(sym, start, end)
            except Exception:
                for sym in chunk:
                    result[sym] = self.ohlcv(sym, start, end)
        return result

    def ohlcv_intraday(self, symbol: str, day: date, interval: str = "15m") -> pd.DataFrame:
        from datetime import timedelta
        ticker = yf.Ticker(symbol)
        df = ticker.history(
            start=str(day),
            end=str(day + timedelta(days=1)),
            interval=interval,
            auto_adjust=True,
        )
        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })
        if df.index.tz is not None:
            df.index = df.index.tz_convert("Asia/Bangkok").tz_localize(None)
        else:
            df.index = pd.to_datetime(df.index)
        return df[["open", "high", "low", "close", "volume"]].dropna()

    def tx_costs(self, symbol: str) -> dict:
        c = TX_COSTS["th"]
        return {
            "commission_bps": c.commission_bps,
            "spread_bps": c.spread_bps,
            "slippage_bps": c.slippage_bps,
        }
