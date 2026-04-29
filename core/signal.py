from __future__ import annotations
import itertools
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

_pos_id_counter = itertools.count(1)


@dataclass
class ExitSignal:
    reason: str       # "sl" | "tp1" | "tp2" | "trail" | "time_stop"
    price: float
    partial: bool = False
    partial_pct: float = 1.0


@dataclass
class Signal:
    symbol: str
    market: str
    strategy: str
    direction: str            # "long" | "short"
    entry: float
    entry_type: str           # "market_close" | "pending_stop" | "pending_limit"
    sl: float
    tp1: float
    tp2: float
    tp3: Optional[float]
    atr: float
    rr: float                 # reward/risk at TP1 (cost-adjusted)
    score: float              # composite 0-100
    meta: dict = field(default_factory=dict)
    # RM params (walk-forward optimised per strategy)
    sl_atr_mult: float = 2.0
    tp1_atr_mult: float = 2.0
    tp2_atr_mult: float = 3.0
    risk_pct: float = 0.005
    # Hard exit params
    max_bars: int = 0
    trail_atr_mult: float = 2.0
    be_trigger_atr_mult: float = 1.0
    tp1_partial_pct: float = 0.3    # fraction to sell at TP1, trail the rest
    tp2_partial_pct: float = 0.3    # fraction to sell at TP2, trail the rest
    ema_exit_period: int = 0        # 0=off, 5=EMA5, 10=EMA10 hard exit after TP1
    hard_stop_mode: str = "both"   # both | trail | ema10
    exit_policies: list = field(default_factory=lambda: ["hard_exit"])
    generated_at: Optional[date] = None


@dataclass
class Position:
    signal: Signal
    entry_price: float
    entry_date: date
    size: int
    bars_held: int = 0
    position_id: int = field(default_factory=lambda: next(_pos_id_counter))
    highest_close: float = 0.0
    sl_current: float = 0.0
    tp1_hit: bool = False
    tp2_hit: bool = False
    is_open: bool = True

    def __post_init__(self) -> None:
        self.sl_current = self.signal.sl
        self.highest_close = self.entry_price
