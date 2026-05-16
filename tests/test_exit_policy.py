from __future__ import annotations

import pandas as pd

from core.exit_policy import HardExitPolicy
from core.signal import Position, Signal


def test_hard_exit_policy_ignores_disabled_tp2() -> None:
    signal = Signal(
        symbol="TEST.AX",
        market="au",
        strategy="pivot_breakout",
        direction="long",
        entry=10.0,
        entry_type="pending_stop",
        sl=9.0,
        tp1=100.0,
        tp2=20.0,
        tp3=None,
        atr=1.0,
        rr=2.0,
        score=50.0,
        tp2_atr_mult=999.0,
        tp2_partial_pct=0.0,
        hard_stop_mode="trail",
    )
    position = Position(signal=signal, entry_price=10.0, entry_date=pd.Timestamp("2022-01-03").date(), size=100)
    bar = {"open": 10.0, "high": 25.0, "low": 9.5, "close": 10.5, "ema10": 10.0}

    exit_sig = HardExitPolicy().check(position, bar, params={})

    assert exit_sig is None
