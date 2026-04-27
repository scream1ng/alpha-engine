from __future__ import annotations
import logging
import random
from statistics import median
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
    sampled_symbols = metrics.get("sampled_symbol_count")
    if sampled_symbols is None:
        coverage_penalty = 1.0
    else:
        universe_size = max(metrics.get("universe_size", sampled_symbols), 1)
        traded_symbols = metrics.get("traded_symbol_count", sampled_symbols)
        coverage_penalty = min(traded_symbols / universe_size, 1.0)

    profitability_penalty = metrics.get("profitable_symbol_rate", 1.0)

    return trade_penalty * coverage_penalty * profitability_penalty * (
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


def aggregate_market_metrics(metrics_list: list[dict], universe_size: int) -> dict:
    if not metrics_list:
        return {
            "sharpe": 0.0,
            "calmar": 0.0,
            "annual_return": 0.0,
            "profit_factor": 0.0,
            "win_rate": 0.0,
            "trade_count": 0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown": 0.0,
            "total_pnl": 0.0,
            "sampled_symbol_count": 0,
            "traded_symbol_count": 0,
            "profitable_symbol_rate": 0.0,
            "universe_size": universe_size,
        }

    def _median(key: str) -> float:
        values = [float(m.get(key, 0.0) or 0.0) for m in metrics_list]
        return float(median(values)) if values else 0.0

    traded_symbol_count = sum(1 for m in metrics_list if m.get("trade_count", 0) > 0)
    profitable_symbol_count = sum(1 for m in metrics_list if (m.get("annual_return", 0.0) or 0.0) > 0)

    return {
        "sharpe": _median("sharpe"),
        "calmar": _median("calmar"),
        "annual_return": _median("annual_return"),
        "profit_factor": _median("profit_factor"),
        "win_rate": _median("win_rate"),
        "trade_count": int(sum(int(m.get("trade_count", 0) or 0) for m in metrics_list)),
        "avg_win": _median("avg_win"),
        "avg_loss": _median("avg_loss"),
        "max_drawdown": _median("max_drawdown"),
        "total_pnl": float(sum(float(m.get("total_pnl", 0.0) or 0.0) for m in metrics_list)),
        "sampled_symbol_count": len(metrics_list),
        "traded_symbol_count": traded_symbol_count,
        "profitable_symbol_rate": profitable_symbol_count / len(metrics_list),
        "universe_size": universe_size,
    }


def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.index = pd.to_datetime(out.index)
    return out.sort_index()


def _iter_windows(df: pd.DataFrame, train_months: int, test_months: int) -> list[dict]:
    start_date = df.index[0].date()
    end_date = df.index[-1].date()

    windows: list[dict] = []
    cursor = start_date + relativedelta(months=train_months)
    while cursor + relativedelta(months=test_months) <= end_date:
        train_start = cursor - relativedelta(months=train_months)
        train_end = cursor
        test_end = cursor + relativedelta(months=test_months)

        train_df = df[
            (df.index.date >= train_start) & (df.index.date < train_end)
        ].copy()
        test_df = df[
            (df.index.date >= train_end) & (df.index.date < test_end)
        ].copy()
        train_df.attrs = df.attrs
        test_df.attrs = df.attrs

        if len(train_df) >= 100 and len(test_df) >= 20:
            windows.append(
                {
                    "train_start": train_start,
                    "train_end": train_end,
                    "test_end": test_end,
                    "train_df": train_df,
                    "test_df": test_df,
                }
            )

        cursor += relativedelta(months=test_months)

    return windows


def _eval_market_params(
    dfs: list[pd.DataFrame],
    strategy: Strategy,
    params: dict,
    capital: float,
    universe_size: int,
) -> dict:
    try:
        metrics_list = [
            run_backtest(df, strategy, params, initial_capital=capital)
            for df in dfs
        ]
        m = aggregate_market_metrics(metrics_list, universe_size=universe_size)
        m["params"] = params
        m["score"] = _composite_score(m)
        return m
    except Exception as exc:
        logger.debug("market param eval failed: %s", exc)
        return {
            "score": -999,
            "params": params,
            "trade_count": 0,
            "sampled_symbol_count": len(dfs),
            "traded_symbol_count": 0,
            "profitable_symbol_rate": 0.0,
            "universe_size": universe_size,
        }


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
    Rolling walk-forward optimisation for a single symbol.
    Returns params from the latest valid out-of-sample window, plus full window history.
    """
    df = _normalise_df(df)

    random.seed(seed)

    full_grid = list(ParameterGrid(strategy.param_space()))
    # Random sampling when grid is large — avoids combinatorial explosion
    if n_iter and len(full_grid) > n_iter:
        param_grid = random.sample(full_grid, n_iter)
    else:
        param_grid = full_grid

    symbol = df.attrs.get("symbol", "?")

    logger.info(
        "  [wf] %s/%s | bars=%d (%s→%s) | grid=%d/%d params",
        symbol, strategy.id, len(df), start_date, end_date, len(param_grid), len(full_grid),
    )

    window_results: list[dict] = []
    windows = _iter_windows(df, train_months, test_months)

    for window_num, window in enumerate(windows, start=1):
        train_start = window["train_start"]
        train_end = window["train_end"]
        test_end = window["test_end"]
        train_df = window["train_df"]
        test_df = window["test_df"]
        window_num += 1

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

    if not window_results:
        logger.warning("  [wf] no valid windows for %s/%s", symbol, strategy.id)
        return {"status": "no_windows", "best_params": strategy.default_params}

    best = window_results[-1]
    status = "ok" if best.get("passes_gate") else "below_gate"
    logger.info(
        "  [wf] RESULT %s/%s: status=%s latest_score=%.3f windows=%d passing=%d",
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


def walk_forward_optimise_market(
    dfs: list[pd.DataFrame],
    strategy: Strategy,
    initial_capital: float = 100_000,
    train_months: int = WALKFORWARD_TRAIN_MONTHS,
    test_months: int = WALKFORWARD_TEST_MONTHS,
    n_jobs: int = -1,
    n_iter: int = 150,
    seed: int = 42,
) -> dict:
    """
    Rolling walk-forward optimisation across a market universe.
    One shared parameter set is scored on all sampled symbols for each window.
    Returns params from the latest valid out-of-sample window.
    """
    if not dfs:
        return {"status": "no_windows", "best_params": strategy.default_params}

    random.seed(seed)

    full_grid = list(ParameterGrid(strategy.param_space()))
    if n_iter and len(full_grid) > n_iter:
        param_grid = random.sample(full_grid, n_iter)
    else:
        param_grid = full_grid

    normalised_dfs = [_normalise_df(df) for df in dfs]
    universe_size = len(normalised_dfs)
    market = normalised_dfs[0].attrs.get("market", "?")

    window_groups: dict[tuple[date, date, date], dict[str, list[pd.DataFrame]]] = {}
    for df in normalised_dfs:
        for window in _iter_windows(df, train_months, test_months):
            key = (window["train_start"], window["train_end"], window["test_end"])
            group = window_groups.setdefault(key, {"train": [], "test": []})
            group["train"].append(window["train_df"])
            group["test"].append(window["test_df"])

    ordered_windows = sorted(window_groups.items(), key=lambda item: item[0][1])
    logger.info(
        "  [wf] %s/%s | symbols=%d | grid=%d/%d params",
        market, strategy.id, universe_size, len(param_grid), len(full_grid),
    )

    window_results: list[dict] = []
    for window_num, (key, group) in enumerate(ordered_windows, start=1):
        train_start, train_end, test_end = key
        train_dfs = group["train"]
        test_dfs = group["test"]

        logger.info(
            "  [wf] window %d: train %s→%s, test →%s | symbols=%d",
            window_num, train_start, train_end, test_end, len(train_dfs),
        )

        results = Parallel(n_jobs=n_jobs)(
            delayed(_eval_market_params)(train_dfs, strategy, p, initial_capital, universe_size)
            for p in param_grid
        )
        results = [r for r in results if r["trade_count"] > 0]

        if not results:
            logger.info("  [wf] window %d — no params produced trades, skipping", window_num)
            continue

        best_train = max(results, key=lambda r: r["score"])
        logger.info(
            "  [wf] window %d best train: score=%.3f sharpe=%.2f calmar=%.2f pf=%.2f wr=%.0f%% trades=%d symbols=%d/%d profitable=%.0f%%",
            window_num,
            best_train["score"], best_train["sharpe"], best_train["calmar"],
            best_train["profit_factor"], best_train["win_rate"] * 100, best_train["trade_count"],
            best_train["traded_symbol_count"], best_train["sampled_symbol_count"],
            best_train["profitable_symbol_rate"] * 100,
        )

        test_metrics = _eval_market_params(test_dfs, strategy, best_train["params"], initial_capital, universe_size)
        test_metrics["params"] = best_train["params"]
        test_metrics["score"] = _composite_score(test_metrics)
        test_metrics["train_start"] = str(train_start)
        test_metrics["train_end"] = str(train_end)
        test_metrics["test_end"] = str(test_end)
        test_metrics["passes_gate"] = _passes_gate(test_metrics)

        gate_str = "PASS" if test_metrics["passes_gate"] else "FAIL"
        logger.info(
            "  [wf] window %d OOS [%s]: score=%.3f sharpe=%.2f calmar=%.2f pf=%.2f wr=%.0f%% trades=%d symbols=%d/%d profitable=%.0f%%",
            window_num, gate_str,
            test_metrics["score"], test_metrics["sharpe"], test_metrics["calmar"],
            test_metrics["profit_factor"], test_metrics["win_rate"] * 100, test_metrics["trade_count"],
            test_metrics["traded_symbol_count"], test_metrics["sampled_symbol_count"],
            test_metrics["profitable_symbol_rate"] * 100,
        )
        window_results.append(test_metrics)

    if not window_results:
        logger.warning("  [wf] no valid windows for %s/%s", market, strategy.id)
        return {"status": "no_windows", "best_params": strategy.default_params}

    best = window_results[-1]
    status = "ok" if best.get("passes_gate") else "below_gate"
    logger.info(
        "  [wf] RESULT %s/%s: status=%s latest_score=%.3f windows=%d passing=%d",
        market, strategy.id, status, best["score"], len(window_results), len([w for w in window_results if w.get("passes_gate")]),
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
