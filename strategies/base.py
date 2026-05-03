from __future__ import annotations
from abc import ABC, abstractmethod
import pandas as pd
from core.signal import Signal


class Strategy(ABC):
    id: str
    default_params: dict
    FILTER_PARAM_KEYS = (
        "trend_filter",
        "trend_sma_period",
        "trend_period",
        "sma_period",
        "rvol_min",
        "rvol_max_on_pullback",
        "rsm_min",
        "str_max",
    )
    RISK_PARAM_KEYS = (
        "sl_atr_mult",
        "tp1_atr_mult",
        "tp2_atr_mult",
        "be_trigger_atr_mult",
        "trail_atr_mult",
        "hard_stop_mode",
    )

    def _subset_param_space(self, keys: tuple[str, ...]) -> dict:
        return {key: values for key, values in self.param_space().items() if key in keys}

    def filter_param_space(self) -> dict:
        return self._subset_param_space(self.FILTER_PARAM_KEYS)

    def risk_param_space(self) -> dict:
        space = self._subset_param_space(self.RISK_PARAM_KEYS)
        if "hard_stop_mode" not in space:
            space["hard_stop_mode"] = ["trail", "ema10"]
        return space

    def _trend_periods(self, params: dict) -> list[int]:
        mode = params.get("trend_filter")
        if mode is None:
            period = int(params.get("trend_sma_period", 0) or 0)
            return [period] if period > 0 else []

        if isinstance(mode, (int, float)):
            period = int(mode)
            return [period] if period > 0 else []

        raw = str(mode).strip().lower()
        if raw in ("", "0", "off", "none"):
            return []

        periods: list[int] = []
        for token in raw.replace("sma", "").split("_"):
            token = token.strip()
            if not token:
                continue
            period = int(token)
            if period > 0 and period not in periods:
                periods.append(period)
        return sorted(periods)

    def _in_uptrend(self, df: pd.DataFrame, params: dict) -> bool:
        """Return False if active trend filters do not confirm an uptrend."""
        periods = self._trend_periods(params)
        if not periods:
            return True
        if len(df) < max(periods):
            return True
        from core.indicators import sma

        close = float(df["close"].iloc[-1])
        sma_values = []
        for period in periods:
            col = f"_sma{period}"
            val = float(df[col].iloc[-1]) if col in df.columns else float(sma(df, period).iloc[-1])
            sma_values.append((period, val))
        if any(close < sma_val for _, sma_val in sma_values):
            return False
        if len(sma_values) < 2:
            return True
        return all(
            faster_val >= slower_val
            for (_, faster_val), (_, slower_val) in zip(sma_values, sma_values[1:])
        )

    def _rsm_ok(self, df: pd.DataFrame, params: dict) -> bool:
        """Return False if RSM filter active and current RSM below threshold.
        RSM not applicable for crypto/commodity — always returns True for those."""
        if df.attrs.get("market", "") in ("crypto", "commodity"):
            return True
        rsm_min = float(params.get("rsm_min", 0))
        if rsm_min <= 0:
            return True
        col = "_rsm" if "_rsm" in df.columns else None
        if col is None:
            from core.indicators import rsm as _rsm_fn
            rsm_val = float(_rsm_fn(df).iloc[-1])
        else:
            rsm_val = float(df[col].iloc[-1])
        import math
        if math.isnan(rsm_val):
            return True  # no benchmark data — pass through
        return rsm_val >= rsm_min

    def _stretch_ok(self, df: pd.DataFrame, params: dict) -> bool:
        """Return False if STR filter active and price is overextended above SMA50.
        STR = (close - SMA50) / ATR. str_max=0 disables the filter."""
        import math
        str_max = float(params.get("str_max", 0))
        if str_max <= 0:
            return True
        if "_stretch" in df.columns:
            val = float(df["_stretch"].iloc[-1])
        else:
            from core.indicators import stretch as _stretch_fn
            val = float(_stretch_fn(df).iloc[-1])
        if math.isnan(val):
            return True
        return val <= str_max

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
            hard_stop_mode=str(p.get("hard_stop_mode", "both")),
            generated_at=gen_date,
        )
