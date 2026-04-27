"""
Shared market pipeline commands — used by all market runners.
All commands are parameterised by a MarketAdapter instance.
"""
from __future__ import annotations
import argparse
import json
import logging
from datetime import date, timedelta

from markets.base import MarketAdapter

logger = logging.getLogger(__name__)


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
    from validation.optimizer import walk_forward_optimise_market
    from validation.consistency import check_consistency_market
    from db.models import SessionLocal, StrategyParamsModel
    from config import SELECTION_GATE

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
    for idx, (strategy_id, strategy_cls) in enumerate(strategies_map.items(), start=1):
        strategy = strategy_cls()
        logger.info("[%d/%d] optimising %s across %d symbols",
                    idx, total_strategies, strategy_id, len(market_dfs))

        opt = walk_forward_optimise_market(market_dfs, strategy, initial_capital=args.capital)

        if opt["status"] == "no_windows":
            logger.warning("  → no valid windows, skipping DB write")
            continue

        consistency = check_consistency_market(market_dfs, strategy, opt["best_params"], args.capital)
        m = opt.get("best_metrics", {})
        is_live = opt["status"] == "ok" and consistency["pass"]

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
        row.consistency_pass = consistency["pass"]
        row.is_live = is_live
        db.commit()
        logger.info("  → saved to DB (score=%.3f is_live=%s)", new_score, is_live)

    db.close()
    logger.info("=== optimisation complete | strategies=%d symbols=%d ===", total_strategies, len(market_dfs))

    _print_report(market, universe, strategies_map)


def _print_report(market: str, universe: list, strategies_map: dict) -> None:
    import strategies as _strats  # noqa: F401
    from db.models import SessionLocal as _SL, StrategyParamsModel as _SP
    from core.registry import StrategyRegistry as _SR
    from config import SELECTION_GATE
    from datetime import date as _date

    _db = _SL()
    rows = _db.query(_SP).filter_by(market=market).order_by(_SP.backtest_score.desc()).all()
    _db.close()

    _strat_map = _SR.for_market(market)
    g = SELECTION_GATE
    W = 88

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

    print("\n" + "=" * W)
    print(f"  {market.upper()} OPTIMISE REPORT — {_date.today()}")
    if universe:
        print(f"  Universe: {len(universe)} symbols  |  Strategies: {len(strategies_map)}  |  Combos: {len(universe) * len(strategies_map)}")
    else:
        print(f"  Strategies in DB: {len(rows)}  (reading saved results)")
    print("  Metrics: shared params per strategy, latest OOS window, median across sampled symbols")
    print("=" * W)

    live_count = 0
    for r in rows:
        p = r.params or {}
        defaults = _strat_map[r.strategy]().default_params if r.strategy in _strat_map else {}

        if r.is_live:
            status = "LIVE ✓"
            live_count += 1
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
        print(f"  │  OOS Return: {ann_ret:+.1f}%{_flag(ann_ok)}(≥{g['min_annual_return']*100:.0f}%)  "
              f"MaxDD={max_dd:.1f}%  "
              f"Trades={r.backtest_trade_count or 0}")
        print(f"  │  Trade pct:  "
              f"AvgWin={(r.backtest_avg_win or 0) * 100:+.1f}%  "
              f"AvgLoss={(r.backtest_avg_loss or 0) * 100:+.1f}%  "
              f"Ratio={abs(r.backtest_avg_win or 0) / abs(r.backtest_avg_loss or 1):.2f}x")
        print(f"  │  Quality  :  "
              f"Sharpe={r.backtest_sharpe or 0:.2f}{_flag(sharpe_ok)}(≥{g['min_sharpe']})  "
              f"Calmar={r.backtest_calmar or 0:.1f}{_flag(calmar_ok)}(≥{g['min_calmar']})  "
              f"PF={r.backtest_pf or 0:.2f}{_flag(pf_ok)}(≥{g['min_profit_factor']})  "
              f"WR={((r.backtest_winrate or 0)*100):.0f}%{_flag(wr_ok)}(≥{g['min_win_rate']*100:.0f}%)  "
              f"Score={r.backtest_score or 0:.1f}")

        if p:
            sig_keys = ["nr_period", "atr_pct_max", "psth", "lookback",
                        "bb_period", "bb_std", "kc_mult", "kc_period",
                        "fast_period", "slow_period", "trend_period",
                        "rsi_threshold", "consec_down_days", "rvol_min",
                        "pullback_atr_band", "rvol_max_on_pullback", "body_pct_min",
                        "rsm_min", "trend_sma_period"]
            sig_str = _param_delta(p, defaults, sig_keys)
            if sig_str:
                print(f"  │  Signal filter:  {sig_str}")

            print(f"  │  Entry → Exit :")
            print(f"  │    SL     = {sl}×ATR  (cut loss here)")
            trend_p = p.get("trend_sma_period", 0)
            trend_str = f"SMA{trend_p} uptrend filter" if trend_p else "no trend filter"
            tp1_pct  = int(p.get("tp1_partial_pct", 0.5) * 100)
            tp2_pct  = int(p.get("tp2_partial_pct", 1.0) * 100)
            after_tp1 = 100 - tp1_pct
            after_tp2 = after_tp1 - int(after_tp1 * p.get("tp2_partial_pct", 1.0))
            print(f"  │    Trend  = {trend_str}")
            print(f"  │    TP1    = {tp1}×ATR  → sell {tp1_pct}%,  {after_tp1}% remains,  SL→breakeven")
            print(f"  │    TP2    = {tp2}×ATR  → sell {tp2_pct}% of remaining"
                  + (f",  {after_tp2}% trails to stop" if after_tp2 > 0 else "  (full close)"))
            print(f"  │    Trail  = {trail}×ATR  (trailing stop on remainder after TP1)")
            print(f"  │    EMA    = {ema_str}  (hard exit if close < EMA after TP1)")
            print(f"  │    Time   = exit after {p.get('max_bars','?')} bars (no TP hit)")
            print(f"  │  Risk     :  {risk_pct*100:.2f}% capital per trade  |  Raw RR = {rr_raw}:1  (TP1/SL)")

            changed = []
            for k, ov in p.items():
                dv = defaults.get(k)
                if dv is not None and isinstance(ov, (int, float)) and isinstance(dv, (int, float)):
                    if abs(ov - dv) > 1e-9:
                        changed.append(f"{k}: {dv}→{ov}")
            if changed:
                print(f"  │  Optimizer changed: {', '.join(changed)}")

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


def make_parser(market_id: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{market_id.upper()} market pipeline")
    parser.add_argument("command",
                        choices=["scan", "diagnose", "optimise", "paper", "validate", "live", "report"])
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
        "paper":    lambda: cmd_paper(adapter, args),
        "validate": lambda: (cmd_optimise(adapter, args), cmd_paper(adapter, args)),
        "live":     lambda: cmd_scan(adapter, args),
        "report":   lambda: cmd_report(adapter, args),
    }
    dispatch.get(command, lambda: cmd_scan(adapter, args))()
