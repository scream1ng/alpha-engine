from __future__ import annotations
from abc import ABC, abstractmethod
import pandas as pd
from core.signal import Signal


class Strategy(ABC):
    id: str
    default_params: dict

    def _in_uptrend(self, df: pd.DataFrame, params: dict) -> bool:
        """Return False if trend filter is active and price is below the SMA."""
        period = int(params.get("trend_sma_period", 0))
        if period == 0:
            return True
        if len(df) < period:
            return True
        from core.indicators import sma
        sma_val = float(sma(df, period).iloc[-1])
        return float(df["close"].iloc[-1]) >= sma_val

    @abstractmethod
    def scan(self, df: pd.DataFrame, params: dict) -> list[Signal]:
        """
        Check if the last bar of df generates a signal.
        df is already look-ahead guarded (no future bars).
        df.attrs must contain 'symbol' and 'market'.
        """

    @abstractmethod
    def param_space(self) -> dict:
        """
        Combined strategy + RM param grid for walk-forward optimiser.
        Keys map to lists of candidate values.
        """

    def _build_signal(
        self,
        df: pd.DataFrame,
        params: dict,
        entry: float,
        entry_type: str,
        atr_val: float,
        direction: str = "long",
        meta: dict | None = None,
    ) -> Signal:
        p = {**self.default_params, **params}
        if direction == "long":
            sl = entry - p["sl_atr_mult"] * atr_val
            tp1 = entry + p["tp1_atr_mult"] * atr_val
            tp2 = entry + p["tp2_atr_mult"] * atr_val
        else:
            sl = entry + p["sl_atr_mult"] * atr_val
            tp1 = entry - p["tp1_atr_mult"] * atr_val
            tp2 = entry - p["tp2_atr_mult"] * atr_val

        from core.tx_cost import cost_adjust_rr
        rr = cost_adjust_rr(entry, sl, tp1, df.attrs.get("market", ""))

        last_date = df.index[-1]
        gen_date = last_date.date() if hasattr(last_date, "date") else None

        return Signal(
            symbol=df.attrs.get("symbol", ""),
            market=df.attrs.get("market", ""),
            strategy=self.id,
            direction=direction,
            entry=entry,
            entry_type=entry_type,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=None,
            atr=atr_val,
            rr=rr,
            score=50.0,
            meta=meta or {},
            sl_atr_mult=p["sl_atr_mult"],
            tp1_atr_mult=p["tp1_atr_mult"],
            tp2_atr_mult=p["tp2_atr_mult"],
            risk_pct=p["risk_pct"],
            max_bars=p["max_bars"],
            trail_atr_mult=p["trail_atr_mult"],
            be_trigger_atr_mult=p["be_trigger_atr_mult"],
            tp1_partial_pct=float(p.get("tp1_partial_pct", 0.5)),
            tp2_partial_pct=float(p.get("tp2_partial_pct", 1.0)),
            ema_exit_period=int(p.get("ema_exit_period", 0)),
            generated_at=gen_date,
        )
