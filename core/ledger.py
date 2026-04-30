from __future__ import annotations
from datetime import date
from typing import Optional
import pandas as pd
from core.signal import Position, ExitSignal


class PortfolioLedger:
    def __init__(self) -> None:
        self._positions: list[Position] = []
        self._closed: list[dict] = []

    def register_fill(self, position: Position) -> None:
        self._positions.append(position)

    def register_exit(
        self, position: Position, exit_signal: ExitSignal, exit_date: date
    ) -> None:
        sig = position.signal
        if sig.direction == "long":
            pnl_per = exit_signal.price - position.entry_price
        else:
            pnl_per = position.entry_price - exit_signal.price

        size = (
            int(position.size * exit_signal.partial_pct)
            if exit_signal.partial
            else position.size
        )
        self._closed.append(
            {
                "symbol": sig.symbol,
                "market": sig.market,
                "strategy": sig.strategy,
                "direction": sig.direction,
                "entry_price": position.entry_price,
                "exit_price": exit_signal.price,
                "exit_reason": exit_signal.reason,
                "entry_date": position.entry_date,
                "exit_date": exit_date,
                "size": size,
                "pnl": pnl_per * size,
                "bars_held": position.bars_held,
                "position_id": position.position_id,
            }
        )
        if not exit_signal.partial:
            position.is_open = False
            self._positions = [p for p in self._positions if p.is_open]
        else:
            position.size -= size

    def open_positions(self) -> list[Position]:
        return list(self._positions)

    def current_heat(self, market: Optional[str] = None) -> float:
        positions = self._positions
        if market:
            positions = [p for p in positions if p.signal.market == market]
        return sum(p.signal.risk_pct for p in positions)

    def correlation_matrix(self) -> pd.DataFrame:
        if len(self._positions) < 2:
            return pd.DataFrame()
        symbols = [p.signal.symbol for p in self._positions]
        return pd.DataFrame(index=symbols, columns=symbols, data=0.0)

    def closed_trades(self) -> list[dict]:
        return list(self._closed)

    def pnl_summary(self) -> dict:
        if not self._closed:
            return {
                "total_pnl": 0.0,
                "win_rate": 0.0,
                "trade_count": 0,
                "avg_bars": 0.0,
            }
        from collections import defaultdict
        pos_pnl: dict = defaultdict(float)
        pos_bars: dict = defaultdict(int)
        for t in self._closed:
            pid = t.get("position_id", id(t))
            pos_pnl[pid] += t["pnl"]
            pos_bars[pid] = t["bars_held"]
        pnls = list(pos_pnl.values())
        wins = [p for p in pnls if p > 0]
        avg_bars = sum(pos_bars.values()) / len(pos_bars) if pos_bars else 0.0
        return {
            "total_pnl": sum(pnls),
            "win_rate": len(wins) / len(pnls),
            "trade_count": len(pnls),
            "avg_bars": avg_bars,
        }
