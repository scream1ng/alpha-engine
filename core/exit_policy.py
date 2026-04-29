from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional
from core.signal import ExitSignal, Position


class ExitPolicy(ABC):
    id: str

    @abstractmethod
    def check(self, position: Position, bar: dict, params: dict) -> Optional[ExitSignal]:
        """Return ExitSignal to exit, None to hold."""


class HardExitPolicy(ExitPolicy):
    """Time stop + trailing stop + breakeven, in priority order."""
    id = "hard_exit"

    def check(self, position: Position, bar: dict, params: dict) -> Optional[ExitSignal]:
        sig = position.signal
        close = bar["close"]
        high = bar["high"]
        low = bar["low"]
        hard_stop_mode = str(params.get("hard_stop_mode", getattr(sig, "hard_stop_mode", "both")) or "both").lower()
        use_trail = hard_stop_mode in ("both", "trail")
        use_ema = hard_stop_mode in ("both", "ema10")

        if sig.direction == "long":
            # 1. Hard SL
            if low <= position.sl_current:
                return ExitSignal(reason="sl", price=position.sl_current)

            # 2. TP2 partial or full exit
            if not position.tp2_hit and high >= sig.tp2:
                position.tp2_hit = True
                pct = getattr(sig, "tp2_partial_pct", 1.0)
                partial = pct < 1.0
                return ExitSignal(reason="tp2", price=sig.tp2, partial=partial, partial_pct=pct)

            # 3. TP1 partial exit, move SL to entry
            if not position.tp1_hit and high >= sig.tp1:
                position.tp1_hit = True
                position.sl_current = sig.entry
                pct = getattr(sig, "tp1_partial_pct", 0.5)
                partial = pct < 1.0
                return ExitSignal(reason="tp1", price=sig.tp1, partial=partial, partial_pct=pct)

            # 4. Breakeven trigger (move SL to entry without exiting)
            be_trigger = sig.entry + sig.be_trigger_atr_mult * sig.atr
            if not position.tp1_hit and close >= be_trigger:
                position.sl_current = max(position.sl_current, sig.entry)
            be_after_bars = int(params.get("be_after_bars", 0) or 0)
            if be_after_bars and position.bars_held >= be_after_bars:
                position.sl_current = max(position.sl_current, sig.entry)

            # 5. Trailing stop update
            if use_trail:
                position.highest_close = max(position.highest_close, close)
                trail_sl = position.highest_close - sig.trail_atr_mult * sig.atr
                if trail_sl > position.sl_current:
                    position.sl_current = trail_sl

            # 6. EMA exit — only active after TP1 hit (locks in partial profits)
            ema_period = 10 if hard_stop_mode == "ema10" else params.get("ema_exit_period", getattr(sig, "ema_exit_period", 0))
            ema_exit_always = bool(params.get("ema_exit_always", False))
            if use_ema and ema_period and (position.tp1_hit or ema_exit_always):
                ema_val = bar.get(f"ema{ema_period}")
                if ema_val is not None and close < ema_val:
                    return ExitSignal(reason=f"ema{ema_period}_exit", price=close)

            # 7. Optional time stop
            max_bars = int(params.get("max_bars", sig.max_bars) or 0)
            if max_bars > 0 and position.bars_held >= max_bars:
                return ExitSignal(reason="time_stop", price=close)

        else:  # short
            if high >= position.sl_current:
                return ExitSignal(reason="sl", price=position.sl_current)
            if not position.tp2_hit and low <= sig.tp2:
                position.tp2_hit = True
                pct = getattr(sig, "tp2_partial_pct", 1.0)
                partial = pct < 1.0
                return ExitSignal(reason="tp2", price=sig.tp2, partial=partial, partial_pct=pct)
            if not position.tp1_hit and low <= sig.tp1:
                position.tp1_hit = True
                position.sl_current = sig.entry
                pct = getattr(sig, "tp1_partial_pct", 0.5)
                partial = pct < 1.0
                return ExitSignal(reason="tp1", price=sig.tp1, partial=partial, partial_pct=pct)
            be_trigger = sig.entry - sig.be_trigger_atr_mult * sig.atr
            if not position.tp1_hit and close <= be_trigger:
                position.sl_current = min(position.sl_current, sig.entry)
            be_after_bars = int(params.get("be_after_bars", 0) or 0)
            if be_after_bars and position.bars_held >= be_after_bars:
                position.sl_current = min(position.sl_current, sig.entry)
            if use_trail:
                position.highest_close = min(position.highest_close, close)
                trail_sl = position.highest_close + sig.trail_atr_mult * sig.atr
                if trail_sl < position.sl_current:
                    position.sl_current = trail_sl
            ema_period = 10 if hard_stop_mode == "ema10" else params.get("ema_exit_period", getattr(sig, "ema_exit_period", 0))
            ema_exit_always = bool(params.get("ema_exit_always", False))
            if use_ema and ema_period and (position.tp1_hit or ema_exit_always):
                ema_val = bar.get(f"ema{ema_period}")
                if ema_val is not None and close > ema_val:
                    return ExitSignal(reason=f"ema{ema_period}_exit", price=close)
            max_bars = int(params.get("max_bars", sig.max_bars) or 0)
            if max_bars > 0 and position.bars_held >= max_bars:
                return ExitSignal(reason="time_stop", price=close)

        return None


EXIT_POLICIES: dict[str, ExitPolicy] = {
    HardExitPolicy.id: HardExitPolicy(),
}


def get_exit_policies(policy_ids: list[str]) -> list[ExitPolicy]:
    return [EXIT_POLICIES[pid] for pid in policy_ids if pid in EXIT_POLICIES]
