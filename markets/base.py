from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import date
import pandas as pd


class MarketAdapter(ABC):
    market_id: str
    benchmark: str
    currency: str
    min_adv: float
    lot_size: int

    @abstractmethod
    def universe(self, as_of: date, top_n: int = 50) -> list[str]:
        """Point-in-time universe — no survivorship bias."""

    @abstractmethod
    def ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Return OHLCV DataFrame with lowercase columns: open high low close volume."""

    @abstractmethod
    def tx_costs(self, symbol: str) -> dict:
        """Return {commission_bps, spread_bps, slippage_bps}."""

    def benchmark_ohlcv(self, start: date, end: date) -> pd.DataFrame:
        return self.ohlcv(self.benchmark, start, end)

    def rsm(
        self,
        df: pd.DataFrame,
        benchmark_df: pd.DataFrame,
        period: int = 63,
    ) -> float:
        """Relative strength vs benchmark over period bars."""
        if len(df) < period or len(benchmark_df) < period:
            return 0.0
        stock_ret = float(df["close"].pct_change(period).iloc[-1])
        bench_ret = float(benchmark_df["close"].pct_change(period).iloc[-1])
        return stock_ret - bench_ret
