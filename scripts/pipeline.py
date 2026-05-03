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

_REGIME_DISCOVERY_PARAMS = {
    "sl_atr_mult": 1.5,
    "tp1_atr_mult": 3.0,
    "tp1_partial_pct": 1.0,   # full exit at TP1 — single TP, pure 2:1 RR test
    "tp2_atr_mult": 999.0,
    "tp2_partial_pct": 0.0,
    "trail_atr_mult": 999.0,
    "be_trigger_atr_mult": 999.0,
    "ema_exit_period": 0,     # no EMA exit — pure SL/TP only
    "hard_stop_mode": "trail",
    "be_after_bars": 0,
    "max_bars": 0,
    "risk_pct": 0.005,
}

_QO_TRIAGE_PARAMS = {
    "sl_atr_mult": 1.5,
    "ema_exit_period": 10,
    "ema_exit_always": True,
    "hard_stop_mode": "ema10",
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

_QO_BASE_PARAMS = {
    "tp1_atr_mult": 2.0,
    "tp1_partial_pct": 0.3,
    "tp2_atr_mult": 4.0,
    "tp2_partial_pct": 0.3,
    "trail_atr_mult": 3.0,
    "ema_exit_period": 10,
    "be_after_bars": 3,
    "be_trigger_atr_mult": 999.0,
    "risk_pct": 0.005,
}

_QO_PARAM_SPACE = {
    "trend_filter": [0, 50, 200],
    "rvol_min": [1.2, 1.5, 2.0],
    "sl_atr_mult": [1.0, 1.5],
    "hard_stop_mode": ["ema10"],
    "str_max": [0, 4],
    "rsm_min": [0, 75],
}

_QO_SURVIVAL_GATES = {
    "min_trades": 30,
    "min_profit_factor": 1.25,
    "max_drawdown": 0.15,
    "min_calmar": 1.0,
}

_QO_TRIAGE_DD_CUTOFF = 0.30
_QO_TARGET_PASS_RET = 0.0
_QO_TARGET_PASS_DD = 0.20


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


def _strategy_job_count(args: argparse.Namespace, total_strategies: int) -> int:
    requested = max(int(getattr(args, "strategy_jobs", 1) or 1), 1)
    return min(requested, total_strategies)


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


def _qr_eval_strategy(strategy_cls, phase_params: dict, windows: list, capital: float) -> list:
    """Evaluate one strategy across all windows for one phase. Module-level for joblib pickling."""
    from validation.backtest import run_portfolio_backtest
    strategy = strategy_cls()
    params = {**strategy.default_params, **phase_params}
    results = []
    for _, wdfs in windows:
        if wdfs:
            results.append(run_portfolio_backtest(wdfs, strategy, params, initial_capital=capital))
        else:
            results.append(None)
    return results


def _qo_eval_combo(strategy_cls, combo: dict, base_params: dict, dfs: list, capital: float) -> dict:
    """Module-level for joblib. Evaluate one grid combo on IS dfs."""
    from validation.backtest import run_portfolio_backtest
    strategy = strategy_cls()
    params = {**strategy.default_params, **base_params, **combo}
    try:
        m = run_portfolio_backtest(dfs, strategy, params, initial_capital=capital)
        m["params"] = params
        return m
    except Exception:
        return {
            "trade_count": 0, "params": params, "annual_return": 0.0,
            "max_drawdown": 0.0, "calmar": 0.0, "profit_factor": 0.0, "win_rate": 0.0,
        }


def _qo_gate_checks(metrics: dict) -> dict[str, bool]:
    g = _QO_SURVIVAL_GATES
    return {
        "trades": metrics.get("trade_count", 0) >= g["min_trades"],
        "profit_factor": metrics.get("profit_factor", 0.0) >= g["min_profit_factor"],
        "drawdown": (metrics.get("max_drawdown", 0.0) or 0.0) <= g["max_drawdown"],
        "calmar": (metrics.get("calmar", 0.0) or 0.0) >= g["min_calmar"],
    }


def _qo_rank_key(entry: dict) -> tuple:
    m = entry["metrics"]
    return (
        int(entry.get("gate_hits", 0)),
        float(m.get("annual_return", 0.0) or 0.0),
        float(m.get("calmar", 0.0) or 0.0),
        float(m.get("profit_factor", 0.0) or 0.0),
        float(m.get("win_rate", 0.0) or 0.0),
        -float(m.get("max_drawdown", 0.0) or 0.0),
        int(m.get("trade_count", 0) or 0),
    )


def _qo_trend_label(params: dict) -> str:
    trend = params.get("trend_filter", params.get("trend_sma_period", 0))
    return str(trend or "off")


def _save_optimise_candidates(market: str, candidates: list[dict]) -> None:
    if not candidates:
        return

    from db.models import SessionLocal, StrategyCandidateModel

    db = SessionLocal()
    try:
        db.query(StrategyCandidateModel).filter_by(market=market).delete()
        for candidate in candidates:
            is_metrics = candidate["is_metrics"]
            oos_metrics = candidate["oos_metrics"]
            db.add(
                StrategyCandidateModel(
                    market=market,
                    strategy=candidate["strategy_id"],
                    candidate_source=candidate["source"],
                    candidate_status="tradable" if candidate["oos_pass"] else "watchlist",
                    params=candidate["params"],
                    gate_hits=int(candidate.get("gate_hits", 0)),
                    gate_misses=list(candidate.get("gate_misses", [])),
                    is_annual_return=is_metrics.get("annual_return"),
                    is_calmar=is_metrics.get("calmar"),
                    is_profit_factor=is_metrics.get("profit_factor"),
                    is_win_rate=is_metrics.get("win_rate"),
                    is_trade_count=is_metrics.get("trade_count"),
                    is_max_drawdown=is_metrics.get("max_drawdown"),
                    oos_annual_return=oos_metrics.get("annual_return"),
                    oos_calmar=oos_metrics.get("calmar"),
                    oos_profit_factor=oos_metrics.get("profit_factor"),
                    oos_win_rate=oos_metrics.get("win_rate"),
                    oos_trade_count=oos_metrics.get("trade_count"),
                    oos_max_drawdown=oos_metrics.get("max_drawdown"),
                    oos_pass=bool(candidate["oos_pass"]),
                )
            )
        db.commit()
        logger.info("saved %d optimise candidates for %s", len(candidates), market)
    finally:
        db.close()


def cmd_optimise(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    from sklearn.model_selection import ParameterGrid
    from validation.backtest import run_portfolio_backtest

    market = adapter.market_id
    today = date.today()

    y1_end,  y1_start = today,    today - timedelta(days=365)
    y2_end,  y2_start = y1_start, today - timedelta(days=730)
    y3_end,  y3_start = y2_start, today - timedelta(days=1095)

    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    strategies_map = StrategyRegistry.for_market(market)

    logger.info("=== %s optimise | %s | symbols=%d strategies=%d ===",
                market.upper(), today, len(universe), len(strategies_map))
    logger.info("    IS (Y2+Y3): %s -> %s  |  OOS vault (Y1): %s -> %s",
                y3_start, y1_start, y1_start, y1_end)

    bm_close = _fetch_benchmark(adapter, y3_start, today)

    all_dfs = []
    for idx, symbol in enumerate(universe, start=1):
        logger.info("[%d/%d] fetching %s (3yr)", idx, len(universe), symbol)
        df = adapter.ohlcv(symbol, y3_start, today)
        df = _attach_benchmark(df, bm_close)
        if df.empty or len(df) < 60:
            continue
        df.attrs = {"symbol": symbol, "market": market}
        all_dfs.append(df)

    if not all_dfs:
        logger.warning("=== no eligible symbols for %s optimise ===", market.upper())
        return

    from validation.backtest import _precompute_indicators
    precomputed = []
    for df in all_dfs:
        saved = df.attrs
        pdf = _precompute_indicators(df)
        pdf.attrs = saved
        precomputed.append(pdf)

    oos_dfs    = _slice_dfs(precomputed, y1_start, y1_end)   # Y1 — held-out OOS vault
    y2_dfs     = _slice_dfs(precomputed, y2_start, y2_end)
    y3_dfs     = _slice_dfs(precomputed, y3_start, y3_end)
    is_dfs     = _slice_dfs(precomputed, y3_start, y1_start)  # Y2+Y3 — in-sample fit

    if not is_dfs:
        logger.warning("=== no eligible IS symbols for %s optimise ===", market.upper())
        return

    W = 128
    full_grid = list(ParameterGrid(_QO_PARAM_SPACE))
    strategy_items = list(strategies_map.items())
    strategy_jobs = _strategy_job_count(args, len(strategy_items))
    combo_jobs = max(int(getattr(args, "strategy_jobs", 1) or 1), 1)

    print("\n" + "=" * W)
    print(f"  {market.upper()} OPTIMISE - {today}")
    print(f"  Universe: {len(all_dfs)} symbols  |  IS (Y2+Y3): {y3_start} -> {y1_start}  |  OOS vault (Y1): {y1_start} -> {y1_end}")
    print(f"  Strategies: {len(strategies_map)}  |  Grid combos: {len(full_grid)}  |  Params: trend/rvol/sl/runner/STR/RSM")
    print(f"  Jobs: strategy={strategy_jobs}  combo={combo_jobs}")
    print(f"  Fixed: TP1=2xATR@30%  TP2=4xATR@30%  BE after 3 bars")
    print("=" * W)

    # ── PHASE 2: NAKED TRIAGE (IS data) ────────────────────────────────────
    print(f"\n  PHASE 2 - IS NAKED TRIAGE (EMA10 hard stop, no filters)")
    print(f"  Discard if IS max_drawdown > {_QO_TRIAGE_DD_CUTOFF*100:.0f}%")
    print("  " + "-" * (W - 4))
    print(f"  {'Strategy':<22} {'IS Ret':>8} {'IS DD':>7} {'Calmar':>8} {'Trades':>7} {'WR':>6}  Status")
    print("  " + "-" * (W - 4))

    triage_raw = Parallel(n_jobs=strategy_jobs)(
        delayed(_qr_eval_strategy)(strategy_cls, _QO_TRIAGE_PARAMS, [("IS", is_dfs)], args.capital)
        for _, strategy_cls in strategy_items
    )

    survivors: list[tuple[str, type]] = []
    for (strategy_id, strategy_cls), row_results in zip(strategy_items, triage_raw):
        agg = row_results[0]
        if not agg or agg.get("trade_count", 0) == 0:
            print(f"  {strategy_id:<22} {'N/A':>8} {'N/A':>7} {'N/A':>8} {'N/A':>7} {'N/A':>6}  SKIP (no trades)")
            continue
        ret = agg.get("annual_return", 0.0) * 100
        dd  = agg.get("max_drawdown",  0.0) * 100
        cal = agg.get("calmar",        0.0)
        tr  = agg.get("trade_count",   0)
        wr  = agg.get("win_rate",      0.0) * 100
        fail = (agg.get("max_drawdown", 0.0) or 0.0) > _QO_TRIAGE_DD_CUTOFF
        status = f"FAIL DD>{_QO_TRIAGE_DD_CUTOFF*100:.0f}%" if fail else "PASS"
        print(f"  {strategy_id:<22} {ret:>+8.1f} {dd:>7.1f} {cal:>8.2f} {tr:>7d} {wr:>5.0f}%  {status}")
        if not fail:
            survivors.append((strategy_id, strategy_cls))

    print("  " + "-" * (W - 4))
    print(f"  Survivors: {len(survivors)}/{len(strategy_items)}  {[s for s, _ in survivors]}")

    if not survivors:
        print("\n  All strategies failed triage. Lower _QO_TRIAGE_DD_CUTOFF or review strategies.")
        print("=" * W + "\n")
        return

    # ── PHASE 3: GRID SEARCH (IS data) ─────────────────────────────────────
    _ps = _QO_PARAM_SPACE
    print(f"\n  PHASE 3 - GRID SEARCH ({len(full_grid)} combos x {len(survivors)} strategies, IS data, objective=Calmar)")
    print(f"  Grid: trend={_ps['trend_filter']}  rvol={_ps['rvol_min']}  sl={_ps['sl_atr_mult']}  "
          f"runner={_ps['hard_stop_mode']}  str={_ps['str_max']}  rsm={_ps['rsm_min']}")
    print("  " + "-" * (W - 4))
    print(f"  {'Strategy':<22} {'BestCalmar':>10} {'Ret':>7} {'DD':>6} {'Tr':>5}  BestParams                     Pass/Total")
    print("  " + "-" * (W - 4))

    leaderboard: list[dict] = []
    for strategy_id, strategy_cls in survivors:
        combo_results = Parallel(n_jobs=combo_jobs)(
            delayed(_qo_eval_combo)(strategy_cls, combo, _QO_BASE_PARAMS, is_dfs, args.capital)
            for combo in full_grid
        )
        combo_results = [r for r in combo_results if r.get("trade_count", 0) > 0]
        if not combo_results:
            print(f"  {strategy_id}: no combos produced trades")
            continue
        for r in combo_results:
            gate_checks = _qo_gate_checks(r)
            passes = all(gate_checks.values())
            leaderboard.append({
                "strategy_id":  strategy_id,
                "strategy_cls": strategy_cls,
                "params":       r["params"],
                "metrics":      r,
                "gate_checks":  gate_checks,
                "gate_hits":    sum(1 for ok in gate_checks.values() if ok),
                "passes_gate":  passes,
            })
        best = max(combo_results, key=lambda r: float(r.get("calmar", 0.0) or 0.0))
        bp = best.get("params", {})
        best_params_str = (f"runner={bp.get('hard_stop_mode','?')}  "
                           f"trend={_qo_trend_label(bp)}  "
                           f"rvol={bp.get('rvol_min',0)}  "
                           f"sl={bp.get('sl_atr_mult',0)}")
        n_pass = sum(1 for e in leaderboard if e["strategy_id"] == strategy_id and e["passes_gate"])
        print(f"  {strategy_id:<22} {best.get('calmar',0):>10.2f} {best.get('annual_return',0)*100:>+7.1f} "
              f"{(best.get('max_drawdown',0) or 0)*100:>6.1f} {best.get('trade_count',0):>5d}  "
              f"{best_params_str:<35} {n_pass}/{len(combo_results)}")

    # ── PHASE 4: SURVIVAL GATES (top 5 per strategy) ──────────────────────
    passing = [e for e in leaderboard if e["passes_gate"]]
    passing.sort(key=_qo_rank_key, reverse=True)

    g = _QO_SURVIVAL_GATES
    _GATE_KEYS = ["trades", "profit_factor", "drawdown", "calmar"]
    _GATE_HDR  = ["Tr", "PF", "DD", "Cal"]

    print(f"\n  PHASE 4 - SURVIVAL GATES  (top 5 per strategy)")
    print(f"  Gates: Trades>={g['min_trades']}  PF>={g['min_profit_factor']}  DD<={g['max_drawdown']*100:.0f}%  Calmar>={g['min_calmar']}")
    print("  " + "-" * (W - 4))
    print(f"  {'#':>3}  {'Strategy':<22} {'Calmar':>7} {'Ret':>7} {'DD':>6} {'Tr':>5} {'PF':>5} {'WR':>5}  "
          f"{'Trend':<6} {'RVol':<5} {'SL':<4}  {'|':1}  {'  '.join(_GATE_HDR)}")
    print("  " + "-" * (W - 4))

    phase5_label = "passed-gate"

    # Collect top-5 per strategy from full leaderboard (sorted by calmar desc)
    strat_entries: dict[str, list] = {}
    for e in sorted(leaderboard, key=lambda x: float(x["metrics"].get("calmar", 0.0) or 0.0), reverse=True):
        sid = e["strategy_id"]
        if len(strat_entries.setdefault(sid, [])) < 5:
            strat_entries[sid].append(e)

    all_strategy_ids = list(strat_entries.keys())
    for sid in all_strategy_ids:
        entries = strat_entries[sid]
        n_pass = sum(1 for e in leaderboard if e["strategy_id"] == sid and e["passes_gate"])
        n_total = sum(1 for e in leaderboard if e["strategy_id"] == sid)
        print(f"\n  {sid}  ({n_pass}/{n_total} pass gates)")
        for rank, e in enumerate(entries, start=1):
            m = e["metrics"]
            p = e["params"]
            gc = e.get("gate_checks", {})
            gate_cols = "  ".join("✓" if gc.get(k) else "✗" for k in _GATE_KEYS)
            ok_marker = " " if e["passes_gate"] else "·"
            print(
                f"  {ok_marker}{rank:>2}  {'':<22} "
                f"{float(m.get('calmar',0.0) or 0.0):>7.2f} "
                f"{m.get('annual_return',0.0)*100:>+7.1f} "
                f"{(m.get('max_drawdown',0.0) or 0.0)*100:>6.1f} "
                f"{int(m.get('trade_count',0)):>5d} "
                f"{m.get('profit_factor',0.0):>5.2f} "
                f"{m.get('win_rate',0.0)*100:>4.0f}%  "
                f"{_qo_trend_label(p):<6} "
                f"{p.get('rvol_min',0.0):<5} "
                f"{p.get('sl_atr_mult',0.0):<4}  "
                f"{'|':1}  {gate_cols}"
            )
    print("\n  " + "-" * (W - 4))

    # top_n for Phase 5: all gate passers
    top_n: list[dict] = []
    if not passing:
        print("  No combos passed all gates.")
        # Fallback: best combo per survivor strategy
        seen_fb: set[str] = set()
        for e in sorted(leaderboard, key=_qo_rank_key, reverse=True):
            sid = e["strategy_id"]
            if sid not in seen_fb:
                seen_fb.add(sid)
                top_n.append(e)
        if not top_n:
            print("=" * W + "\n")
            return
        print(f"  Fallback: carrying best combo per strategy ({len(top_n)}) to OOS exam...")
        phase5_label = "near-miss"
    else:
        top_n = passing  # all gate passers go to OOS
        print(f"  {len(passing)} combos passed gates. All {len(top_n)} going to OOS exam.")

    # ── PHASE 5: OOS EXAM (Y1 vault) ───────────────────────────────────────
    print(f"\n  PHASE 5 - OOS EXAM (Y1 vault: {y1_start} -> {y1_end})")
    print(f"  Pass: annual_return > {_QO_TARGET_PASS_RET*100:.0f}%  AND  max_drawdown < {_QO_TARGET_PASS_DD*100:.0f}%")
    print("  " + "-" * (W - 4))
    print(f"  {'#':>3}  {'Strategy':<22} {'IS Cal':>7} {'OOS Ret':>9} {'OOS DD':>8} {'OOS Cal':>9} {'OOS Tr':>7} {'OOS WR':>7}  Verdict")
    print("  " + "-" * (W - 4))

    # Pre-populate all Phase 4 passers as watchlist (IS metrics only)
    candidate_records: list[dict] = []
    for e in passing:
        candidate_records.append({
            "strategy_id": e["strategy_id"],
            "source":      phase5_label,
            "params":      e["params"],
            "gate_hits":   int(e.get("gate_hits", 0)),
            "gate_misses": [name for name, ok in e.get("gate_checks", {}).items() if not ok],
            "is_metrics":  e["metrics"],
            "oos_metrics": {},
            "oos_pass":    False,
            "_entry":      e,  # temp ref for OOS update
        })
    # Index by (strategy_id, params key) for fast lookup
    _cand_idx: dict[int, int] = {id(e["_entry"]): i for i, e in enumerate(candidate_records)}

    oos_pass_count = 0
    prev_sid = None
    for idx, e in enumerate(top_n, start=1):
        if e["strategy_id"] != prev_sid:
            if prev_sid is not None:
                print("  " + "·" * (W - 4))
            prev_sid = e["strategy_id"]

        strategy = e["strategy_cls"]()
        try:
            is_m  = e["metrics"]
            oos_m = run_portfolio_backtest(oos_dfs, strategy, e["params"], initial_capital=args.capital) if oos_dfs else {}
        except Exception as exc:
            print(f"  {idx:>3}  {e['strategy_id']:<22} ERROR: {exc}")
            continue

        oos_ret = (oos_m.get("annual_return", 0.0) or 0.0) * 100
        oos_dd  = (oos_m.get("max_drawdown",  0.0) or 0.0) * 100
        oos_cal = oos_m.get("calmar", 0.0) or 0.0
        oos_tr  = oos_m.get("trade_count", 0)
        oos_wr  = (oos_m.get("win_rate", 0.0) or 0.0) * 100

        oos_ok  = oos_ret > _QO_TARGET_PASS_RET * 100 and oos_dd < _QO_TARGET_PASS_DD * 100
        verdict = "PASS ✓" if oos_ok else "FAIL ✗"
        if oos_ok:
            oos_pass_count += 1

        # Update the matching candidate_record with OOS results
        ci = _cand_idx.get(id(e))
        if ci is not None:
            candidate_records[ci]["oos_metrics"] = oos_m
            candidate_records[ci]["oos_pass"]    = oos_ok

        is_cal = float(is_m.get("calmar", 0.0) or 0.0)
        print(
            f"  {idx:>3}  {e['strategy_id']:<22} "
            f"{is_cal:>7.2f} "
            f"{oos_ret:>+9.1f} {oos_dd:>8.1f} {oos_cal:>9.2f} {oos_tr:>7d} {oos_wr:>6.0f}%  {verdict}"
        )
        p = e["params"]
        print(
            f"       runner={p.get('hard_stop_mode','?')}  "
            f"trend={_qo_trend_label(p)}  "
            f"rvol={p.get('rvol_min',0.0)}  "
            f"sl={p.get('sl_atr_mult',0.0)}  "
            f"str={p.get('str_max',0)}  "
            f"rsm={p.get('rsm_min',0)}"
        )

    # Strip temp ref before saving
    for c in candidate_records:
        c.pop("_entry", None)

    _save_optimise_candidates(market, candidate_records)

    print("  " + "-" * (W - 4))
    tradable_count = sum(1 for c in candidate_records if c["oos_pass"])
    watchlist_count = len(candidate_records) - tradable_count
    if oos_pass_count == 0:
        print("  No candidates passed OOS exam.")
    else:
        print(f"  {oos_pass_count}/{len(top_n)} passed OOS exam.  "
              f"Saved: {tradable_count} tradable + {watchlist_count} watchlist to strategy_candidates.")
    print("=" * W + "\n")


def cmd_report(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Print latest optimise candidates from DB — grouped by strategy."""
    from collections import defaultdict
    from db.models import SessionLocal, StrategyCandidateModel

    market = adapter.market_id
    db = SessionLocal()
    try:
        rows = (
            db.query(StrategyCandidateModel)
            .filter_by(market=market)
            .order_by(StrategyCandidateModel.evaluated_at.desc())
            .all()
        )
    finally:
        db.close()

    W = 160
    tradable_count = sum(1 for r in rows if r.candidate_status == "tradable")
    watchlist_count = len(rows) - tradable_count
    last_eval = rows[0].evaluated_at if rows else "—"

    print("\n" + "=" * W)
    print(f"  {market.upper()} OPTIMISE REPORT — {date.today()}")
    print(f"  {len(rows)} total  |  {tradable_count} tradable  |  {watchlist_count} watchlist  |  Last evaluated: {last_eval}")
    print("=" * W)

    if not rows:
        print("  No candidates found. Run optimise first.")
        print("=" * W + "\n")
        return

    # Group by strategy, tradable first within each group
    by_strategy: dict[str, list] = defaultdict(list)
    for r in rows:
        by_strategy[r.strategy].append(r)
    for sid in by_strategy:
        by_strategy[sid].sort(key=lambda r: (0 if r.candidate_status == "tradable" else 1,
                                              -(r.oos_calmar or 0.0)))

    COL_HDR = (f"  {'#':>3}  {'Status':<9} "
               f"{'IS Cal':>7} {'IS Ret':>7} {'IS DD':>6} {'IS WR':>6} {'IS Tr':>5}  "
               f"{'OOS Cal':>7} {'OOS Ret':>8} {'OOS DD':>7} {'OOS WR':>7} {'OOS Tr':>6}  "
               f"Trend  RVol  SL  STR RSM")

    for sid, group in sorted(by_strategy.items()):
        n_trad = sum(1 for r in group if r.candidate_status == "tradable")
        n_watch = len(group) - n_trad
        print(f"\n  ── {sid}  ({n_trad} tradable / {n_watch} watchlist) " + "─" * max(0, W - 35 - len(sid)))
        print(COL_HDR)
        print("  " + "-" * (W - 4))
        for idx, row in enumerate(group[:5], start=1):
            p = row.params or {}
            trend = str(p.get("trend_filter", p.get("trend_sma_period", 0)) or "off")
            rvol  = p.get("rvol_min", 0.0)
            sl    = p.get("sl_atr_mult", 0.0)
            str_v = p.get("str_max", 0)
            rsm_v = p.get("rsm_min", 0)
            is_cal = row.is_calmar or 0.0
            is_ret = (row.is_annual_return or 0.0) * 100
            is_dd  = (row.is_max_drawdown  or 0.0) * 100
            is_wr  = (row.is_win_rate      or 0.0) * 100
            is_tr  = row.is_trade_count or 0
            oos_cal = row.oos_calmar or 0.0
            oos_ret = (row.oos_annual_return or 0.0) * 100
            oos_dd  = (row.oos_max_drawdown  or 0.0) * 100
            oos_wr  = (row.oos_win_rate     or 0.0) * 100
            oos_tr  = row.oos_trade_count or 0
            status  = "TRADABLE" if row.candidate_status == "tradable" else "watchlist"
            oos_str = (f"{oos_cal:>7.2f} {oos_ret:>+8.1f} {oos_dd:>7.1f} {oos_wr:>6.0f}% {oos_tr:>6d}"
                       if oos_tr else f"{'—':>7} {'—':>8} {'—':>7} {'—':>7} {'—':>6}")
            print(
                f"  {idx:>3}  {status:<9} "
                f"{is_cal:>7.2f} {is_ret:>+7.1f} {is_dd:>6.1f} {is_wr:>5.0f}% {is_tr:>5d}  "
                f"{oos_str}  "
                f"{trend:<6} {rvol:<5} {sl:<4} {str_v:>3} {rsm_v:>3}"
            )
            if row.candidate_status != "tradable" and row.gate_misses:
                print(f"       miss: {', '.join(row.gate_misses)}")

    print("\n" + "=" * W + "\n")


def cmd_chart(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Generate interactive HTML chart for a optimise candidate and open in browser."""
    import webbrowser
    from pathlib import Path
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    from db.models import SessionLocal, StrategyCandidateModel
    from validation.backtest import run_portfolio_backtest, _precompute_indicators
    from charts.strategy_chart import generate_html

    market = adapter.market_id
    today = date.today()
    y1_end,  y1_start = today, today - timedelta(days=365)
    y3_start = today - timedelta(days=1095)
    chart_start = y1_start - timedelta(days=90)  # 3-month context before OOS

    # Load candidate from DB
    chart_strategy = getattr(args, "chart_strategy", None)
    candidate_num  = max(1, getattr(args, "candidate", 1))

    db = SessionLocal()
    try:
        q = db.query(StrategyCandidateModel).filter_by(market=market)
        if chart_strategy:
            q = q.filter_by(strategy=chart_strategy)
        all_rows = q.all()
    finally:
        db.close()

    if not all_rows:
        hint = f" for strategy '{chart_strategy}'" if chart_strategy else ""
        print(f"  No candidates in DB{hint}. Run optimise first.")
        return

    # Sort same as report: tradable first, then OOS Calmar desc
    all_rows.sort(key=lambda r: (0 if r.candidate_status == "tradable" else 1,
                                 -(r.oos_calmar or 0.0)))

    candidate_idx = min(candidate_num - 1, len(all_rows) - 1)
    row = all_rows[candidate_idx]

    strategies_map = StrategyRegistry.for_market(market)
    if row.strategy not in strategies_map:
        print(f"  ERROR: strategy '{row.strategy}' not found in registry.")
        return
    strategy = strategies_map[row.strategy]()

    print(f"\n  Charting {row.strategy} #{candidate_num}/{len(all_rows)}"
          f"  [{row.candidate_status}]")
    print(f"  OOS period: {y1_start} \u2192 {y1_end}")

    # Fetch universe + OHLCV
    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    bm_close = _fetch_benchmark(adapter, y3_start, today)

    precomputed: list = []
    for idx, symbol in enumerate(universe, 1):
        logger.info("[%d/%d] fetching %s", idx, len(universe), symbol)
        df = adapter.ohlcv(symbol, y3_start, today)
        df = _attach_benchmark(df, bm_close)
        if df.empty or len(df) < 60:
            continue
        df.attrs = {"symbol": symbol, "market": market}
        pdf = _precompute_indicators(df)
        pdf.attrs = {"symbol": symbol, "market": market}
        precomputed.append(pdf)

    if not precomputed:
        print("  No data fetched.")
        return

    # Slice data
    oos_dfs    = _slice_dfs(precomputed, y1_start, y1_end)    # backtest on OOS only
    chart_dfs  = _slice_dfs(precomputed, chart_start, y1_end)  # display includes context

    chart_df_map = {df.attrs["symbol"]: df for df in chart_dfs}

    # Run portfolio backtest on OOS to get per-symbol trades
    oos_result = run_portfolio_backtest(oos_dfs, strategy, row.params,
                                        initial_capital=args.capital)
    all_trades: list[dict] = oos_result.get("trades", [])

    # Group trades by symbol
    trades_by_symbol: dict[str, list[dict]] = {}
    for t in all_trades:
        trades_by_symbol.setdefault(t["symbol"], []).append(t)

    # Build symbol_data — all OOS symbols, trades optional
    symbol_data: list[dict] = []
    for df in oos_dfs:
        sym = df.attrs["symbol"]
        symbol_data.append({
            "symbol": sym,
            "df":     chart_df_map.get(sym, df),
            "trades": trades_by_symbol.get(sym, []),
        })
    # Symbols with trades first, then alphabetical
    symbol_data.sort(key=lambda x: (-len(x["trades"]), x["symbol"]))

    # IS / OOS metrics from DB row
    is_metrics = {
        "calmar":        row.is_calmar,
        "annual_return": row.is_annual_return,
        "max_drawdown":  row.is_max_drawdown,
        "trade_count":   row.is_trade_count,
        "win_rate":      row.is_win_rate,
    }
    oos_metrics = {
        "calmar":        row.oos_calmar,
        "annual_return": row.oos_annual_return,
        "max_drawdown":  row.oos_max_drawdown,
        "trade_count":   row.oos_trade_count,
        "win_rate":      row.oos_win_rate,
    }

    html = generate_html(
        market=market,
        strategy_id=row.strategy,
        params=row.params,
        is_metrics=is_metrics,
        oos_metrics=oos_metrics,
        y1_start=y1_start,
        y1_end=y1_end,
        symbol_data=symbol_data,
    )

    out_dir = Path(__file__).parent.parent / "charts" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{market}_{row.strategy}_{today}.html"
    out_file.write_text(html, encoding="utf-8")

    traded = sum(1 for sd in symbol_data if sd["trades"])
    total  = sum(len(sd["trades"]) for sd in symbol_data)
    print(f"  {traded}/{len(symbol_data)} symbols traded | {total} total trade events")
    print(f"  Chart: {out_file}")
    webbrowser.open(out_file.as_uri())


def cmd_regime(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """5-year regime discovery: which strategies have edge in uptrend/choppy/downtrend."""
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    from core.regime import label_regime, REGIMES
    from validation.backtest import run_portfolio_backtest, _precompute_indicators

    market   = adapter.market_id
    today    = date.today()
    y5_start = today - timedelta(days=1825)

    universe       = adapter.universe(today, top_n=getattr(args, "symbols", None))
    strategies_map = StrategyRegistry.for_market(market)

    logger.info("=== %s regime-discover | 5yr %s → %s | symbols=%d strategies=%d ===",
                market.upper(), y5_start, today, len(universe), len(strategies_map))

    bm_close = _fetch_benchmark(adapter, y5_start, today)
    if bm_close is None:
        print("  ERROR: no benchmark data — cannot compute regime.")
        return

    regime_series = label_regime(bm_close["_bm_close"])
    regime_date_map = {
        (ts.date() if hasattr(ts, "date") else ts): r
        for ts, r in regime_series.items()
    }

    regime_dist = regime_series.value_counts()
    total_bars  = len(regime_series)

    # Fetch + precompute all symbol data
    all_dfs = []
    for idx, symbol in enumerate(universe, 1):
        logger.info("[%d/%d] fetching %s (5yr)", idx, len(universe), symbol)
        df = adapter.ohlcv(symbol, y5_start, today)
        df = _attach_benchmark(df, bm_close)
        if df.empty or len(df) < 60:
            continue
        df.attrs = {"symbol": symbol, "market": market}
        pdf = _precompute_indicators(df)
        pdf.attrs = {"symbol": symbol, "market": market}
        all_dfs.append(pdf)

    if not all_dfs:
        print("  No data fetched.")
        return

    print(f"\n  Data ready: {len(all_dfs)} symbols. Running {len(strategies_map)} strategies × 5yr backtest... please wait.\n")

    EDGE_WR = 33.3  # breakeven for 2:1 RR

    # Run all strategies first, collect tagged trades
    all_strategy_trades: dict[str, list[dict]] = {}
    for strategy_id, strategy_cls in strategies_map.items():
        strategy = strategy_cls()
        params   = {**strategy.default_params, **_REGIME_DISCOVERY_PARAMS}
        try:
            result = run_portfolio_backtest(all_dfs, strategy, params,
                                            initial_capital=args.capital)
            trades = result.get("trades", [])
        except Exception as exc:
            logger.warning("regime backtest error %s: %s", strategy_id, exc)
            trades = []

        tagged = []
        for t in trades:
            r = regime_date_map.get(t["entry_date"], "choppy")
            tagged.append({**t, "regime": r, "year": t["entry_date"].year})
        all_strategy_trades[strategy_id] = tagged

    all_years = sorted({t["year"] for trades in all_strategy_trades.values() for t in trades})
    YC = 11  # chars per year cell: "+22.0%/12 "
    W  = 4 + 22 + 2 + 7 + 2 + len(all_years) * (YC + 2)

    dist_str = "  |  ".join(
        f"{r}: {regime_dist.get(r, 0) / total_bars * 100:.0f}% ({regime_dist.get(r, 0)}d)"
        for r in REGIMES
    )

    print("\n" + "=" * W)
    print(f"  {market.upper()} REGIME DISCOVERY — {today}  |  5yr: {y5_start} → {today}")
    print(f"  Universe: {len(all_dfs)} symbols  |  Strategies: {len(strategies_map)}  |  "
          f"SL=1.5×ATR  TP=3×ATR  (2:1 RR  edge=WR>{EDGE_WR:.0f}%)")
    print(f"  Benchmark: {dist_str}")

    year_hdr = "  ".join(f"{y:>{YC}}" for y in all_years)

    for regime in REGIMES:
        print("\n" + "=" * W)
        print(f"  {regime.upper()}")
        print("=" * W)
        print(f"  {'Strategy':<22}  {'WR':>6}  {year_hdr}")
        print("  " + "-" * (W - 4))

        for strategy_id in strategies_map:
            trades = all_strategy_trades[strategy_id]
            r_trades = [t for t in trades if t["regime"] == regime]

            if not r_trades:
                blanks = "  ".join(f"{'—':>{YC}}" for _ in all_years)
                print(f"  {strategy_id:<22}  {'—':>6}  {blanks}")
                continue

            wins = sum(1 for t in r_trades if t["pnl"] > 0)
            wr   = wins / len(r_trades) * 100
            edge = "✓" if wr >= EDGE_WR else "✗"
            wr_str = f"{wr:.0f}%{edge}"

            year_cols = []
            for y in all_years:
                yr = [t for t in r_trades if t["year"] == y]
                if not yr:
                    year_cols.append(f"{'—':>{YC}}")
                else:
                    ret = sum(t["pnl"] for t in yr) / args.capital * 100
                    n   = len(yr)
                    cell = f"{ret:>+5.1f}%/{n}"
                    year_cols.append(f"{cell:>{YC}}")

            print(f"  {strategy_id:<22}  {wr_str:>6}  {'  '.join(year_cols)}")

    # ── DEPLOYMENT SUMMARY ────────────────────────────────────────────────
    deploy: dict[str, list[str]] = {r: [] for r in REGIMES}
    no_edge: list[str] = []
    regime_results: list[dict] = []

    for strategy_id in strategies_map:
        trades  = all_strategy_trades[strategy_id]
        has_edge = False
        for regime in REGIMES:
            r_trades = [t for t in trades if t["regime"] == regime]
            wr = (sum(1 for t in r_trades if t["pnl"] > 0) / len(r_trades) * 100) if r_trades else 0.0
            acc = wr >= EDGE_WR and len(r_trades) > 0
            if acc:
                deploy[regime].append(strategy_id)
                has_edge = True
            yearly = {}
            for y in all_years:
                yr = [t for t in r_trades if t["year"] == y]
                if yr:
                    yearly[str(y)] = {
                        "ret_pct": round(sum(t["pnl"] for t in yr) / args.capital * 100, 2),
                        "trade_count": len(yr),
                    }
            regime_results.append({
                "strategy": strategy_id,
                "regime":   regime,
                "wr":       round(wr, 2),
                "trade_count": len(r_trades),
                "yearly":   yearly,
                "acceptable": acc,
            })
        if not has_edge:
            no_edge.append(strategy_id)

    print("\n" + "=" * W)
    print("  REGIME DEPLOYMENT MAP")
    print("=" * W)
    for regime in REGIMES:
        strats = "  ".join(deploy[regime]) if deploy[regime] else "—"
        print(f"  {regime.upper():<12} → {strats}")
    if no_edge:
        print(f"  {'DROP':<12} → {chr(32).join(no_edge)}  (no edge in any regime)")
    print("=" * W + "\n")

    # ── SAVE TO DB ────────────────────────────────────────────────────────
    from db.models import SessionLocal, RegimeMapModel
    db = SessionLocal()
    try:
        db.query(RegimeMapModel).filter_by(market=market).delete()
        for row in regime_results:
            db.add(RegimeMapModel(
                market      = market,
                strategy    = row["strategy"],
                regime      = row["regime"],
                wr          = row["wr"],
                trade_count = row["trade_count"],
                yearly      = row["yearly"],
                acceptable  = row["acceptable"],
            ))
        db.commit()
        logger.info("saved %d regime-map rows for %s", len(regime_results), market)
    finally:
        db.close()


def cmd_regime_report(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Print saved regime discovery results from DB."""
    from collections import defaultdict
    from db.models import SessionLocal, RegimeMapModel
    from core.regime import REGIMES

    market = adapter.market_id
    db = SessionLocal()
    try:
        rows = db.query(RegimeMapModel).filter_by(market=market).all()
    finally:
        db.close()

    if not rows:
        print("  No regime data. Run regime first.")
        return

    last_eval = rows[0].evaluated_at
    all_years = sorted({y for r in rows for y in (r.yearly or {}).keys()})
    EDGE_WR   = 33.3
    YC        = 11
    W         = 4 + 22 + 2 + 7 + 2 + len(all_years) * (YC + 2)

    print("\n" + "=" * W)
    print(f"  {market.upper()} REGIME REPORT  |  Last evaluated: {last_eval}")
    print("=" * W)

    by_regime: dict[str, list] = defaultdict(list)
    for r in rows:
        by_regime[r.regime].append(r)

    year_hdr = "  ".join(f"{y:>{YC}}" for y in all_years)

    for regime in REGIMES:
        regime_rows = by_regime.get(regime, [])
        print("\n" + "=" * W)
        print(f"  {regime.upper()}")
        print("=" * W)
        print(f"  {'Strategy':<22}  {'WR':>6}  {year_hdr}")
        print("  " + "-" * (W - 4))
        for row in regime_rows:
            edge   = "✓" if row.acceptable else "✗"
            wr_str = f"{row.wr:.0f}%{edge}"
            yearly = row.yearly or {}
            year_cols = []
            for y in all_years:
                d = yearly.get(str(y))
                if not d:
                    year_cols.append(f"{'—':>{YC}}")
                else:
                    cell = f"{d['ret_pct']:>+5.1f}%/{d['trade_count']}"
                    year_cols.append(f"{cell:>{YC}}")
            print(f"  {row.strategy:<22}  {wr_str:>6}  {'  '.join(year_cols)}")

    # Deployment map
    deploy: dict[str, list[str]] = {r: [] for r in REGIMES}
    no_edge: set[str] = set()
    all_strats = {r.strategy for r in rows}
    for r in rows:
        if r.acceptable:
            deploy[r.regime].append(r.strategy)
    for s in all_strats:
        if not any(s in deploy[r] for r in REGIMES):
            no_edge.add(s)

    print("\n" + "=" * W)
    print("  REGIME DEPLOYMENT MAP")
    print("=" * W)
    for regime in REGIMES:
        strats = "  ".join(sorted(set(deploy[regime]))) if deploy[regime] else "—"
        print(f"  {regime.upper():<12} → {strats}")
    if no_edge:
        print(f"  {'DROP':<12} → {' '.join(sorted(no_edge))}")
    print("=" * W + "\n")


def cmd_regime_optimise(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Phase 3: regime-aware grid search using saved regime map. (coming soon)"""
    from db.models import SessionLocal, RegimeMapModel
    from core.regime import REGIMES

    market = adapter.market_id
    db = SessionLocal()
    try:
        rows = db.query(RegimeMapModel).filter_by(market=market, acceptable=True).all()
    finally:
        db.close()

    if not rows:
        print("  No acceptable regime mappings found. Run regime first.")
        return

    print(f"\n  {market.upper()} REGIME OPTIMISE — coming soon.")
    print(f"  Will optimise {len(rows)} strategy×regime pairs using regime-filtered IS data.")
    for regime in REGIMES:
        strats = [r.strategy for r in rows if r.regime == regime]
        if strats:
            print(f"  {regime.upper():<12} → {', '.join(strats)}")
    print()


def make_parser(market_id: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{market_id.upper()} market pipeline")
    parser.add_argument("command",
                        choices=["scan", "diagnose", "paper", "live", "report", "optimise", "chart",
                                 "regime", "regime-report", "regime-optimise"])
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--symbols", type=int,
                        help="Limit to top N symbols after turnover filtering (default: all)")
    parser.add_argument("--candidate", type=int, default=1,
                        help="Which DB candidate to chart (1-based, default: 1 = most recent)")
    return parser


def run(adapter: MarketAdapter, command: str, args: argparse.Namespace) -> None:
    dispatch = {
        "scan":           lambda: cmd_scan(adapter, args),
        "diagnose":       lambda: cmd_diagnose(adapter, args),
        "paper":          lambda: cmd_paper(adapter, args),
        "live":           lambda: cmd_scan(adapter, args),
        "report":         lambda: cmd_report(adapter, args),
        "optimise":       lambda: cmd_optimise(adapter, args),
        "chart":          lambda: cmd_chart(adapter, args),
        "regime":         lambda: cmd_regime(adapter, args),
        "regime-report":  lambda: cmd_regime_report(adapter, args),
        "regime-optimise": lambda: cmd_regime_optimise(adapter, args),
    }
    dispatch.get(command, lambda: cmd_scan(adapter, args))()
