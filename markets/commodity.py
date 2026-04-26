from __future__ import annotations
from datetime import date
import pandas as pd
import yfinance as yf
from markets.base import MarketAdapter
from core.tx_cost import TX_COSTS
from core.universe import get_universe


class CommodityAdapter(MarketAdapter):
    market_id = "commodity"
    benchmark = "GC=F"
    currency = "USD"
    min_adv = 0
    lot_size = 1

    def universe(self, as_of: date, top_n: int = 50) -> list[str]:
        return get_universe("commodity", as_of, top_n=top_n)

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

    def tx_costs(self, symbol: str) -> dict:
        c = TX_COSTS["commodity"]
        return {
            "commission_bps": c.commission_bps,
            "spread_bps": c.spread_bps,
            "slippage_bps": c.slippage_bps,
        }
