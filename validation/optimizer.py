from __future__ import annotations
import logging
from datetime import date
from dateutil.relativedelta import relativedelta
import pandas as pd
from joblib import Parallel, delayed
from sklearn.model_selection import ParameterGrid
from strategies.base import Strategy
from validation.backtest import run_backtest
from config import (
    WALKFORWARD_TRAIN_MONTHS,
    WALKFORWARD_TEST_MONTHS,
    SCORING_WEIGHTS,
    SELECTION_GATE,
)

logger = logging.getLogger(__name__)


def _composite_score(metrics: dict) -> float:
    w = SCORING_WEIGHTS
    # Scale up to 1.0 at 10 trades — fewer trades = unreliable Calmar/Sharpe
    trade_penalty = min(metrics.get("trade_count", 0) / 10.0, 1.0)
    return trade_penalty * (
        w["calmar"] * metrics["calmar"]
        + w["sharpe"] * metrics["sharpe"]
        + w["profit_factor"] * (metrics["profit_factor"] - 1)
        + w["win_rate"] * metrics["win_rate"]
    )


def _passes_gate(metrics: dict) -> bool:
    g = SELECTION_GATE
    return (
        metrics.get("annual_return", 0.0) >= g["min_annual_return"]
        and metrics["sharpe"] >= g["min_sharpe"]
        and metrics["calmar"] >= g["min_calmar"]
        and metrics["profit_factor"] >= g["min_profit_factor"]
        and metrics["win_rate"] >= g["min_win_rate"]
        and metrics["trade_count"] >= g["min_trades"]
    )


def _eval_params(df: pd.DataFrame, strategy: Strategy, params: dict, capital: float) -> dict:
    try:
        m = run_backtest(df, strategy, params, initial_capital=capital)
        m["params"] = params
        m["score"] = _composite_score(m)
        return m
    except Exception as exc:
        logger.debug("param eval failed: %s", exc)
        return {"score": -999, "params": params, "trade_count": 0}


def walk_forward_optimise(
    df: pd.DataFrame,
    strategy: Strategy,
    initial_capital: float = 100_000,
    train_months: int = WALKFORWARD_TRAIN_MONTHS,
    test_months: int = WALKFORWARD_TEST_MONTHS,
    n_jobs: int = -1,
    n_iter: int = 150,
    seed: int = 42,
) -> dict:
    """
    Rolling walk-forward optimisation.
    Returns best params from the most recent test window, plus full window history.
    """
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    import random
    random.seed(seed)

    full_grid = list(ParameterGrid(strategy.param_space()))
    # Random sampling when grid is large — avoids combinatorial explosion
    if n_iter and len(full_grid) > n_iter:
        param_grid = random.sample(full_grid, n_iter)
    else:
        param_grid = full_grid

    start_date = df.index[0].date()
    end_date = df.index[-1].date()
    symbol = df.attrs.get("symbol", "?")

    logger.info(
        "  [wf] %s/%s | bars=%d (%s→%s) | grid=%d/%d params",
        symbol, strategy.id, len(df), start_date, end_date, len(param_grid), len(full_grid),
    )

    window_results: list[dict] = []
    cursor = start_date + relativedelta(months=train_months)
    window_num = 0

    while cursor + relativedelta(months=test_months) <= end_date:
        train_start = cursor - relativedelta(months=train_months)
        train_end = cursor
        test_end = cursor + relativedelta(months=test_months)
        window_num += 1

        train_df = df[
            (df.index.date >= train_start) & (df.index.date < train_end)
        ].copy()
        test_df = df[
            (df.index.date >= train_end) & (df.index.date < test_end)
        ].copy()
        train_df.attrs = df.attrs
        test_df.attrs = df.attrs

        if len(train_df) < 100 or len(test_df) < 20:
            logger.debug("  [wf] window %d skipped — insufficient bars (train=%d test=%d)",
                         window_num, len(train_df), len(test_df))
            cursor += relativedelta(months=test_months)
            continue

        logger.info(
            "  [wf] window %d: train %s→%s (%d bars), test →%s (%d bars)",
            window_num, train_start, train_end, len(train_df), test_end, len(test_df),
        )

        # Grid search on train window
        results = Parallel(n_jobs=n_jobs)(
            delayed(_eval_params)(train_df, strategy, p, initial_capital)
            for p in param_grid
        )
        results = [r for r in results if r["trade_count"] > 0]

        if not results:
            logger.info("  [wf] window %d — no params produced trades, skipping", window_num)
            cursor += relativedelta(months=test_months)
            continue

        best_train = max(results, key=lambda r: r["score"])
        logger.info(
            "  [wf] window %d best train: score=%.3f sharpe=%.2f calmar=%.2f pf=%.2f wr=%.0f%% trades=%d",
            window_num,
            best_train["score"], best_train["sharpe"], best_train["calmar"],
            best_train["profit_factor"], best_train["win_rate"] * 100, best_train["trade_count"],
        )

        # Evaluate best params on out-of-sample test window
        test_metrics = run_backtest(test_df, strategy, best_train["params"], initial_capital)
        test_metrics["params"] = best_train["params"]
        test_metrics["score"] = _composite_score(test_metrics)
        test_metrics["train_start"] = str(train_start)
        test_metrics["train_end"] = str(train_end)
        test_metrics["test_end"] = str(test_end)
        test_metrics["passes_gate"] = _passes_gate(test_metrics)

        gate_str = "PASS" if test_metrics["passes_gate"] else "FAIL"
        logger.info(
            "  [wf] window %d OOS [%s]: score=%.3f sharpe=%.2f calmar=%.2f pf=%.2f wr=%.0f%% trades=%d",
            window_num, gate_str,
            test_metrics["score"], test_metrics["sharpe"], test_metrics["calmar"],
            test_metrics["profit_factor"], test_metrics["win_rate"] * 100, test_metrics["trade_count"],
        )

        window_results.append(test_metrics)
        cursor += relativedelta(months=test_months)

    if not window_results:
        logger.warning("  [wf] no valid windows for %s/%s", symbol, strategy.id)
        return {"status": "no_windows", "best_params": strategy.default_params}

    passing = [w for w in window_results if w.get("passes_gate")]
    if not passing:
        logger.warning("  [wf] %s/%s — no window passed gate, using best-of-all", symbol, strategy.id)
        passing = window_results

    best = max(passing, key=lambda w: w["score"])
    status = "ok" if best.get("passes_gate") else "below_gate"
    logger.info(
        "  [wf] RESULT %s/%s: status=%s score=%.3f windows=%d passing=%d",
        symbol, strategy.id, status, best["score"], len(window_results), len([w for w in window_results if w.get("passes_gate")]),
    )

    return {
        "status": status,
        "best_params": best["params"],
        "best_score": best["score"],
        "best_metrics": {k: v for k, v in best.items() if k not in ("params", "trades")},
        "window_results": [
            {k: v for k, v in w.items() if k != "trades"} for w in window_results
        ],
    }
