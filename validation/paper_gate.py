from __future__ import annotations
import logging
from datetime import date
from dateutil.relativedelta import relativedelta
from core.ledger import PortfolioLedger
from config import PAPER_MIN_TRADES, PAPER_MIN_MONTHS, SELECTION_GATE

logger = logging.getLogger(__name__)


def evaluate_paper_gate(
    ledger: PortfolioLedger,
    paper_start_date: date,
    today: date,
    backtest_win_rate: float,
    backtest_max_dd: float,
    win_rate_tolerance: float = 0.15,
    max_dd_tolerance: float = 0.10,
) -> dict:
    """
    Gate check after paper trading period.
    Pass conditions:
    - min PAPER_MIN_MONTHS months elapsed OR min PAPER_MIN_TRADES trades
    - actual win rate within win_rate_tolerance of backtest
    - actual max drawdown within max_dd_tolerance of backtest
    """
    summary = ledger.pnl_summary()
    trade_count = summary["trade_count"]
    elapsed_months = (
        (today.year - paper_start_date.year) * 12
        + (today.month - paper_start_date.month)
    )

    # Check minimum duration / trades
    duration_ok = elapsed_months >= PAPER_MIN_MONTHS or trade_count >= PAPER_MIN_TRADES
    if not duration_ok:
        return {
            "pass": False,
            "reason": f"insufficient_data: {elapsed_months}mo / {trade_count} trades",
            "trade_count": trade_count,
            "elapsed_months": elapsed_months,
        }

    if trade_count == 0:
        return {"pass": False, "reason": "no_trades", "trade_count": 0}

    actual_win_rate = summary["win_rate"]

    # Compute actual max drawdown from closed trades
    trades = ledger.closed_trades()
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t["pnl"]
        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak
            max_dd = max(max_dd, dd)

    wr_diff = abs(actual_win_rate - backtest_win_rate)
    dd_diff = abs(max_dd - backtest_max_dd)

    wr_ok = wr_diff <= win_rate_tolerance
    dd_ok = dd_diff <= max_dd_tolerance
    passed = wr_ok and dd_ok

    return {
        "pass": passed,
        "reason": "ok" if passed else (
            f"win_rate_drift={wr_diff:.2%}" if not wr_ok else f"max_dd_drift={dd_diff:.2%}"
        ),
        "trade_count": trade_count,
        "elapsed_months": elapsed_months,
        "actual_win_rate": actual_win_rate,
        "backtest_win_rate": backtest_win_rate,
        "win_rate_diff": wr_diff,
        "actual_max_dd": max_dd,
        "backtest_max_dd": backtest_max_dd,
        "max_dd_diff": dd_diff,
    }
