from __future__ import annotations
import pandas as pd
from strategies.base import Strategy
from core.registry import StrategyRegistry
from core.signal import Signal
from core.indicators import atr, rvol


@StrategyRegistry.register
class NarrowRange(Strategy):
    """NR7 strategy — sets a pending stop order above the narrow range bar."""
    id = "narrow_range"
    default_params = {
        "nr_period": 7,
        "atr_pct_max": 0.025,
        "rvol_min": 1.5,
        "sl_atr_mult": 1.5,
        "tp1_atr_mult": 2.0,
        "tp2_atr_mult": 3.0,
        "risk_pct": 0.005,
        "max_bars": 10,
        "trail_atr_mult": 1.5,
        "be_trigger_atr_mult": 0.75,
        "rsm_min": 0,
    }

    def scan(self, df: pd.DataFrame, params: dict) -> list[Signal]:
        p = {**self.default_params, **params}
        nr = p["nr_period"]
        if len(df) < nr + 5:
            return []

        _atr = df["_atr"] if "_atr" in df.columns else atr(df)
        _rvol = df["_rvol"] if "_rvol" in df.columns else rvol(df)
        bar = df.iloc[-1]
        atr_val = float(_atr.iloc[-1])
        if atr_val == 0:
            return []

        if not self._in_uptrend(df, p):
            return []
        if not self._rsm_ok(df, p):
            return []
        if p["rvol_min"] > 0 and float(_rvol.iloc[-1]) < p["rvol_min"]:
            return []

        # NR7: today's range is the narrowest of the last nr_period bars
        today_range = bar["high"] - bar["low"]
        past_ranges = (df["high"] - df["low"]).iloc[-nr:]
        if today_range != past_ranges.min():
            return []

        # ATR% filter: ensure compression is real, not just a low-vol symbol
        atr_pct = atr_val / bar["close"]
        if atr_pct > p["atr_pct_max"]:
            return []

        # Entry: pending stop 1 tick above NR7 high
        tick = bar["close"] * 0.001
        entry = float(bar["high"]) + tick

        # Build signal with pending_stop entry type
        from core.tx_cost import cost_adjust_rr
        sl_price = float(bar["low"])
        tp1_price = entry + p["tp1_atr_mult"] * atr_val
        tp2_price = entry + p["tp2_atr_mult"] * atr_val
        rr = cost_adjust_rr(entry, sl_price, tp1_price, df.attrs.get("market", ""))

        if rr < 1.0:
            return []

        from core.signal import Signal
        return [Signal(
            symbol=df.attrs.get("symbol", ""),
            market=df.attrs.get("market", ""),
            strategy=self.id,
            direction="long",
            entry=entry,
            entry_type="pending_stop",
            sl=sl_price,
            tp1=tp1_price,
            tp2=tp2_price,
            tp3=None,
            atr=atr_val,
            rr=rr,
            score=50.0,
            meta={"nr7_high": float(bar["high"]), "nr7_low": float(bar["low"])},
            sl_atr_mult=p["sl_atr_mult"],
            tp1_atr_mult=p["tp1_atr_mult"],
            tp2_atr_mult=p["tp2_atr_mult"],
            risk_pct=p["risk_pct"],
            max_bars=p["max_bars"],
            trail_atr_mult=p["trail_atr_mult"],
            be_trigger_atr_mult=p["be_trigger_atr_mult"],
            generated_at=df.index[-1].date() if hasattr(df.index[-1], "date") else None,
        )]

    def param_space(self) -> dict:
        return {
            "nr_period":           [5, 7],
            "atr_pct_max":         [0.015, 0.025, 0.035],
            "rvol_min":            [1.5, 2.0],
            "sl_atr_mult":         [1.0, 1.5],
            "tp1_atr_mult":        [1.0, 1.5, 2.0, 2.5, 3.0],
            "tp2_atr_mult":        [3.0, 3.5, 4.0, 4.5, 5.0],
            "risk_pct":            [0.003, 0.005],
            "max_bars":            [5, 10],
            "trail_atr_mult":      [1.0, 1.5],
            "be_trigger_atr_mult": [0.5, 1.0],
            "ema_exit_period":     [0, 5, 10],
            "trend_sma_period":    [0, 50, 100, 200],
            "tp1_partial_pct":     [0.2, 0.3, 0.4, 0.5],
            "tp2_partial_pct":     [0.2, 0.3, 0.4, 0.5],
            "rsm_min":             [0, 70, 75, 80],
        }
