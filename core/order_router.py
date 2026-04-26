from __future__ import annotations
from core.signal import Signal


def is_pending_order(signal: Signal) -> bool:
    return signal.entry_type in ("pending_stop", "pending_limit")


def is_market_order(signal: Signal) -> bool:
    return signal.entry_type == "market_close"


def check_pending_triggered(signal: Signal, bar: dict) -> bool:
    """Return True if bar triggered the pending order."""
    if signal.entry_type == "pending_stop":
        return bar["high"] >= signal.entry
    if signal.entry_type == "pending_limit":
        return bar["low"] <= signal.entry
    return False
