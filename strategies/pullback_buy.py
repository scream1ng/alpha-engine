from __future__ import annotations
import pandas as pd
from strategies.base import Strategy
from core.registry import StrategyRegistry
from core.signal import Signal
from core.indicators import atr, rvol, candle_body_pct, close_position_in_range


@StrategyRegistry.register
class PullbackBuy(Strategy):
    id = "pullback_buy"
    default_params = {
        "lookback": 10,
        "pullback_atr_band": 0.5,
        "rvol_max_on_pullback": 1.2,
        "body_pct_min": 0.4,
        "close_position_min": 0.6,
        "sl_atr_mult": 1.5,
        "tp1_atr_mult": 2.0,
        "tp2_atr_mult": 3.0,
        "risk_pct": 0.005,
        "max_bars": 0,
        "trail_atr_mult": 1.5,
        "be_trigger_atr_mult": 1.0,
        "rsm_min": 0,
    }

    def scan(self, df: pd.DataFrame, params: dict) -> list[Signal]:
        p = {**self.default_params, **params}
        lookback = p["lookback"]
        if len(df) < lookback + 10:
            return []

        _atr = df["_atr"] if "_atr" in df.columns else atr(df)
        _rvol = df["_rvol"] if "_rvol" in df.columns else rvol(df)
        _body = df["_body_pct"] if "_body_pct" in df.columns else candle_body_pct(df)
        _cpos = df["_close_pos"] if "_close_pos" in df.columns else close_position_in_range(df)
        bar = df.iloc[-1]
        atr_val = float(_atr.iloc[-1])
        if atr_val == 0:
            return []

        # Find the most recent pivot break in the prior lookback window
        window = df.iloc[-lookback - 1:-1]
        if window.empty:
            return []
        if not self._in_uptrend(df, p):
            return []
        if not self._rsm_ok(df, p):
            return []
        pivot_high = window["high"].max()
        pivot_date_idx = window["high"].idxmax()
        pivot_bar = window.loc[pivot_date_idx]

        # Current bar is a pullback to the breakpoint ± pullback_atr_band * ATR
        breakpoint = float(pivot_bar["high"])
        band = p["pullback_atr_band"] * atr_val
        if not (breakpoint - band <= bar["close"] <= breakpoint + band):
            return []

        # Reversal candle quality
        if _body.iloc[-1] < p["body_pct_min"]:
            return []
        if _cpos.iloc[-1] < p["close_position_min"]:
            return []

        # Volume must be quiet on pullback (healthy retrace)
        if _rvol.iloc[-1] > p["rvol_max_on_pullback"]:
            return []

        sig = self._build_signal(
            df=df,
            params=p,
            entry=float(bar["close"]),
            entry_type="market_close",
            atr_val=atr_val,
            meta={
                "breakpoint": breakpoint,
                "rvol": float(_rvol.iloc[-1]),
            },
        )
        if sig.rr < 1.0:
            return []
        return [sig]

    def param_space(self) -> dict:
        return {
            "lookback":              [5, 10],
            "pullback_atr_band":     [0.3, 0.5],
            "rvol_max_on_pullback":  [1.0, 1.5],
            "body_pct_min":          [0.3, 0.5],
            "close_position_min":    [0.5, 0.7],
            "sl_atr_mult":           [1.0, 1.5],
            "tp1_atr_mult":          [1.0, 1.5, 2.0, 2.5, 3.0],
            "tp2_atr_mult":          [3.0, 3.5, 4.0, 4.5, 5.0],
            "risk_pct":              [0.003, 0.005],
            "trail_atr_mult":        [1.0, 1.5],
            "be_trigger_atr_mult":   [0.5, 1.0],
            "ema_exit_period":       [0, 5, 10],
            "trend_filter":          [0, 50, 100, 200, "50_100", "50_200", "100_200"],
            "tp1_partial_pct":       [0.2, 0.3, 0.4, 0.5],
            "tp2_partial_pct":       [0.2, 0.3, 0.4, 0.5],
            "rsm_min":               [0, 75, 80],
        }
