from __future__ import annotations
import pandas as pd
from strategies.base import Strategy
from core.registry import StrategyRegistry
from core.signal import Signal
from core.indicators import atr, bollinger_bands, keltner_channel, momentum_histogram


@StrategyRegistry.register
class BBSqueeze(Strategy):
    id = "bb_squeeze"
    default_params = {
        "bb_period": 20,
        "bb_std": 2.0,
        "kc_period": 20,
        "kc_mult": 2.0,
        "sl_atr_mult": 1.5,
        "tp1_atr_mult": 3.0,
        "tp2_atr_mult": 4.5,
        "risk_pct": 0.005,
        "max_bars": 15,
        "trail_atr_mult": 2.0,
        "be_trigger_atr_mult": 1.0,
        "rsm_min": 0,
    }

    def scan(self, df: pd.DataFrame, params: dict) -> list[Signal]:
        p = {**self.default_params, **params}
        period = max(p["bb_period"], p["kc_period"])
        if len(df) < period + 5:
            return []
        if not self._rsm_ok(df, p):
            return []

        _atr = df["_atr"] if "_atr" in df.columns else atr(df)
        atr_val = float(_atr.iloc[-1])
        if atr_val == 0:
            return []

        bb_upper, bb_mid, bb_lower = bollinger_bands(df, p["bb_period"], p["bb_std"])
        kc_upper, kc_mid, kc_lower = keltner_channel(df, p["kc_period"], p["kc_mult"])
        momentum = df["_momentum"] if "_momentum" in df.columns else momentum_histogram(df)

        # Squeeze: previous bar had BB inside KC
        prev_squeeze = (
            float(bb_upper.iloc[-2]) < float(kc_upper.iloc[-2])
            and float(bb_lower.iloc[-2]) > float(kc_lower.iloc[-2])
        )
        # Release: current bar BB outside KC
        cur_release = (
            float(bb_upper.iloc[-1]) >= float(kc_upper.iloc[-1])
            or float(bb_lower.iloc[-1]) <= float(kc_lower.iloc[-1])
        )
        if not (prev_squeeze and cur_release):
            return []
        if not self._in_uptrend(df, p):
            return []

        # Momentum turning up on release bar (increasing, even if still negative)
        if float(momentum.iloc[-1]) <= float(momentum.iloc[-2]):
            return []

        bar = df.iloc[-1]
        sig = self._build_signal(
            df=df,
            params=p,
            entry=float(bar["close"]),
            entry_type="market_close",
            atr_val=atr_val,
            meta={
                "bb_upper": float(bb_upper.iloc[-1]),
                "bb_mid": float(bb_mid.iloc[-1]),
                "momentum": float(momentum.iloc[-1]),
            },
        )
        if sig.rr < 1.0:
            return []
        return [sig]

    def param_space(self) -> dict:
        return {
            "bb_period":           [15, 20],
            "bb_std":              [1.5, 2.0],
            "kc_period":           [15, 20],
            "kc_mult":             [1.5, 2.0, 2.5],
            "sl_atr_mult":         [1.0, 1.5, 2.0],
            "tp1_atr_mult":        [1.0, 1.5, 2.0, 2.5, 3.0],
            "tp2_atr_mult":        [3.0, 3.5, 4.0, 4.5, 5.0],
            "risk_pct":            [0.003, 0.005],
            "max_bars":            [10, 15],
            "trail_atr_mult":      [1.5, 2.0],
            "be_trigger_atr_mult": [0.5, 1.0],
            "ema_exit_period":     [0, 5, 10],
            "trend_sma_period":    [0, 50, 100, 200],
            "tp1_partial_pct":     [0.2, 0.3, 0.4, 0.5],
            "tp2_partial_pct":     [0.2, 0.3, 0.4, 0.5],
            "rsm_min":             [0, 70, 75, 80],
        }
