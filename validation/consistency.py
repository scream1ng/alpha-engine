from __future__ import annotations
import logging
from datetime import date
from dateutil.relativedelta import relativedelta
import pandas as pd
from strategies.base import Strategy
from validation.backtest import run_backtest
from config import CONSISTENCY_THRESHOLD

logger = logging.getLogger(__name__)


def check_consistency(
    df: pd.DataFrame,
    strategy: Strategy,
    params: dict,
    initial_capital: float = 100_000,
    threshold: float = CONSISTENCY_THRESHOLD,
) -> dict:
    """
    Compare metrics over 2yr full period vs 1yr recent period.
    Fail if any key metric in recent 1yr < threshold × full 2yr metric.
    Returns {pass: bool, details: dict}.
    """
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    end_date = df.index[-1].date()
    start_2yr = end_date - relativedelta(years=2)
    start_1yr = end_date - relativedelta(years=1)

    df_2yr = df[df.index.date >= start_2yr].copy()
    df_1yr = df[df.index.date >= start_1yr].copy()
    df_2yr.attrs = df.attrs
    df_1yr.attrs = df.attrs

    if len(df_2yr) < 100 or len(df_1yr) < 50:
        return {"pass": False, "reason": "insufficient_data", "details": {}}

    m2 = run_backtest(df_2yr, strategy, params, initial_capital)
    m1 = run_backtest(df_1yr, strategy, params, initial_capital)

    check_keys = ["sharpe", "profit_factor", "win_rate"]
    failures: list[str] = []
    details: dict = {}

    for key in check_keys:
        v2 = m2.get(key, 0.0)
        v1 = m1.get(key, 0.0)
        ratio = v1 / v2 if v2 > 0 else 0.0
        details[key] = {"full_2yr": v2, "recent_1yr": v1, "ratio": ratio}
        if ratio < threshold:
            failures.append(key)

    passed = len(failures) == 0
    return {
        "pass": passed,
        "failures": failures,
        "reason": f"drift in {failures}" if failures else "ok",
        "details": details,
        "full_2yr": {k: v for k, v in m2.items() if k != "trades"},
        "recent_1yr": {k: v for k, v in m1.items() if k != "trades"},
    }
