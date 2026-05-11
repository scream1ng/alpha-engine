from __future__ import annotations
import pandas as pd
from strategies.base import Strategy
from core.registry import StrategyRegistry
from core.signal import Signal
from core.indicators import atr, rvol
from core.tick import set_tick


@StrategyRegistry.register
class PivotBreakout(Strategy):
    id = "pivot_breakout"
    default_params = {
        "psth": 0.005,
        "rvol_min": 1.1,
        "lookback": 10,
        "sl_atr_mult": 1.5,
        "tp1_atr_mult": 3.0,
        "tp2_atr_mult": 4.5,
        "risk_pct": 0.005,
        "max_bars": 0,
        "trail_atr_mult": 2.0,
        "be_trigger_atr_mult": 1.0,
        "rsm_min": 0,
        "str_max": 0,
    }

    def scan(self, df: pd.DataFrame, params: dict) -> list[Signal]:
        p = {**self.default_params, **params}
        lookback = p["lookback"]
        if len(df) < lookback + 5:
            return []

        _atr = df["_atr"] if "_atr" in df.columns else atr(df)
        _rvol = df["_rvol"] if "_rvol" in df.columns else rvol(df)
        bar = df.iloc[-1]
        i = len(df) - 1

        # Break above the highest close of the lookback window (not high)
        prev_high = df["close"].iloc[max(0, i - lookback):i].max()

        if not self._in_uptrend(df, p):
            return []
        if not self._rsm_ok(df, p):
            return []
        if not self._stretch_ok(df, p):
            return []
        if bar["close"] <= prev_high:
            return []
        if (bar["close"] - prev_high) / prev_high < p["psth"]:
            return []
        if _rvol.iloc[-1] < p["rvol_min"]:
            return []
        if float(_atr.iloc[-1]) == 0:
            return []

        entry_mode = p.get("entry_mode", "market_close")
        if entry_mode in ("limit_intraday", "limit_fakeout", "intraday_breakout"):
            entry = round(float(prev_high) + set_tick(float(prev_high)), 6)
            etype = entry_mode if entry_mode != "intraday_breakout" else "market_close"
        else:
            entry = float(bar["close"])
            etype = "market_close"

        sig = self._build_signal(
            df=df,
            params=p,
            entry=entry,
            entry_type=etype,
            atr_val=float(_atr.iloc[-1]),
            meta={"prev_high": float(prev_high), "fakeout_level": float(prev_high),
                  "rvol": float(_rvol.iloc[-1])},
        )
        if entry_mode == "limit_fakeout":
            sig.exit_policies = ["fakeout", "hard_exit"]
        if sig.rr < 1.0:
            return []
        return [sig]

    def param_space(self) -> dict:
        return {
            "psth":                [0.005, 0.01, 0.02],
            "rvol_min":            [1.5, 2.0],
            "lookback":            [5, 10],
            "sl_atr_mult":         [1.0, 1.5, 2.0],
            "tp1_atr_mult":        [1.0, 1.5, 2.0, 2.5, 3.0],
            "tp2_atr_mult":        [3.0, 3.5, 4.0, 4.5, 5.0],
            "risk_pct":            [0.003, 0.005],
            "trail_atr_mult":      [1.5, 2.0],
            "be_trigger_atr_mult": [0.5, 1.0],
            "ema_exit_period":     [0, 5, 10],
            "trend_filter":        [0, 50, 100, 200, "50_100", "50_200", "100_200"],
            "tp1_partial_pct":     [0.2, 0.3, 0.4, 0.5],
            "tp2_partial_pct":     [0.2, 0.3, 0.4, 0.5],
            "rsm_min":             [0, 75, 80],
            "str_max":             [0, 3, 4, 5],
        }
