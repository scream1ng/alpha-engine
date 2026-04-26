from __future__ import annotations
import pandas as pd
from strategies.base import Strategy
from core.registry import StrategyRegistry
from core.signal import Signal
from core.indicators import atr, ema, sma, rvol


@StrategyRegistry.register
class MACross(Strategy):
    id = "ma_cross"
    default_params = {
        "fast_period": 20,
        "slow_period": 50,
        "trend_period": 100,
        "rvol_min": 1.2,
        "sl_atr_mult": 1.5,
        "tp1_atr_mult": 3.0,
        "tp2_atr_mult": 4.5,
        "risk_pct": 0.005,
        "max_bars": 20,
        "trail_atr_mult": 2.0,
        "be_trigger_atr_mult": 1.0,
        "rsm_min": 0,
    }

    def scan(self, df: pd.DataFrame, params: dict) -> list[Signal]:
        p = {**self.default_params, **params}
        if len(df) < p["trend_period"] + 5:
            return []
        if not self._rsm_ok(df, p):
            return []

        _atr = df["_atr"] if "_atr" in df.columns else atr(df)
        _rvol = df["_rvol"] if "_rvol" in df.columns else rvol(df)
        fast_ema = ema(df, p["fast_period"])
        slow_ema = ema(df, p["slow_period"])
        trend_sma = sma(df, p["trend_period"])

        atr_val = float(_atr.iloc[-1])
        if atr_val == 0:
            return []

        # Fresh EMA cross: fast crossed above slow within last 3 bars
        crossed_up = any(
            float(fast_ema.iloc[-k]) > float(slow_ema.iloc[-k])
            and float(fast_ema.iloc[-k - 1]) <= float(slow_ema.iloc[-k - 1])
            for k in range(1, 4)
        )
        if not crossed_up:
            return []

        # Long-term trend filter
        if float(df["close"].iloc[-1]) < float(trend_sma.iloc[-1]):
            return []

        # Volume confirmation on cross bar
        if float(_rvol.iloc[-1]) < p["rvol_min"]:
            return []

        bar = df.iloc[-1]
        # Entry on next open — use today's close as proxy for signal generation
        sig = self._build_signal(
            df=df,
            params=p,
            entry=float(bar["close"]),
            entry_type="market_close",
            atr_val=atr_val,
            meta={
                "fast_ema": float(fast_ema.iloc[-1]),
                "slow_ema": float(slow_ema.iloc[-1]),
                "rvol": float(_rvol.iloc[-1]),
            },
        )
        if sig.rr < 1.0:
            return []
        return [sig]

    def param_space(self) -> dict:
        return {
            "fast_period":         [10, 20],
            "slow_period":         [40, 60],
            "trend_period":        [100, 150, 200],
            "rvol_min":            [1.0, 1.2, 1.5],
            "sl_atr_mult":         [1.0, 1.5, 2.0],
            "tp1_atr_mult":        [1.0, 1.5, 2.0, 2.5, 3.0],
            "tp2_atr_mult":        [3.0, 3.5, 4.0, 4.5, 5.0],
            "risk_pct":            [0.003, 0.005],
            "max_bars":            [15, 25],
            "trail_atr_mult":      [1.5, 2.0],
            "be_trigger_atr_mult": [0.5, 1.0],
            "ema_exit_period":     [0, 5, 10],
            "tp1_partial_pct":     [0.2, 0.3, 0.4, 0.5],
            "tp2_partial_pct":     [0.2, 0.3, 0.4, 0.5],
            "rsm_min":             [0, 70, 75, 80],
        }
