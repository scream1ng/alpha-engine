from __future__ import annotations
import numpy as np
import pandas as pd
from strategies.base import Strategy
from core.registry import StrategyRegistry
from core.signal import Signal
from core.indicators import atr, rvol, sma


@StrategyRegistry.register
class TrendlineBreakout(Strategy):
    """
    Descending trendline fan breakout.

    1. Find the highest swing high within anchor_lookback bars (the anchor).
    2. After the anchor, collect all subsequent swing highs that are lower than anchor.
    3. Active trendline = anchor → most recent lower swing high (fan rotates down).
    4. Signal when close breaks above the projected trendline value.
    5. Invalidate entire setup if price crosses below SMA.
    """
    id = "trendline_breakout"
    default_params = {
        "anchor_lookback": 60,      # bars to look back for the main high anchor
        "swing_period": 3,          # bars each side to confirm a swing high
        "min_pivots": 1,            # need at least N lower pivots after anchor
        "rvol_min": 1.2,
        "sma_period": 50,           # invalidation SMA
        "sl_atr_mult": 1.5,
        "tp1_atr_mult": 2.0,
        "tp2_atr_mult": 3.5,
        "risk_pct": 0.005,
        "max_bars": 15,
        "trail_atr_mult": 1.5,
        "be_trigger_atr_mult": 1.0,
        "rsm_min": 0,
    }

    def scan(self, df: pd.DataFrame, params: dict) -> list[Signal]:
        p = {**self.default_params, **params}
        swing_period = int(p["swing_period"])
        anchor_lookback = int(p["anchor_lookback"])
        min_bars = anchor_lookback + swing_period * 2 + 5

        if len(df) < min_bars:
            return []

        if not self._rsm_ok(df, p):
            return []

        _atr = df["_atr"] if "_atr" in df.columns else atr(df)
        _rvol = df["_rvol"] if "_rvol" in df.columns else rvol(df)
        atr_val = float(_atr.iloc[-1])
        if atr_val == 0:
            return []

        highs = df["high"].values
        closes = df["close"].values
        n = len(df)
        current_idx = n - 1
        current_close = closes[current_idx]

        # SMA invalidation check
        sma_period = int(p["sma_period"])
        _sma = sma(df, sma_period)
        if current_close < float(_sma.iloc[-1]):
            return []

        # Find swing highs: local max with swing_period bars on each side
        swing_highs: list[tuple[int, float]] = []
        search_start = max(swing_period, current_idx - anchor_lookback - swing_period * 2)
        for i in range(search_start, current_idx - swing_period):
            left  = highs[i - swing_period:i]
            right = highs[i + 1:i + swing_period + 1]
            if len(left) < swing_period or len(right) < swing_period:
                continue
            if highs[i] > max(left) and highs[i] >= max(right):
                swing_highs.append((i, float(highs[i])))

        if len(swing_highs) < 2:
            return []

        # Anchor = highest swing high within anchor_lookback bars
        anchor_candidates = [(idx, price) for idx, price in swing_highs
                             if idx >= current_idx - anchor_lookback]
        if not anchor_candidates:
            return []
        anchor_idx, anchor_price = max(anchor_candidates, key=lambda x: x[1])

        # Subsequent lower swing highs after anchor (each below anchor)
        lower_pivots = [
            (idx, price) for idx, price in swing_highs
            if idx > anchor_idx and price < anchor_price
        ]
        if len(lower_pivots) < int(p["min_pivots"]):
            return []

        # Active trendline: anchor → most recent lower pivot
        pivot_idx, pivot_price = lower_pivots[-1]

        # Check SMA didn't cross down between anchor and now
        # (if any close between anchor and now was below SMA → invalidate)
        sma_vals = _sma.values
        for i in range(anchor_idx, current_idx):
            if closes[i] < sma_vals[i]:
                return []

        # Project trendline to current bar
        bars_span = pivot_idx - anchor_idx
        if bars_span <= 0:
            return []
        slope = (pivot_price - anchor_price) / bars_span
        projected = anchor_price + slope * (current_idx - anchor_idx)

        # Signal: current close breaks above projected trendline
        if current_close <= projected:
            return []

        # Volume confirmation
        if float(_rvol.iloc[-1]) < p["rvol_min"]:
            return []

        sig = self._build_signal(
            df=df,
            params=p,
            entry=current_close,
            entry_type="market_close",
            atr_val=atr_val,
            meta={
                "anchor_price": anchor_price,
                "anchor_bars_ago": current_idx - anchor_idx,
                "pivot_price": pivot_price,
                "pivot_bars_ago": current_idx - pivot_idx,
                "trendline_projected": round(projected, 2),
                "rvol": float(_rvol.iloc[-1]),
            },
        )
        if sig.rr < 1.0:
            return []
        return [sig]

    def param_space(self) -> dict:
        return {
            "anchor_lookback":     [40, 60, 80],
            "swing_period":        [2, 3, 4],
            "min_pivots":          [1, 2],
            "rvol_min":            [1.0, 1.2, 1.5],
            "sma_period":          [50, 100, 200],
            "sl_atr_mult":         [1.0, 1.5, 2.0],
            "tp1_atr_mult":        [1.5, 2.0, 2.5, 3.0],
            "tp2_atr_mult":        [3.0, 3.5, 4.0, 4.5, 5.0],
            "risk_pct":            [0.003, 0.005],
            "max_bars":            [10, 15, 20],
            "trail_atr_mult":      [1.5, 2.0],
            "be_trigger_atr_mult": [0.5, 1.0],
            "ema_exit_period":     [0, 5, 10],
            "tp1_partial_pct":     [0.2, 0.3, 0.4, 0.5],
            "tp2_partial_pct":     [0.2, 0.3, 0.4, 0.5],
            "rsm_min":             [0, 70, 75, 80],
        }
