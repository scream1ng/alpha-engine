from __future__ import annotations
import logging
import random
from statistics import median
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
import pandas as pd
from joblib import Parallel, delayed
from sklearn.model_selection import ParameterGrid
from strategies.base import Strategy
from validation.backtest import run_backtest, run_portfolio_backtest
from config import (
    WALKFORWARD_TRAIN_MONTHS,
    WALKFORWARD_TEST_MONTHS,
    OPTIMIZER_OBJECTIVE,
    SCORING_WEIGHTS,
    SELECTION_GATE,
)

logger = logging.getLogger(__name__)


def _objective_name() -> str:
    objective = str(OPTIMIZER_OBJECTIVE or "annual_return").strip().lower()
    return objective if objective in {"annual_return", "score"} else "annual_return"


def _objective_value(metrics: dict) -> tuple[float, float, float, float, float, int]:
    objective = _objective_name()
    primary = float(metrics.get(objective, 0.0) or 0.0)
    secondary = float(metrics.get("score", _composite_score(metrics)) or 0.0)
    return (
        primary,
        secondary,
        float(metrics.get("calmar", 0.0) or 0.0),
        float(metrics.get("sharpe", 0.0) or 0.0),
        float(metrics.get("profit_factor", 0.0) or 0.0),
        int(metrics.get("trade_count", 0) or 0),
    )


def _effective_params(base_params: dict | None, params: dict) -> dict:
    if not base_params:
        return dict(params)
    return {**base_params, **params}


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


def _eval_params(
    df: pd.DataFrame,
    strategy: Strategy,
    params: dict,
    capital: float,
    base_params: dict | None = None,
) -> dict:
    try:
        effective_params = _effective_params(base_params, params)
        m = run_backtest(df, strategy, effective_params, initial_capital=capital)
        m["params"] = effective_params
        m["score"] = _composite_score(m)
        return m
    except Exception as exc:
        logger.debug("param eval failed: %s", exc)
        return {"score": -999, "params": _effective_params(base_params, params), "trade_count": 0}


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
    base_params: dict | None = None,
) -> dict:
    try:
        effective_params = _effective_params(base_params, params)
        metrics_list = [
            run_backtest(df, strategy, effective_params, initial_capital=capital)
            for df in dfs
        ]
        m = aggregate_market_metrics(metrics_list, universe_size=universe_size)
        m["params"] = effective_params
        m["score"] = _composite_score(m)
        return m
    except Exception as exc:
        logger.debug("market param eval failed: %s", exc)
        return {
            "score": -999,
            "params": _effective_params(base_params, params),
            "trade_count": 0,
            "sampled_symbol_count": len(dfs),
            "traded_symbol_count": 0,
            "profitable_symbol_rate": 0.0,
            "universe_size": universe_size,
        }


def _slice_by_date(dfs: list, start: date, end: date) -> list:
    import pandas as pd
    out = []
    ts_start, ts_end = pd.Timestamp(start), pd.Timestamp(end)
    for df in dfs:
        sl = df[(df.index >= ts_start) & (df.index < ts_end)].copy()
        sl.attrs = df.attrs
        if len(sl) >= 60:
            out.append(sl)
    return out


def optimise_market_grid(
    dfs: list,
    strategy: "Strategy",
    param_space: dict,
    base_params: dict,
    capital: float,
    as_of: date,
    n_jobs: int = 1,
    n_iter: int = 300,
    seed: int = 42,
) -> dict:
    """
    Grid search param_space against Y1 portfolio backtest.
    Ranks candidates by Y1 annual_return (most recent year = "best return for now").
    No rolling windows — direct 3yr evaluation.
    """
    if not dfs:
        return {"status": "no_data", "best_params": dict(base_params), "best_score": 0.0, "best_metrics": {}}

    random.seed(seed)

    full_grid = list(ParameterGrid(param_space)) if param_space else [{}]
    if n_iter and len(full_grid) > n_iter:
        param_grid = random.sample(full_grid, n_iter)
    else:
        param_grid = full_grid

    market = dfs[0].attrs.get("market", "?")
    logger.info(
        "  [grid] %s/%s | symbols=%d | grid=%d/%d | objective=Y1_annual_return",
        market, strategy.id, len(dfs), len(param_grid), len(full_grid),
    )

    y1_dfs = _slice_by_date(dfs, as_of - timedelta(days=365), as_of)
    if not y1_dfs:
        logger.warning("  [grid] no Y1 data for %s/%s", market, strategy.id)
        return {"status": "no_data", "best_params": dict(base_params), "best_score": 0.0, "best_metrics": {}}

    def _eval(combo: dict) -> dict:
        effective = {**base_params, **combo}
        try:
            m = run_portfolio_backtest(y1_dfs, strategy, effective, initial_capital=capital)
            m["params"] = effective
            m["score"] = _composite_score(m)
        except Exception as exc:
            logger.debug("grid eval failed: %s", exc)
            m = {"score": -999.0, "params": effective, "trade_count": 0, "annual_return": 0.0,
                 "sharpe": 0.0, "calmar": 0.0, "profit_factor": 0.0, "win_rate": 0.0, "max_drawdown": 0.0}
        return m

    results = Parallel(n_jobs=n_jobs)(delayed(_eval)(p) for p in param_grid)
    results = [r for r in results if r.get("trade_count", 0) > 0]

    if not results:
        logger.warning("  [grid] no combos produced trades for %s/%s", market, strategy.id)
        return {"status": "no_data", "best_params": dict(base_params), "best_score": 0.0, "best_metrics": {}}

    best = max(results, key=lambda r: float(r.get("annual_return", 0.0) or 0.0))

    logger.info(
        "  [grid] best: ret=%+.1f%% score=%.3f sharpe=%.2f trades=%d | params=%s",
        best.get("annual_return", 0.0) * 100, best.get("score", 0.0),
        best.get("sharpe", 0.0), best.get("trade_count", 0),
        {k: v for k, v in best["params"].items() if k in param_space},
    )

    passes = _passes_gate(best)
    return {
        "status": "ok" if passes else "below_gate",
        "best_params": best["params"],
        "best_score": best.get("score", 0.0),
        "best_metrics": {k: v for k, v in best.items() if k not in ("params", "trades")},
    }


def walk_forward_optimise(
    df: pd.DataFrame,
    strategy: Strategy,
    initial_capital: float = 100_000,
    param_space: dict | None = None,
    base_params: dict | None = None,
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

    full_grid = list(ParameterGrid(param_space or strategy.param_space()))
    # Random sampling when grid is large — avoids combinatorial explosion
    if n_iter and len(full_grid) > n_iter:
        param_grid = random.sample(full_grid, n_iter)
    else:
        param_grid = full_grid

    symbol = df.attrs.get("symbol", "?")
    start_date = df.index[0].date()
    end_date = df.index[-1].date()

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

        logger.info(
            "  [wf] window %d: train %s→%s (%d bars), test →%s (%d bars)",
            window_num, train_start, train_end, len(train_df), test_end, len(test_df),
        )

        # Grid search on train window
        results = Parallel(n_jobs=n_jobs)(
            delayed(_eval_params)(train_df, strategy, p, initial_capital, base_params)
            for p in param_grid
        )
        results = [r for r in results if r["trade_count"] > 0]

        if not results:
            logger.info("  [wf] window %d — no params produced trades, skipping", window_num)
            continue

        best_train = max(results, key=_objective_value)
        logger.info(
            "  [wf] window %d best train: objective=%s ret=%+.1f%% score=%.3f sharpe=%.2f calmar=%.2f pf=%.2f wr=%.0f%% trades=%d",
            window_num,
            _objective_name(),
            best_train.get("annual_return", 0.0) * 100,
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
            "  [wf] window %d OOS [%s]: ret=%+.1f%% score=%.3f sharpe=%.2f calmar=%.2f pf=%.2f wr=%.0f%% trades=%d",
            window_num, gate_str,
            test_metrics.get("annual_return", 0.0) * 100,
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


def optimise_single_period(
    df: pd.DataFrame,
    strategy: Strategy,
    initial_capital: float = 100_000,
    param_space: dict | None = None,
    base_params: dict | None = None,
    n_jobs: int = -1,
    n_iter: int = 150,
    seed: int = 42,
) -> dict:
    """
    Single-period optimisation: evaluate each param set on the full history
    and return the best set by the configured objective. Also return per-year
    metrics (calendar years) so the caller can inspect Y1/Y2/Y3 breakdowns.
    """
    df = _normalise_df(df)

    random.seed(seed)

    full_grid = list(ParameterGrid(param_space or strategy.param_space()))
    if n_iter and len(full_grid) > n_iter:
        param_grid = random.sample(full_grid, n_iter)
    else:
        param_grid = full_grid

    symbol = df.attrs.get("symbol", "?")
    start_date = df.index[0].date()
    end_date = df.index[-1].date()

    logger.info(
        "  [single] %s/%s | bars=%d (%s→%s) | grid=%d/%d params",
        symbol, strategy.id, len(df), start_date, end_date, len(param_grid), len(full_grid),
    )


    def _yearly_summary_for_params(params: dict) -> dict:
        years = sorted({int(ts.year) for ts in df.index})
        summary: dict = {}
        for y in years:
            year_df = df[df.index.year == y]
            if len(year_df) < 10:
                # skip very small years
                continue
            try:
                m = run_backtest(year_df, strategy, params, initial_capital=initial_capital)
                summary[str(y)] = {
                    "annual_return": float(m.get("annual_return", 0.0) or 0.0),
                    "total_pnl": float(m.get("total_pnl", 0.0) or 0.0),
                    "trade_count": int(m.get("trade_count", 0) or 0),
                }
            except Exception:
                summary[str(y)] = {"annual_return": 0.0, "total_pnl": 0.0, "trade_count": 0}
        return summary


    def _eval_with_years(p: dict) -> dict:
        effective = _effective_params(base_params, p)
        m = _eval_params(df, strategy, p, initial_capital, base_params)
        m["yearly_summary"] = _yearly_summary_for_params(m.get("params", effective))
        return m


    results = Parallel(n_jobs=n_jobs)(delayed(_eval_with_years)(p) for p in param_grid)
    results = [r for r in results if r.get("trade_count", 0) > 0]

    if not results:
        logger.warning("  [single] no params produced trades for %s/%s", symbol, strategy.id)
        return {"status": "no_params", "best_params": strategy.default_params}

    best = max(results, key=_objective_value)

    logger.info(
        "  [single] best: objective=%s ret=%+.1f%% score=%.3f sharpe=%.2f calmar=%.2f pf=%.2f wr=%.0f%% trades=%d",
        _objective_name(), best.get("annual_return", 0.0) * 100, best.get("score", 0.0), best.get("sharpe", 0.0),
        best.get("calmar", 0.0), best.get("profit_factor", 0.0), best.get("win_rate", 0.0) * 100, best.get("trade_count", 0),
    )

    return {
        "status": "ok",
        "best_params": best["params"],
        "best_score": best.get("score", 0.0),
        "best_metrics": {k: v for k, v in best.items() if k not in ("params", "trades")},
        "results": [{k: v for k, v in r.items() if k != "trades"} for r in results],
    }


def walk_forward_optimise_market(
    dfs: list[pd.DataFrame],
    strategy: Strategy,
    initial_capital: float = 100_000,
    param_space: dict | None = None,
    base_params: dict | None = None,
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

    full_grid = list(ParameterGrid(param_space or strategy.param_space()))
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
        "  [wf] %s/%s | symbols=%d | objective=%s | grid=%d/%d params",
        market, strategy.id, universe_size, _objective_name(), len(param_grid), len(full_grid),
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
            delayed(_eval_market_params)(train_dfs, strategy, p, initial_capital, universe_size, base_params)
            for p in param_grid
        )
        results = [r for r in results if r["trade_count"] > 0]

        if not results:
            logger.info("  [wf] window %d — no params produced trades, skipping", window_num)
            continue

        best_train = max(results, key=_objective_value)
        logger.info(
            "  [wf] window %d best train: objective=%s ret=%+.1f%% score=%.3f sharpe=%.2f calmar=%.2f pf=%.2f wr=%.0f%% trades=%d symbols=%d/%d profitable=%.0f%%",
            window_num,
            _objective_name(),
            best_train.get("annual_return", 0.0) * 100,
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
            "  [wf] window %d OOS [%s]: ret=%+.1f%% score=%.3f sharpe=%.2f calmar=%.2f pf=%.2f wr=%.0f%% trades=%d symbols=%d/%d profitable=%.0f%%",
            window_num, gate_str,
            test_metrics.get("annual_return", 0.0) * 100,
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
