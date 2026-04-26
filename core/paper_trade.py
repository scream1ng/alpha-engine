from __future__ import annotations
from datetime import date
from core.signal import Signal, Position
from core.exit_policy import get_exit_policies
from core.ledger import PortfolioLedger
from core.order_router import check_pending_triggered, is_pending_order
from core.risk_policy import get_risk_policy


class PaperTrader:
    def __init__(self, capital: float, ledger: PortfolioLedger) -> None:
        self.capital = capital
        self.ledger = ledger
        self._pending: list[Signal] = []
        self._risk_policy = get_risk_policy()

    def submit_signal(self, signal: Signal, bar_date: date) -> None:
        if is_pending_order(signal):
            self._pending.append(signal)
        else:
            self._open_position(signal, signal.entry, bar_date)

    def process_bar(self, bar: dict, bar_date: date) -> list[dict]:
        events: list[dict] = []

        # Check pending order triggers
        still_pending: list[Signal] = []
        for sig in self._pending:
            if check_pending_triggered(sig, bar):
                self._open_position(sig, sig.entry, bar_date)
                events.append({"type": "fill", "symbol": sig.symbol, "price": sig.entry})
            else:
                still_pending.append(sig)
        self._pending = still_pending

        # Check exits
        for position in list(self.ledger.open_positions()):
            position.bars_held += 1
            policies = get_exit_policies(position.signal.exit_policies)
            for policy in policies:
                exit_sig = policy.check(position, bar, position.signal.__dict__)
                if exit_sig:
                    self.ledger.register_exit(position, exit_sig, bar_date)
                    size = (
                        int(position.size * exit_sig.partial_pct)
                        if exit_sig.partial
                        else position.size
                    )
                    if position.signal.direction == "long":
                        pnl = (exit_sig.price - position.entry_price) * size
                    else:
                        pnl = (position.entry_price - exit_sig.price) * size
                    self.capital += pnl
                    events.append(
                        {
                            "type": "exit",
                            "symbol": position.signal.symbol,
                            "reason": exit_sig.reason,
                            "price": exit_sig.price,
                            "pnl": pnl,
                        }
                    )
                    break

        return events

    def _open_position(self, signal: Signal, price: float, entry_date: date) -> None:
        if not self._risk_policy.approve(
            signal, self.capital, self.ledger.current_heat(signal.market), {}
        ):
            return
        size = self._risk_policy.size(self.capital, signal, {}, self.ledger)
        if size == 0:
            return
        position = Position(
            signal=signal,
            entry_price=price,
            entry_date=entry_date,
            size=size,
        )
        self.ledger.register_fill(position)
