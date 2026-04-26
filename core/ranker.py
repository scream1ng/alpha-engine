from __future__ import annotations
from core.signal import Signal
from core.tx_cost import cost_adjust_rr


def _score(signal: Signal) -> float:
    rr_score = min(signal.rr / 3.0, 1.0) * 40
    raw_score = (signal.score / 100.0) * 40
    rr_adj = cost_adjust_rr(signal.entry, signal.sl, signal.tp1, signal.market)
    cost_score = min(rr_adj / 2.0, 1.0) * 20
    return rr_score + raw_score + cost_score


def rank_signals(signals: list[Signal]) -> list[Signal]:
    for sig in signals:
        sig.score = _score(sig)
    return sorted(signals, key=lambda s: s.score, reverse=True)
