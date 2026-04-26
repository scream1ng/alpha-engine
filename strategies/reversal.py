from __future__ import annotations
import pandas as pd
from strategies.base import Strategy
from core.registry import StrategyRegistry
from core.signal import Signal
from core.indicators import atr, rsi, rvol


@StrategyRegistry.register
class Reversal(Strategy):
    id = "reversal"
    default_params = {
        "rsi_threshold": 35,
        "consec_down_days": 2,
        "rvol_min": 1.2,
        "support_lookback": 60,
        "sl_atr_mult": 1.0,
        "tp1_atr_mult": 1.5,
        "tp2_atr_mult": 2.5,
        "risk_pct": 0.003,
        "max_bars": 8,
        "trail_atr_mult": 1.5,
        "be_trigger_atr_mult": 0.75,
    }

    def scan(self, df: pd.DataFrame, params: dict) -> list[Signal]:
        p = {**self.default_params, **params}
        if len(df) < 30:
            return []

        _atr = df["_atr"] if "_atr" in df.columns else atr(df)
        _rsi = df["_rsi"] if "_rsi" in df.columns else rsi(df)
        _rvol = df["_rvol"] if "_rvol" in df.columns else rvol(df)
        bar = df.iloc[-1]
        atr_val = float(_atr.iloc[-1])
        if atr_val == 0:
            return []

        # Condition 1: oversold RSI OR N consecutive down days
        rsi_ok = float(_rsi.iloc[-1]) < p["rsi_threshold"]
        closes = df["close"].iloc[-p["consec_down_days"] - 1:]
        consec_ok = all(
            closes.iloc[i] < closes.iloc[i - 1]
            for i in range(1, len(closes))
        )
        if not rsi_ok and not consec_ok:
            return []

        # Condition 2: bullish engulfing OR hammer
        prev_bar = df.iloc[-2]
        engulfing = (
            bar["open"] < prev_bar["close"]
            and bar["close"] > prev_bar["open"]
            and bar["close"] > bar["open"]
        )
        lower_wick = bar["open"] - bar["low"] if bar["close"] >= bar["open"] else bar["close"] - bar["low"]
        body = abs(bar["close"] - bar["open"])
        hammer = lower_wick >= 2 * body and bar["close"] > (bar["high"] + bar["low"]) / 2

        if not engulfing and not hammer:
            return []

        # Condition 3: volume spike on reversal bar
        if float(_rvol.iloc[-1]) < p["rvol_min"]:
            return []

        sig = self._build_signal(
            df=df,
            params=p,
            entry=float(bar["close"]),
            entry_type="market_close",
            atr_val=atr_val,
            meta={
                "rsi": float(_rsi.iloc[-1]),
                "pattern": "engulfing" if engulfing else "hammer",
            },
        )
        if sig.rr < 0.8:
            return []
        return [sig]

    def param_space(self) -> dict:
        return {
            "rsi_threshold":       [30, 35, 40],
            "consec_down_days":    [2, 3, 4],
            "rvol_min":            [1.0, 1.2, 1.5],
            "support_lookback":    [40, 80],
            "sl_atr_mult":         [0.75, 1.5],
            "tp1_atr_mult":        [1.0, 1.5, 2.0, 2.5, 3.0],
            "tp2_atr_mult":        [3.0, 3.5, 4.0, 4.5, 5.0],
            "risk_pct":            [0.003, 0.005],
            "max_bars":            [6, 10],
            "trail_atr_mult":      [1.0, 1.5],
            "be_trigger_atr_mult": [0.5, 1.0],
            "ema_exit_period":     [0, 5, 10],
            "tp1_partial_pct":     [0.2, 0.3, 0.4, 0.5],
            "tp2_partial_pct":     [0.2, 0.3, 0.4, 0.5],
        }
