"""
Shared market pipeline commands — used by all market runners.
All commands are parameterised by a MarketAdapter instance.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
from datetime import date, timedelta

from joblib import Parallel, delayed

from markets.base import MarketAdapter

logger = logging.getLogger(__name__)

_LEARN_BASE = {
    "sl_atr_mult": 1.5,
    "ema_exit_period": 10,
    "ema_exit_always": True,
    "tp1_atr_mult": 999.0,
    "tp2_atr_mult": 999.0,
    "tp1_partial_pct": 1.0,
    "tp2_partial_pct": 1.0,
    "trail_atr_mult": 999.0,
    "be_trigger_atr_mult": 999.0,
    "be_after_bars": 0,
    "max_bars": 0,
    "risk_pct": 0.005,
    "trend_sma_period": 0,
    "rvol_min": 0.0,
}

_LEARN_PHASES = [
    ("EMA10 exit only",      {}),
    ("+ SMA50 trend filter", {"trend_sma_period": 50}),
    ("+ RVol >=1.5x filter", {"trend_sma_period": 50, "rvol_min": 1.5}),
    ("+ STR <=4.0 filter",   {"trend_sma_period": 50, "rvol_min": 1.5, "str_max": 4.0}),
    ("+ RSM >=75 filter",   {"trend_sma_period": 50, "rvol_min": 1.5, "str_max": 4.0, "rsm_min": 75}),
    ("+ TP/BE risk mgmt",    {
        "trend_sma_period":    50,
        "rvol_min":            1.5,
        "str_max":             4.0,
        "rsm_min":             75,
        "sl_atr_mult":         1.0,
        "tp1_atr_mult":        2.0,
        "tp1_partial_pct":     0.3,
        "tp2_atr_mult":        4.0,
        "tp2_partial_pct":     0.3,
        "be_after_bars":       3,
        "be_trigger_atr_mult": 999.0,  # price-based BE disabled; use bars-based only
    }),
]


def _slice_dfs(dfs: list, start: date, end: date) -> list:
    import pandas as pd
    out = []
    ts_start, ts_end = pd.Timestamp(start), pd.Timestamp(end)
    for df in dfs:
        sl = df[(df.index >= ts_start) & (df.index < ts_end)].copy()
        sl.attrs = df.attrs
        if len(sl) >= 60:
            out.append(sl)
    return out


def _fetch_benchmark(adapter: MarketAdapter, start: date, end: date):
    """Fetch benchmark close prices. Returns Series indexed by date, or None on failure."""
    try:
        bm_df = adapter.ohlcv(adapter.benchmark, start, end)
        if bm_df.empty:
            return None
        return bm_df[["close"]].rename(columns={"close": "_bm_close"})
    except Exception as exc:
        logger.warning("benchmark fetch failed (%s): %s", adapter.benchmark, exc)
        return None


def _attach_benchmark(df, bm_close) -> "pd.DataFrame":
    """Join _bm_close column to stock df, forward-fill gaps."""
    if bm_close is None or df.empty:
        return df
    import pandas as pd
    merged = df.join(bm_close, how="left")
    merged["_bm_close"] = merged["_bm_close"].ffill()
    merged.attrs = df.attrs
    return merged


def _load_live_params(market: str, strategy_id: str) -> dict | None:
    from db.models import SessionLocal, StrategyParamsModel
    db = SessionLocal()
    try:
        row = (
            db.query(StrategyParamsModel)
            .filter_by(market=market, strategy=strategy_id, is_live=True)
            .first()
        )
        if row:
            return dict(row.params)
    finally:
        db.close()
    return None


def _load_saved_params_map(db, market: str) -> dict[str, dict]:
    from db.models import StrategyParamsModel

    rows = db.query(StrategyParamsModel).filter_by(market=market).all()
    return {row.strategy: dict(row.params or {}) for row in rows}


def _optimise_mode_config(strategy, command: str, saved_params: dict | None = None) -> tuple[dict | None, dict | None, str]:
    if command == "optimise-filter":
        return dict(strategy.default_params), strategy.filter_param_space(), "filter"
    if command == "optimise-risk":
        return dict(saved_params or strategy.default_params), strategy.risk_param_space(), "risk"
    return None, None, "full"


def _optimise_strategy_task(
    market: str,
    strategy_id: str,
    strategy_cls,
    market_dfs: list,
    capital: float,
    command: str,
    saved_params: dict | None = None,
) -> dict:
    from validation.optimizer import walk_forward_optimise_market, optimise_market_grid
    from validation.consistency import check_consistency_market

    strategy = strategy_cls()
    base_params, param_space, mode_label = _optimise_mode_config(strategy, command, saved_params)

    if command in ("optimise-filter", "optimise-risk"):
        opt = optimise_market_grid(
            market_dfs,
            strategy,
            param_space or {},
            base_params or {},
            capital,
            date.today(),
            n_jobs=1,
        )
    else:
        opt = walk_forward_optimise_market(
            market_dfs, strategy, initial_capital=capital,
            param_space=param_space, base_params=base_params, n_jobs=1,
        )

    if opt["status"] in ("no_windows", "no_data"):
        return {
            "strategy_id": strategy_id,
            "mode": mode_label,
            "status": opt["status"],
            "opt": opt,
            "consistency": None,
            "is_live": False,
            "metrics": {},
        }

    consistency = check_consistency_market(market_dfs, strategy, opt["best_params"], capital)
    metrics = opt.get("best_metrics", {})
    is_live = opt["status"] == "ok" and consistency["pass"]

    return {
        "strategy_id": strategy_id,
        "mode": mode_label,
        "status": opt["status"],
        "opt": opt,
        "consistency": consistency,
        "is_live": is_live,
        "metrics": metrics,
    }


def _strategy_job_count(args: argparse.Namespace, total_strategies: int) -> int:
    requested = max(int(getattr(args, "strategy_jobs", 1) or 1), 1)
    return min(requested, total_strategies)


def _build_yearly_summary(
    dfs: list,
    strategy,
    params: dict,
    capital: float,
    as_of: date,
) -> dict:
    from validation.backtest import run_portfolio_backtest

    windows = [
        ("y1", as_of - timedelta(days=365), as_of),
        ("y2", as_of - timedelta(days=730), as_of - timedelta(days=365)),
        ("y3", as_of - timedelta(days=1095), as_of - timedelta(days=730)),
    ]

    summary: dict[str, dict] = {}
    for key, start, end in windows:
        window_dfs = _slice_dfs(dfs, start, end)
        if not window_dfs:
            summary[key] = {
                "start": str(start),
                "end": str(end),
                "annual_return": None,
                "max_drawdown": None,
                "trade_count": 0,
                "win_rate": None,
            }
            continue

        agg = run_portfolio_backtest(window_dfs, strategy, params, initial_capital=capital)
        summary[key] = {
            "start": str(start),
            "end": str(end),
            "annual_return": agg.get("annual_return"),
            "max_drawdown": agg.get("max_drawdown"),
            "trade_count": agg.get("trade_count", 0),
            "win_rate": agg.get("win_rate"),
        }

    return summary


def cmd_scan(adapter: MarketAdapter, args: argparse.Namespace) -> list:
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    from core.guard import apply_lookahead_guard
    from core.ranker import rank_signals
    from core.risk import apply_heat_limit

    market = adapter.market_id
    today = date.today()
    start = today - timedelta(days=365)

    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    strategies_map = StrategyRegistry.for_market(market)
    logger.info("%s scan — %s | symbols=%d strategies=%s",
                market.upper(), today, len(universe), list(strategies_map.keys()))

    bm_close = _fetch_benchmark(adapter, start, today)
    all_signals = []
    skipped = 0

    for symbol in universe:
        df = adapter.ohlcv(symbol, start, today)
        if df.empty or len(df) < 60:
            skipped += 1
            continue
        df = apply_lookahead_guard(df, today)
        df = _attach_benchmark(df, bm_close)
        df.attrs = {"symbol": symbol, "market": market}

        sym_signals = []
        for strategy_id, strategy_cls in strategies_map.items():
            params = _load_live_params(market, strategy_id)
            if params is None:
                continue
            strategy = strategy_cls()
            try:
                signals = strategy.scan(df, params)
                if signals:
                    logger.info("  + %s / %s → %d signal(s)", symbol, strategy_id, len(signals))
                sym_signals.extend(signals)
            except Exception as exc:
                logger.warning("  scan error %s/%s: %s", symbol, strategy_id, exc)
        all_signals.extend(sym_signals)

    logger.info("scan done: %d symbols scanned, %d skipped, %d raw signals",
                len(universe) - skipped, skipped, len(all_signals))

    ranked = rank_signals(all_signals)
    approved = apply_heat_limit(ranked, current_heat=0.0)
    logger.info("after ranking+heat filter: %d approved", len(approved))
    for sig in approved:
        logger.info(
            "  %s | %s | entry=%.2f sl=%.2f tp1=%.2f rr=%.2f score=%.1f",
            sig.symbol, sig.strategy, sig.entry, sig.sl, sig.tp1, sig.rr, sig.score,
        )
    return approved


def cmd_diagnose(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    from core.guard import apply_lookahead_guard

    market = adapter.market_id
    today = date.today()
    start = today - timedelta(days=365)

    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    strategies_map = StrategyRegistry.for_market(market)

    fire_count: dict[str, dict[str, int]] = {sid: {} for sid in strategies_map}
    sample_signals: dict[str, list] = {sid: [] for sid in strategies_map}

    print(f"\nDIAGNOSE [{market.upper()}]: scanning {len(universe)} symbols × "
          f"{len(strategies_map)} strategies over 12 months ({start} → {today})\n")

    bm_close = _fetch_benchmark(adapter, start, today)

    for symbol in universe:
        df = adapter.ohlcv(symbol, start, today)
        if df.empty or len(df) < 60:
            print(f"  {symbol}: skip (only {len(df)} bars)")
            continue
        df = apply_lookahead_guard(df, today)
        df = _attach_benchmark(df, bm_close)
        df.attrs = {"symbol": symbol, "market": market}

        for strategy_id, strategy_cls in strategies_map.items():
            strategy = strategy_cls()
            count = 0
            for i in range(50, len(df)):
                bar_df = df.iloc[: i + 1].copy()
                bar_df.attrs = df.attrs
                try:
                    sigs = strategy.scan(bar_df, strategy.default_params)
                    if sigs:
                        count += len(sigs)
                        if len(sample_signals[strategy_id]) < 3:
                            s = sigs[0]
                            sample_signals[strategy_id].append(
                                f"  {symbol} @ {df.index[i].date()} "
                                f"entry={s.entry:.2f} sl={s.sl:.2f} rr={s.rr:.2f}"
                            )
                except Exception:
                    pass
            if count:
                fire_count[strategy_id][symbol] = count

    print("=" * 70)
    print(f"  {'STRATEGY':<20} {'TOTAL SIGNALS':>14} {'SYMBOLS HIT':>12} {'AVG/SYMBOL':>12}")
    print("  " + "-" * 66)
    for sid in strategies_map:
        counts = fire_count[sid]
        total = sum(counts.values())
        n_syms = len(counts)
        avg = total / n_syms if n_syms else 0
        flag = "" if total >= 30 else "  ← TOO FEW"
        print(f"  {sid:<20} {total:>14} {n_syms:>12} {avg:>11.1f}x{flag}")
        for line in sample_signals[sid]:
            print(f"    SAMPLE: {line}")
    print("=" * 70)
    print()
    print("GUIDE:")
    print("  < 10 total → strategy barely fires. Loosen conditions.")
    print("  10-50      → fires but rare. OK for low-frequency strategies.")
    print("  50+        → healthy signal flow. Ready to optimise.")
    print()


def cmd_optimise(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    from db.models import SessionLocal, StrategyParamsModel

    market = adapter.market_id
    today = date.today()
    history_start = today - timedelta(days=365 * 5)

    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    strategies_map = StrategyRegistry.for_market(market)

    logger.info("=== %s optimise | %s | symbols=%d strategies=%d ===",
                market.upper(), today, len(universe), len(strategies_map))

    # Fetch benchmark once — used for RSM calculation across all stocks
    bm_close = _fetch_benchmark(adapter, history_start, today)

    db = SessionLocal()
    market_dfs = []

    for idx, symbol in enumerate(universe, start=1):
        logger.info("[%d/%d] fetching %s (5yr history)", idx, len(universe), symbol)
        df = adapter.ohlcv(symbol, history_start, today)
        df = _attach_benchmark(df, bm_close)

        if df.empty:
            logger.warning("  %s — no data returned, skipping", symbol)
            continue
        if len(df) < 300:
            logger.warning("  %s — only %d bars (need 300+), skipping", symbol, len(df))
            continue

        logger.info("  %s — %d bars (%s → %s)", symbol, len(df),
                    df.index[0].date(), df.index[-1].date())
        df.attrs = {"symbol": symbol, "market": market}
        market_dfs.append(df)

    if not market_dfs:
        db.close()
        logger.warning("=== no eligible symbols for %s optimise ===", market.upper())
        return

    logger.info("=== %s optimise sample | eligible symbols=%d/%d ===",
                market.upper(), len(market_dfs), len(universe))

    total_strategies = len(strategies_map)
    strategy_items = list(strategies_map.items())
    outer_jobs = _strategy_job_count(args, len(strategy_items))
    logger.info(
        "=== %s optimise compute | strategy_jobs=%d param_jobs=1 ===",
        market.upper(), outer_jobs,
    )

    results = Parallel(n_jobs=outer_jobs)(
        delayed(_optimise_strategy_task)(market, strategy_id, strategy_cls, market_dfs, args.capital, "optimise")
        for strategy_id, strategy_cls in strategy_items
    )

    for idx, result in enumerate(results, start=1):
        strategy_id = result["strategy_id"]
        logger.info("[%d/%d] optimised %s across %d symbols",
                    idx, total_strategies, strategy_id, len(market_dfs))

        opt = result["opt"]
        if result["status"] == "no_windows":
            logger.warning("  → no valid windows, skipping DB write")
            continue

        consistency = result["consistency"]
        m = result["metrics"]
        is_live = result["is_live"]
        strategy = strategies_map[strategy_id]()
        yearly_summary = _build_yearly_summary(market_dfs, strategy, opt["best_params"], args.capital, today)

        logger.info(
            "  → score=%.3f sharpe=%.2f calmar=%.2f pf=%.2f wr=%.0f%% trades=%d symbols=%d/%d profitable=%.0f%% | "
            "consistency=%s | gate=%s | is_live=%s",
            opt.get("best_score", 0),
            m.get("sharpe", 0), m.get("calmar", 0),
            m.get("profit_factor", 0), (m.get("win_rate", 0) or 0) * 100,
            m.get("trade_count", 0),
            m.get("traded_symbol_count", 0), m.get("sampled_symbol_count", 0),
            (m.get("profitable_symbol_rate", 0) or 0) * 100,
            "PASS" if consistency["pass"] else f"FAIL({consistency.get('reason', '')})",
            opt["status"].upper(),
            "YES" if is_live else "NO",
        )
        if not consistency["pass"]:
            for key, detail in consistency.get("details", {}).items():
                logger.info(
                    "     drift check %s: 2yr=%.2f 1yr=%.2f ratio=%.0f%%",
                    key, detail["full_2yr"], detail["recent_1yr"], detail["ratio"] * 100,
                )

        row = (
            db.query(StrategyParamsModel)
            .filter_by(market=market, strategy=strategy_id)
            .first()
        )
        if not row:
            row = StrategyParamsModel(market=market, strategy=strategy_id)
            db.add(row)

        new_score = opt.get("best_score") or 0
        row.params = opt["best_params"]
        row.backtest_score = new_score
        row.backtest_annual_return = m.get("annual_return")
        row.backtest_sharpe = m.get("sharpe")
        row.backtest_calmar = m.get("calmar")
        row.backtest_pf = m.get("profit_factor")
        row.backtest_winrate = m.get("win_rate")
        row.backtest_trade_count = m.get("trade_count")
        row.backtest_avg_win = m.get("avg_win")
        row.backtest_avg_loss = m.get("avg_loss")
        row.backtest_max_dd = m.get("max_drawdown")
        row.yearly_summary = yearly_summary
        row.consistency_pass = consistency["pass"]
        row.is_live = is_live
        db.commit()
        logger.info("  → saved to DB (score=%.3f is_live=%s)", new_score, is_live)

    db.close()
    logger.info("=== optimisation complete | strategies=%d symbols=%d ===", total_strategies, len(market_dfs))

    _print_report(market, universe, strategies_map)


def _cmd_optimise_mode(adapter: MarketAdapter, args: argparse.Namespace, command: str) -> None:
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    from db.models import SessionLocal, StrategyParamsModel

    market = adapter.market_id
    today = date.today()
    history_start = today - timedelta(days=365 * 3)

    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    strategies_map = StrategyRegistry.for_market(market)

    logger.info("=== %s %s | %s | symbols=%d strategies=%d ===",
                market.upper(), command, today, len(universe), len(strategies_map))

    bm_close = _fetch_benchmark(adapter, history_start, today)

    db = SessionLocal()
    saved_params_map = _load_saved_params_map(db, market)
    market_dfs = []

    for idx, symbol in enumerate(universe, start=1):
        logger.info("[%d/%d] fetching %s (5yr history)", idx, len(universe), symbol)
        df = adapter.ohlcv(symbol, history_start, today)
        df = _attach_benchmark(df, bm_close)

        if df.empty:
            logger.warning("  %s — no data returned, skipping", symbol)
            continue
        if len(df) < 60:
            logger.warning("  %s — only %d bars (need 60+), skipping", symbol, len(df))
            continue

        logger.info("  %s — %d bars (%s → %s)", symbol, len(df),
                    df.index[0].date(), df.index[-1].date())
        df.attrs = {"symbol": symbol, "market": market}
        market_dfs.append(df)

    if not market_dfs:
        db.close()
        logger.warning("=== no eligible symbols for %s %s ===", market.upper(), command)
        return

    logger.info("=== %s %s sample | eligible symbols=%d/%d ===",
                market.upper(), command, len(market_dfs), len(universe))

    total_strategies = len(strategies_map)
    strategy_items = list(strategies_map.items())
    outer_jobs = _strategy_job_count(args, len(strategy_items))
    logger.info(
        "=== %s %s compute | strategy_jobs=%d param_jobs=1 ===",
        market.upper(), command, outer_jobs,
    )

    results = Parallel(n_jobs=outer_jobs)(
        delayed(_optimise_strategy_task)(
            market,
            strategy_id,
            strategy_cls,
            market_dfs,
            args.capital,
            command,
            saved_params_map.get(strategy_id),
        )
        for strategy_id, strategy_cls in strategy_items
    )

    for idx, result in enumerate(results, start=1):
        strategy_id = result["strategy_id"]
        logger.info("[%d/%d] %s %s across %d symbols",
                    idx, total_strategies, command, strategy_id, len(market_dfs))

        opt = result["opt"]
        if result["status"] == "no_windows":
            logger.warning("  → no valid windows, skipping DB write")
            continue

        consistency = result["consistency"]
        m = result["metrics"]
        is_live = result["is_live"]
        strategy = strategies_map[strategy_id]()
        yearly_summary = _build_yearly_summary(market_dfs, strategy, opt["best_params"], args.capital, today)

        logger.info(
            "  → mode=%s score=%.3f sharpe=%.2f calmar=%.2f pf=%.2f wr=%.0f%% trades=%d symbols=%d/%d profitable=%.0f%% | consistency=%s | gate=%s | is_live=%s",
            result["mode"],
            opt.get("best_score", 0),
            m.get("sharpe", 0), m.get("calmar", 0),
            m.get("profit_factor", 0), (m.get("win_rate", 0) or 0) * 100,
            m.get("trade_count", 0),
            m.get("traded_symbol_count", 0), m.get("sampled_symbol_count", 0),
            (m.get("profitable_symbol_rate", 0) or 0) * 100,
            "PASS" if consistency["pass"] else f"FAIL({consistency.get('reason', '')})",
            opt["status"].upper(),
            "YES" if is_live else "NO",
        )

        row = (
            db.query(StrategyParamsModel)
            .filter_by(market=market, strategy=strategy_id)
            .first()
        )
        if not row:
            row = StrategyParamsModel(market=market, strategy=strategy_id)
            db.add(row)

        new_score = opt.get("best_score") or 0
        row.params = opt["best_params"]
        row.backtest_score = new_score
        row.backtest_annual_return = m.get("annual_return")
        row.backtest_sharpe = m.get("sharpe")
        row.backtest_calmar = m.get("calmar")
        row.backtest_pf = m.get("profit_factor")
        row.backtest_winrate = m.get("win_rate")
        row.backtest_trade_count = m.get("trade_count")
        row.backtest_avg_win = m.get("avg_win")
        row.backtest_avg_loss = m.get("avg_loss")
        row.backtest_max_dd = m.get("max_drawdown")
        row.yearly_summary = yearly_summary
        row.consistency_pass = consistency["pass"]
        row.is_live = is_live
        db.commit()
        logger.info("  → saved to DB (score=%.3f is_live=%s)", new_score, is_live)

    db.close()
    logger.info("=== %s complete | mode=%s strategies=%d symbols=%d ===",
                market.upper(), command, total_strategies, len(market_dfs))

    _print_report(market, universe, strategies_map)


def cmd_optimise_filter(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    _cmd_optimise_mode(adapter, args, "optimise-filter")


def cmd_optimise_risk(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    _cmd_optimise_mode(adapter, args, "optimise-risk")


def _print_report(market: str, universe: list, strategies_map: dict) -> None:
    import strategies as _strats  # noqa: F401
    from db.models import SessionLocal as _SL, StrategyParamsModel as _SP
    from core.registry import StrategyRegistry as _SR
    from config import OPTIMIZER_OBJECTIVE, SELECTION_GATE
    from datetime import date as _date

    _db = _SL()
    row_query = _db.query(_SP).filter_by(market=market)
    if str(OPTIMIZER_OBJECTIVE or "").strip().lower() == "annual_return":
        rows = row_query.order_by(_SP.backtest_annual_return.desc(), _SP.backtest_score.desc()).all()
    else:
        rows = row_query.order_by(_SP.backtest_score.desc(), _SP.backtest_annual_return.desc()).all()
    _db.close()

    _strat_map = _SR.for_market(market)
    g = SELECTION_GATE
    W = 156

    def _param_delta(optimised: dict, defaults: dict, keys: list[str]) -> str:
        parts = []
        for k in keys:
            if k not in optimised:
                continue
            ov = optimised[k]
            dv = defaults.get(k)
            arrow = ""
            if isinstance(ov, (int, float)) and isinstance(dv, (int, float)):
                if abs(ov - dv) > 1e-9:
                    arrow = " ↑" if ov > dv else " ↓"
            parts.append(f"{k}={ov}{arrow}")
        return "  ".join(parts)

    def _flag(ok: bool) -> str:
        return "✓" if ok else "✗"

    def _fmt_pct(value: float | None, *, signed: bool = False, scale: float = 100.0) -> str:
        number = float(value or 0.0) * scale
        return f"{number:+.1f}%" if signed else f"{number:.1f}%"

    def _fmt_num(value: float | None, digits: int = 2) -> str:
        return f"{float(value or 0.0):.{digits}f}"

    def _status_label(row) -> str:
        if row.is_live:
            return "LIVE"
        if row.consistency_pass is False:
            return "CONS"
        if row.consistency_pass is True:
            return "GATE"
        return "HOLD"

    def _year_metrics(row, key: str) -> dict:
        summary = row.yearly_summary or {}
        return summary.get(key, {}) if isinstance(summary, dict) else {}

    def _year_cell(row, key: str) -> str:
        annual_return = _year_metrics(row, key).get("annual_return")
        return _fmt_pct(annual_return, signed=True) if annual_return is not None else "   N/A"

    def _year_detail(row, key: str) -> str:
        metrics = _year_metrics(row, key)
        annual_return = metrics.get("annual_return")
        if annual_return is None:
            return f"{key.upper()} N/A"
        max_dd = metrics.get("max_drawdown")
        trades = int(metrics.get("trade_count", 0) or 0)
        win_rate = metrics.get("win_rate")
        wr_str = _fmt_pct(win_rate) if win_rate is not None else "N/A"
        return (
            f"{key.upper()} {_fmt_pct(annual_return, signed=True)} | "
            f"DD {_fmt_pct(max_dd)} | Tr {trades} | WR {wr_str}"
        )

    print("\n" + "=" * W)
    print(f"  {market.upper()} OPTIMISE REPORT — {_date.today()}")
    if universe:
        print(f"  Universe: {len(universe)} symbols  |  Strategies: {len(strategies_map)}  |  Combos: {len(universe) * len(strategies_map)}")
    else:
        print(f"  Strategies in DB: {len(rows)}  (reading saved results)")
    print("  Metrics: shared params per strategy, latest OOS window, median across sampled symbols")
    print(f"  Optimiser objective: {OPTIMIZER_OBJECTIVE}")
    print("=" * W)

    def _trend_label(params: dict) -> str:
        trend_filter = params.get("trend_filter")
        if trend_filter is not None:
            raw = str(trend_filter).strip()
            if raw in ("", "0", "off", "none"):
                return "no trend filter"
            if "_" in raw:
                return " + ".join(f"SMA{part}" for part in raw.split("_")) + " uptrend filter"
            return f"SMA{raw} uptrend filter"

        trend_p = int(params.get("trend_sma_period", 0) or 0)
        return f"SMA{trend_p} uptrend filter" if trend_p else "no trend filter"

    def _short_trend_label(params: dict) -> str:
        trend_filter = params.get("trend_filter")
        if trend_filter is not None:
            raw = str(trend_filter).strip()
            if raw in ("", "0", "off", "none"):
                return "off"
            if "_" in raw:
                return "+".join(f"S{part}" for part in raw.split("_"))
            return f"S{raw}"

        trend_p = int(params.get("trend_sma_period", 0) or 0)
        return f"S{trend_p}" if trend_p else "off"

    def _filter_summary(params: dict, *, include_trend: bool = True) -> str:
        bits = []
        if include_trend:
            bits.append(f"Trend={_short_trend_label(params)}")
        if "rvol_min" in params:
            bits.append(f"RVol>={params['rvol_min']}")
        elif "rvol_max_on_pullback" in params:
            bits.append(f"RVol<={params['rvol_max_on_pullback']}")
        if "rsm_min" in params and float(params.get("rsm_min", 0) or 0) > 0:
            bits.append(f"RSM>={int(params['rsm_min'])}")
        else:
            bits.append("RSM=off")
        return " | ".join(bits)

    def _exit_summary(params: dict) -> str:
        ema_period = int(params.get("ema_exit_period", 0) or 0)
        be_after_bars = int(params.get("be_after_bars", 0) or 0)
        be_trigger_atr = float(params.get("be_trigger_atr_mult", 0) or 0)
        max_bars = int(params.get("max_bars", 0) or 0)
        hard_stop_mode = str(params.get("hard_stop_mode", "both") or "both").lower()
        be_label = f"BE {be_after_bars} bars" if be_after_bars else (f"BE {be_trigger_atr:g} ATR" if be_trigger_atr else "BE off")
        ema_label = "EMA10" if hard_stop_mode == "ema10" else (f"EMA{ema_period}" if ema_period else "EMA off")
        parts = [
            f"SL {params.get('sl_atr_mult', '?')} ATR | "
            f"TP1 {params.get('tp1_atr_mult', '?')} ATR @ {int((params.get('tp1_partial_pct', 0.5) or 0) * 100)}% | "
            f"TP2 {params.get('tp2_atr_mult', '?')} ATR @ {int((params.get('tp2_partial_pct', 1.0) or 0) * 100)}% | "
            f"{be_label}"
        ]
        if hard_stop_mode in ("both", "trail"):
            parts.append(f"Trail {params.get('trail_atr_mult', '?')} ATR")
        if hard_stop_mode in ("both", "ema10"):
            parts.append(ema_label)
        if hard_stop_mode in ("trail", "ema10"):
            parts.append(f"HardStop {hard_stop_mode.upper()}")
        if max_bars > 0:
            parts.append(f"TimeStop {max_bars} bars")
        parts.append(f"Risk {_fmt_pct(params.get('risk_pct', 0), scale=100.0)}")
        return " | ".join(parts)

    leaderboard = []
    for row in rows:
        params = row.params or {}
        leaderboard.append({
            "row": row,
            "strategy": row.strategy,
            "status": _status_label(row),
            "ret": float(row.backtest_annual_return or 0.0),
            "dd": float(row.backtest_max_dd or 0.0),
            "trades": int(row.backtest_trade_count or 0),
            "pf": float(row.backtest_pf or 0.0),
            "wr": float(row.backtest_winrate or 0.0),
            "score": float(row.backtest_score or 0.0),
            "y1": _year_cell(row, "y1"),
            "y2": _year_cell(row, "y2"),
            "y3": _year_cell(row, "y3"),
            "filters": _filter_summary(params),
            "exits": _exit_summary(params),
        })

    print("  LEADERBOARD")
    print("  " + "-" * (W - 4))
    print(
        "  "
        f"{'#':>2}  {'Strategy':<20} {'St':<5} {'Ret':>8} {'DD':>7} {'Tr':>5} {'PF':>5} {'WR':>5} {'Score':>7} {'Y1':>8} {'Y2':>8} {'Y3':>8}  Filters"
    )
    print("  " + "-" * (W - 4))

    live_count = 0
    for idx, item in enumerate(leaderboard, start=1):
        row = item["row"]
        if row.is_live:
            live_count += 1
        print(
            "  "
            f"{idx:>2}  {item['strategy']:<20} {item['status']:<5} "
            f"{_fmt_pct(item['ret'], signed=True):>8} "
            f"{_fmt_pct(item['dd']):>7} "
            f"{item['trades']:>5d} "
            f"{_fmt_num(item['pf']):>5} "
            f"{_fmt_pct(item['wr']):>5} "
            f"{item['score']:>7.2f} "
            f"{item['y1']:>8} {item['y2']:>8} {item['y3']:>8}  {item['filters']}"
        )

    print("  " + "-" * (W - 4))
    print(
        "  Gate: "
        f"Ret≥{g['min_annual_return']*100:.0f}%  Sharpe≥{g['min_sharpe']}  Calmar≥{g['min_calmar']}  "
        f"PF≥{g['min_profit_factor']}  WR≥{g['min_win_rate']*100:.0f}%  Trades≥{g['min_trades']}"
    )

    print("\n  DETAILS")
    print("  " + "-" * (W - 4))

    for item in leaderboard:
        r = item["row"]
        p = r.params or {}
        defaults = _strat_map[r.strategy]().default_params if r.strategy in _strat_map else {}

        if r.is_live:
            status = "LIVE ✓"
        elif r.consistency_pass is False:
            status = "not live — consistency fail"
        elif r.consistency_pass is True:
            status = "not live — gate fail"
        else:
            status = "not live"

        sl    = p.get("sl_atr_mult")
        tp1   = p.get("tp1_atr_mult")
        tp2   = p.get("tp2_atr_mult")
        trail = p.get("trail_atr_mult")
        ema_p = p.get("ema_exit_period", 0)
        rr_raw = round(tp1 / sl, 2) if tp1 and sl else "?"
        risk_pct = p.get("risk_pct", 0)
        ema_str = f"EMA{ema_p}" if ema_p else "off"

        ann_ret   = (r.backtest_annual_return or 0) * 100
        max_dd    = (r.backtest_max_dd or 0) * 100
        ann_ok    = (r.backtest_annual_return or 0) >= g["min_annual_return"]
        sharpe_ok = (r.backtest_sharpe  or 0) >= g["min_sharpe"]
        calmar_ok = (r.backtest_calmar  or 0) >= g["min_calmar"]
        pf_ok     = (r.backtest_pf      or 0) >= g["min_profit_factor"]
        wr_ok     = (r.backtest_winrate or 0) >= g["min_win_rate"]

        print(f"\n  ┌─ {r.strategy.upper()}  [{status}]")
        print(f"  │  Return sheet : Ret {ann_ret:+.1f}%{_flag(ann_ok)}  DD {max_dd:.1f}%  Trades {r.backtest_trade_count or 0}  Score {r.backtest_score or 0:.2f}")
        print(f"  │  Year view     : {_year_detail(r, 'y1')}  ||  {_year_detail(r, 'y2')}  ||  {_year_detail(r, 'y3')}")
        print(f"  │  Quality      : Sharpe {r.backtest_sharpe or 0:.2f}{_flag(sharpe_ok)}  Calmar {r.backtest_calmar or 0:.2f}{_flag(calmar_ok)}  PF {r.backtest_pf or 0:.2f}{_flag(pf_ok)}  WR {((r.backtest_winrate or 0)*100):.0f}%{_flag(wr_ok)}")
        print(f"  │  Trade stats  : AvgWin {(r.backtest_avg_win or 0) * 100:+.1f}%  AvgLoss {(r.backtest_avg_loss or 0) * 100:+.1f}%  Win/Loss {abs(r.backtest_avg_win or 0) / abs(r.backtest_avg_loss or 1):.2f}x")

        if p:
            sig_keys = ["nr_period", "atr_pct_max", "psth", "lookback",
                        "bb_period", "bb_std", "kc_mult", "kc_period",
                        "fast_period", "slow_period", "trend_period",
                        "rsi_threshold", "consec_down_days", "rvol_min",
                        "pullback_atr_band", "rvol_max_on_pullback", "body_pct_min",
                        "rsm_min", "trend_filter", "trend_sma_period"]
            sig_str = _param_delta(p, defaults, sig_keys)
            if sig_str:
                print(f"  │  Signal config : {sig_str}")

            trend_str = _trend_label(p)
            print(f"  │  Filters      : {trend_str}  |  {_filter_summary(p, include_trend=False)}")
            print(f"  │  Exit plan    : {_exit_summary(p)}  |  Raw RR {rr_raw}:1")

            changed = []
            for k, ov in p.items():
                dv = defaults.get(k)
                if dv is not None and isinstance(ov, (int, float)) and isinstance(dv, (int, float)):
                    if abs(ov - dv) > 1e-9:
                        changed.append(f"{k}: {dv}→{ov}")
            if changed:
                print(f"  │  Optimizer delta: {', '.join(changed)}")

        print(f"  └{'─' * (W - 4)}")

    print("\n" + "=" * W)
    print(f"  RESULT: {live_count} / {len(rows)} strategies approved for live trading")
    print("=" * W + "\n")


def cmd_paper(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    import strategies  # noqa: F401
    from core.ledger import PortfolioLedger
    from core.paper_trade import PaperTrader
    from core.guard import apply_lookahead_guard
    from core.registry import StrategyRegistry

    market = adapter.market_id
    today = date.today()
    start = today - timedelta(days=90)

    ledger = PortfolioLedger()
    trader = PaperTrader(capital=args.capital, ledger=ledger)
    strategies_map = StrategyRegistry.for_market(market)

    for symbol in adapter.universe(today, top_n=getattr(args, "symbols", None)):
        df = adapter.ohlcv(symbol, start, today)
        if df.empty or len(df) < 60:
            continue
        df.attrs = {"symbol": symbol, "market": market}

        for i in range(50, len(df)):
            bar_df = df.iloc[: i + 1].copy()
            bar_df.attrs = df.attrs
            bar = df.iloc[i]
            bar_date = bar.name.date()
            bar_dict = {
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
            }
            trader.process_bar(bar_dict, bar_date)

            for strategy_id, strategy_cls in strategies_map.items():
                strategy = strategy_cls()
                params = _load_live_params(market, strategy_id)
                try:
                    signals = strategy.scan(bar_df, params)
                    for sig in signals:
                        trader.submit_signal(sig, bar_date)
                except Exception as exc:
                    logger.debug("paper signal error %s: %s", symbol, exc)

    summary = ledger.pnl_summary()
    logger.info("paper trading summary: %s", json.dumps(summary, indent=2))


def cmd_report(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Print the last optimise result from DB — no recomputation."""
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    strategies_map = StrategyRegistry.for_market(adapter.market_id)
    universe_hint: list = []  # report doesn't need live universe
    _print_report(adapter.market_id, universe_hint, strategies_map)


def cmd_quick_report(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    from validation.backtest import run_portfolio_backtest

    market = adapter.market_id
    today = date.today()

    y1_end,  y1_start = today,    today - timedelta(days=365)
    y2_end,  y2_start = y1_start, today - timedelta(days=730)
    y3_end,  y3_start = y2_start, today - timedelta(days=1095)

    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    strategies_map = StrategyRegistry.for_market(market)

    logger.info("=== %s quick-report | %s | symbols=%d strategies=%d ===",
                market.upper(), today, len(universe), len(strategies_map))

    bm_close = _fetch_benchmark(adapter, y3_start, today)

    all_dfs = []
    for idx, symbol in enumerate(universe, start=1):
        logger.info("[%d/%d] fetching %s (3yr history)", idx, len(universe), symbol)
        df = adapter.ohlcv(symbol, y3_start, today)
        df = _attach_benchmark(df, bm_close)
        if df.empty or len(df) < 60:
            continue
        df.attrs = {"symbol": symbol, "market": market}
        all_dfs.append(df)

    if not all_dfs:
        logger.warning("=== no eligible symbols for %s quick-report ===", market.upper())
        return

    windows = [
        (f"Y1 {y1_start}>{y1_end}", _slice_dfs(all_dfs, y1_start, y1_end)),
        (f"Y2 {y2_start}>{y2_end}", _slice_dfs(all_dfs, y2_start, y2_end)),
        (f"Y3 {y3_start}>{y3_end}", _slice_dfs(all_dfs, y3_start, y3_end)),
    ]

    W = 110
    STRAT_W = 22

    print("\n" + "=" * W)
    print(f"  {market.upper()} QUICK REPORT — {today}")
    print(f"  Universe: {len(all_dfs)} symbols  |  Strategies: {len(strategies_map)}")
    print(f"  Entry: strategy signal  |  Exit: close<EMA10 OR SL(1.5xATR)  |  No TP / No Partial / No Trail")
    print("=" * W)

    for phase_label, phase_overrides in _LEARN_PHASES:
        phase_params = {**_LEARN_BASE, **phase_overrides}
        print(f"\n  -- {phase_label} --")

        wl_line = "  " + " " * STRAT_W
        for wl, _ in windows:
            wl_line += f"  {wl:<28}"
        print(wl_line)

        sub_hdr = "  " + f"{'Strategy':<{STRAT_W}}"
        for _ in windows:
            sub_hdr += f"  {'Ret':>7} {'DD':>6} {'Tr':>4} {'WR':>4}"
        print(sub_hdr)
        print("  " + "-" * (W - 4))

        for strategy_id, strategy_cls in strategies_map.items():
            strategy = strategy_cls()
            params = {**strategy.default_params, **phase_params}
            row = f"  {strategy_id:<{STRAT_W}}"
            for _, wdfs in windows:
                if wdfs:
                    agg = run_portfolio_backtest(wdfs, strategy, params, initial_capital=args.capital)
                    ret = agg.get("annual_return", 0.0) * 100
                    dd  = agg.get("max_drawdown", 0.0) * 100
                    tr  = agg.get("trade_count", 0)
                    wr  = agg.get("win_rate", 0.0) * 100
                    row += f"  {ret:>+7.1f} {dd:>6.1f} {tr:>4d} {wr:>4.0f}%"
                else:
                    row += f"  {'N/A':>7} {'N/A':>6} {'N/A':>4} {'N/A':>4}"
            print(row)

    print("\n" + "=" * W)
    print("  SMA filter (phase 2+) affects: pivot_breakout, trendline_breakout, pullback_buy, narrow_range, bb_squeeze")
    print("  RVol filter (phase 3+) affects: pivot_breakout, ma_cross, reversal, trendline_breakout, narrow_range")
    print("  RSM filter  (phase 4+) affects: all strategies (skipped for crypto/commodity)")
    print("  TP/BE    (phase 5)   : SL=1xATR  TP1=2xATR@30%  TP2=4xATR@30%  BE after 3 bars")
    print("=" * W + "\n")


def make_parser(market_id: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{market_id.upper()} market pipeline")
    parser.add_argument("command",
                        choices=["scan", "diagnose", "optimise", "optimise-filter", "optimise-risk", "paper", "validate", "live", "report", "quick-report"])
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--symbols", type=int,
                        help="Limit to top N symbols after turnover filtering (default: all)")
    return parser


def run(adapter: MarketAdapter, command: str, args: argparse.Namespace) -> None:
    dispatch = {
        "scan":     lambda: cmd_scan(adapter, args),
        "diagnose": lambda: cmd_diagnose(adapter, args),
        "optimise": lambda: cmd_optimise(adapter, args),
        "optimise-filter": lambda: cmd_optimise_filter(adapter, args),
        "optimise-risk": lambda: cmd_optimise_risk(adapter, args),
        "paper":    lambda: cmd_paper(adapter, args),
        "validate": lambda: (cmd_optimise(adapter, args), cmd_paper(adapter, args)),
        "live":     lambda: cmd_scan(adapter, args),
        "report":   lambda: cmd_report(adapter, args),
        "quick-report": lambda: cmd_quick_report(adapter, args),
    }
    dispatch.get(command, lambda: cmd_scan(adapter, args))()
