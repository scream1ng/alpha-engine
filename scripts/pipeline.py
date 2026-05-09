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

_OPT_REGIME_MIN_CALMAR = 0.3   # minimum baseline calmar to include pair in optimise-regime

_OPT_REGIME_BASE = {
    "sl_atr_mult": 1.5,
    "tp1_atr_mult": 3.0,
    "be_trigger_atr_mult": 999.0,
    "trail_atr_mult": 999.0,
    "be_after_bars": 0,
    "max_bars": 0,
    "risk_pct": 0.005,
}

_OPT_REGIME_TP_COMBOS = [
    {
        "_label": "original",
        "tp1_partial_pct": 1.0,
        "tp2_atr_mult": 999.0,
        "tp2_partial_pct": 0.0,
        "ema_exit_period": 0,
        "hard_stop_mode": "trail",
    },
    {
        "_label": "tp1_50pct_ema10",
        "tp1_partial_pct": 0.5,
        "tp2_atr_mult": 999.0,
        "tp2_partial_pct": 0.0,
        "ema_exit_period": 10,
        "hard_stop_mode": "ema10",
    },
    {
        "_label": "tp1_30pct_tp2_30pct_ema10",
        "tp1_partial_pct": 0.3,
        "tp2_atr_mult": 6.0,
        "tp2_partial_pct": 0.3,
        "ema_exit_period": 10,
        "hard_stop_mode": "ema10",
    },
]



def _print_regime_summary(regime_results: list[dict], all_years: list[int], W: int, YC: int) -> None:
    """Print PASS strategies summary + combined yearly totals."""
    passing = [r for r in regime_results if r["acceptable"]]
    RC = 12   # regime col
    WC = 7    # wr col
    CC = 8    # calmar col
    DC = 7    # dd col
    print("\n" + "=" * W)
    print("  REGIME SUMMARY — COMBINED PORTFOLIO")
    print("=" * W)
    year_hdr = "  ".join(f"{y:>{YC}}" for y in all_years)
    print(f"  {'Strategy':<22}  {'Regime':<{RC}}  {'WR':>{WC}}  {'Calmar':>{CC}}  {'DD':>{DC}}  {year_hdr}")
    print("  " + "-" * (W - 4))
    for row in passing:
        wr_str  = f"{row['wr']:.0f}%✓"
        cal_str = f"{row['calmar']:.2f}"
        dd_str  = f"{row['max_dd']:.1f}%"
        yearly  = row.get("yearly") or {}
        year_cols = [_fmt_regime_cell(yearly.get(str(y)), YC) for y in all_years]
        print(f"  {row['strategy']:<22}  {row['regime']:<{RC}}  {wr_str:>{WC}}  {cal_str:>{CC}}  {dd_str:>{DC}}  {'  '.join(year_cols)}")
    print("  " + "-" * (W - 4))
    # Combined totals
    totals_ret: dict[str, float] = {}
    totals_trades: dict[str, int] = {}
    for row in passing:
        for ys, d in (row.get("yearly") or {}).items():
            totals_ret[ys]    = totals_ret.get(ys, 0.0) + d["ret_pct"]
            totals_trades[ys] = totals_trades.get(ys, 0) + d["trade_count"]
    total_cells = []
    for y in all_years:
        ys = str(y)
        if ys in totals_ret:
            total_cells.append(f"{totals_ret[ys]:>+.1f}%/{totals_trades[ys]}".rjust(YC))
        else:
            total_cells.append(f"{'—':>{YC}}")
    print(f"  {'TOTAL':<22}  {'all':<{RC}}  {'':>{WC}}  {'':>{CC}}  {'':>{DC}}  {'  '.join(total_cells)}")
    print("=" * W)


def _fmt_regime_cell(d: dict | None, width: int) -> str:
    """Format a yearly regime cell: '+7.4%/24' right-aligned to width."""
    if not d:
        return f"{'—':>{width}}"
    return f"{d['ret_pct']:>+.1f}%/{d['trade_count']}".rjust(width)


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


def _load_regime_params(market: str, strategy_id: str, regime: str) -> dict | None:
    """Load best params for strategy+regime from optimise-regime results."""
    from db.models import SessionLocal, RegimeOptimiseModel
    db = SessionLocal()
    try:
        row = db.query(RegimeOptimiseModel).filter_by(
            market=market, strategy=strategy_id, regime=regime
        ).first()
        return dict(row.params) if row else None
    finally:
        db.close()


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

    # Save regime labels to DB so optimise-regime uses identical boundaries
    from db.models import SessionLocal as _SL, RegimeLabelModel
    _db = _SL()
    try:
        _db.query(RegimeLabelModel).filter_by(market=market).delete()
        _db.bulk_save_objects([
            RegimeLabelModel(market=market, date=d, regime=r)
            for d, r in regime_date_map.items()
        ])
        _db.commit()
    finally:
        _db.close()

    regime_dist = regime_series.value_counts()
    total_bars  = len(regime_series)

    print(f"  Fetching {len(universe)} symbols (5yr) ...", flush=True)
    all_dfs = []
    for idx, symbol in enumerate(universe, 1):
        print(f"  [{idx}/{len(universe)}] {symbol}", flush=True)
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

    print(f"\n  Done — {len(all_dfs)} symbols ready. Starting {len(strategies_map)} strategy backtests...\n")

    EDGE_CALMAR = 0.5

    # Run all strategies first, collect tagged trades
    all_strategy_trades: dict[str, list[dict]] = {}
    n_strats = len(strategies_map)
    for s_idx, (strategy_id, strategy_cls) in enumerate(strategies_map.items(), 1):
        print(f"  [{s_idx}/{n_strats}] {strategy_id} — running 5yr backtest ...", flush=True)
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
        print(f"  [{s_idx}/{n_strats}] {strategy_id} — done  ({len(trades)} trades)", flush=True)

    all_years     = sorted({t["year"] for trades in all_strategy_trades.values() for t in trades})
    partial_years = {y5_start.year, today.year}
    YC  = 11   # ret%/trades per year cell
    WC  = 7    # WR col
    CC  = 8    # Calmar col
    DC  = 7    # DD col
    WP  = 4 + 22 + 2 + WC + 2 + CC + 2 + DC + 2 + len(all_years) * (YC + 2)   # per-regime
    W   = WP + 13                                                                 # summary (+Regime col)

    dist_str = "  |  ".join(
        f"{r}: {regime_dist.get(r, 0) / total_bars * 100:.0f}% ({regime_dist.get(r, 0)}d)"
        for r in REGIMES
    )

    print("\n" + "=" * W)
    print(f"  {market.upper()} REGIME DISCOVERY — {today}  |  5yr: {y5_start} → {today}")
    print(f"  Universe: {len(all_dfs)} symbols  |  Strategies: {len(strategies_map)}  |  "
          f"SL=1.5×ATR  TP=3×ATR  (2:1 RR  edge=Calmar>{EDGE_CALMAR:.0f})")
    print(f"  Benchmark: {dist_str}")

    # ── BUILD REGIME RESULTS ──────────────────────────────────────────────
    regime_results: list[dict] = []

    for strategy_id in strategies_map:
        trades = all_strategy_trades[strategy_id]
        for regime in REGIMES:
            r_trades = [t for t in trades if t["regime"] == regime]
            yearly: dict[str, dict] = {}
            for y in all_years:
                yr = [t for t in r_trades if t["year"] == y]
                if not yr:
                    continue
                yr_wins = sum(1 for t in yr if t["pnl"] > 0)
                yearly[str(y)] = {
                    "ret_pct":     round(sum(t["pnl"] for t in yr) / args.capital * 100, 2),
                    "trade_count": len(yr),
                    "wr":          round(yr_wins / len(yr) * 100, 1),
                }
            # aggregate WR
            agg_wr = (sum(1 for t in r_trades if t["pnl"] > 0) / len(r_trades) * 100) if r_trades else 0.0
            # 5yr calmar + max_dd
            if r_trades:
                eq, peak, max_dd_5yr = args.capital, args.capital, 0.0
                for t in sorted(r_trades, key=lambda x: x["entry_date"]):
                    eq += t["pnl"]
                    if eq > peak:
                        peak = eq
                    if peak > 0:
                        max_dd_5yr = max(max_dd_5yr, (peak - eq) / peak)
                total_ret = sum(t["pnl"] for t in r_trades) / args.capital
                n_regime_bars = max(int(regime_dist.get(regime, 1)), 1)
                annual_ret = total_ret * (252 / n_regime_bars)
                calmar_5yr = annual_ret / max_dd_5yr if max_dd_5yr > 0 else annual_ret
            else:
                max_dd_5yr, calmar_5yr = 0.0, 0.0
            # gate: WR>33% in ≥3 full calendar years
            acc = bool(r_trades) and calmar_5yr > EDGE_CALMAR
            regime_results.append({
                "strategy":    strategy_id,
                "regime":      regime,
                "wr":          round(agg_wr, 2),
                "calmar":      round(calmar_5yr, 2),
                "max_dd":      round(max_dd_5yr * 100, 1),
                "trade_count": len(r_trades),
                "yearly":      yearly,
                "acceptable":  acc,
            })

    # ── PRINT PER-REGIME DISCOVERY TABLES ────────────────────────────────
    year_hdr = "  ".join(f"{y:>{YC}}" for y in all_years)
    by_regime_disc: dict[str, list[dict]] = {r: [] for r in REGIMES}
    for row in regime_results:
        by_regime_disc[row["regime"]].append(row)

    for regime in REGIMES:
        print("\n" + "=" * WP)
        print(f"  {regime.upper()}")
        print("=" * WP)
        print(f"  {'Strategy':<22}  {'WR':>{WC}}  {'Calmar':>{CC}}  {'DD':>{DC}}  {year_hdr}")
        print("  " + "-" * (WP - 4))
        for row in by_regime_disc[regime]:
            if not row["yearly"]:
                blanks = "  ".join(f"{'—':>{YC}}" for _ in all_years)
                print(f"  {row['strategy']:<22}  {'—':>{WC}}  {'—':>{CC}}  {'—':>{DC}}  {blanks}")
                continue
            wr_str  = f"{row['wr']:.0f}%{'✓' if row['acceptable'] else '✗'}"
            cal_str = f"{row['calmar']:.2f}"
            dd_str  = f"{row['max_dd']:.1f}%"
            year_cols = [_fmt_regime_cell(row["yearly"].get(str(y)), YC) for y in all_years]
            print(f"  {row['strategy']:<22}  {wr_str:>{WC}}  {cal_str:>{CC}}  {dd_str:>{DC}}  {'  '.join(year_cols)}")

    _print_regime_summary(regime_results, all_years, W, YC)

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
                calmar      = row["calmar"],
                max_dd      = row["max_dd"],
                trade_count = row["trade_count"],
                yearly      = row["yearly"],
                acceptable  = row["acceptable"],
            ))
        db.commit()
        logger.info("saved %d regime-map rows for %s", len(regime_results), market)
    finally:
        db.close()

    _export_regime_markdown(regime_results, market, today, len(all_dfs), len(strategies_map), all_years, EDGE_CALMAR)


def _export_regime_markdown(
    regime_results: list[dict],
    market: str,
    as_of,
    n_symbols: int,
    n_strategies: int,
    all_years: list[int],
    edge_calmar: float = 0.5,
) -> str:
    from pathlib import Path
    from datetime import datetime
    from core.regime import REGIMES

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    latest_path = Path("reports") / f"{market}_regime_latest.md"
    history_path = Path("reports") / "history" / f"{timestamp}_{market}_regime.md"

    passing   = [r for r in regime_results if r["acceptable"]]
    near_miss = [r for r in regime_results if not r["acceptable"] and (r.get("calmar") or 0) >= 0.5]

    lines = [
        f"# {market.upper()} Regime Discovery",
        "",
        f"- As of: `{as_of}`",
        f"- Market: `{market}`",
        f"- Universe: `{n_symbols} symbols` | `5yr`",
        f"- Strategies tested: `{n_strategies}`",
        f"- Gate: `calmar > {edge_calmar}`",
        f"- Counts: `PASS={len(passing)}` `FAIL={len(regime_results) - len(passing)}`",
        "",
    ]

    for regime in REGIMES:
        regime_rows = [r for r in regime_results if r["regime"] == regime]
        lines.extend([f"## {regime.upper()}", ""])
        year_cols = " | ".join(str(y) for y in all_years)
        lines.append(f"| Strategy | WR | Calmar | DD | Trades | {year_cols} | Status |")
        lines.append("|---|---|---|---|---|" + "---|" * len(all_years) + "---|")
        for row in sorted(regime_rows, key=lambda r: -(r.get("calmar") or 0)):
            wr_s  = f"{row['wr']:.0f}%"       if row.get("wr")      is not None else "—"
            cal_s = f"{row['calmar']:.2f}"     if row.get("calmar")  is not None else "—"
            dd_s  = f"{row['max_dd']:.1f}%"   if row.get("max_dd")  is not None else "—"
            tr_s  = str(row.get("trade_count", 0))
            status = "✓ PASS" if row["acceptable"] else "✗"
            yearly = row.get("yearly") or {}
            year_cells = " | ".join(
                f"{d['ret_pct']:+.1f}%/{d['trade_count']}" if (d := yearly.get(str(y))) else "—"
                for y in all_years
            )
            lines.append(f"| {row['strategy']} | {wr_s} | {cal_s} | {dd_s} | {tr_s} | {year_cells} | {status} |")
        lines.append("")

    lines.extend(["## PASS — Pairs to Optimise", ""])
    if passing:
        for r in sorted(passing, key=lambda r: -(r.get("calmar") or 0)):
            lines.append(
                f"- `{r['strategy']} | {r['regime']}` — "
                f"calmar=`{r['calmar']:.2f}` wr=`{r['wr']:.0f}%` "
                f"dd=`{r['max_dd']:.1f}%` trades=`{r['trade_count']}`"
            )
    else:
        lines.append("_None — lower calmar gate or review strategies._")
    lines.append("")

    lines.extend(["## Near Miss (calmar 0.5–1.0)", ""])
    if near_miss:
        for r in sorted(near_miss, key=lambda r: -(r.get("calmar") or 0)):
            lines.append(
                f"- `{r['strategy']} | {r['regime']}` — "
                f"calmar=`{r['calmar']:.2f}` wr=`{r['wr']:.0f}%` dd=`{r['max_dd']:.1f}%`"
            )
    else:
        lines.append("_None_")
    lines.append("")

    lines.extend([
        "## AI Suggestions",
        "",
        "<!-- AI reads this file and writes specific next-run suggestions here -->",
        "",
        "## Next Loop",
        "",
        f"- Run `python run.py {market} regime` again after strategy/code changes.",
        f"- Run `python run.py {market} optimise-regime` on PASS pairs to tune rvol + TP.",
        "- Change one thing at a time.",
        "",
    ])

    content = "\n".join(lines)
    for path in (latest_path, history_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    print(f"  Markdown: {latest_path}")
    return str(latest_path)


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
    all_years = sorted({int(y) for r in rows for y in (r.yearly or {}).keys()})
    YC        = 11
    WC        = 7
    CC        = 8
    DC        = 7
    WP        = 4 + 22 + 2 + WC + 2 + CC + 2 + DC + 2 + len(all_years) * (YC + 2)
    W         = WP + 13

    print("\n" + "=" * W)
    print(f"  {market.upper()} REGIME REPORT  |  Last evaluated: {last_eval}")
    print("=" * W)

    by_regime: dict[str, list] = defaultdict(list)
    for r in rows:
        by_regime[r.regime].append(r)

    year_hdr = "  ".join(f"{y:>{YC}}" for y in all_years)

    for regime in REGIMES:
        regime_rows = by_regime.get(regime, [])
        print("\n" + "=" * WP)
        print(f"  {regime.upper()}")
        print("=" * WP)
        print(f"  {'Strategy':<22}  {'WR':>{WC}}  {'Calmar':>{CC}}  {'DD':>{DC}}  {year_hdr}")
        print("  " + "-" * (WP - 4))
        for row in regime_rows:
            edge    = "✓" if row.acceptable else "✗"
            wr_str  = f"{row.wr:.0f}%{edge}" if row.wr is not None else "—"
            cal_str = f"{row.calmar:.2f}" if row.calmar is not None else "—"
            dd_str  = f"{row.max_dd:.1f}%" if row.max_dd is not None else "—"
            yearly  = row.yearly or {}
            year_cols = [_fmt_regime_cell(yearly.get(str(y)), YC) for y in all_years]
            print(f"  {row.strategy:<22}  {wr_str:>{WC}}  {cal_str:>{CC}}  {dd_str:>{DC}}  {'  '.join(year_cols)}")

    regime_results = [
        {
            "strategy":    r.strategy,
            "regime":      r.regime,
            "wr":          r.wr or 0.0,
            "calmar":      r.calmar,
            "max_dd":      r.max_dd,
            "trade_count": r.trade_count or 0,
            "yearly":      r.yearly or {},
            "acceptable":  r.acceptable,
        }
        for r in rows
    ]
    _print_regime_summary(regime_results, all_years, W, YC)

    # ── ACTIVE PARAMS (from optimise-regime) ──────────────────────────────
    from db.models import RegimeOptimiseModel
    opt_db = SessionLocal()
    try:
        opt_rows = opt_db.query(RegimeOptimiseModel).filter_by(market=market).all()
    finally:
        opt_db.close()

    if opt_rows:
        print("\n" + "=" * W)
        print("  ACTIVE PARAMS  (best combo from optimise-regime)")
        print("=" * W)
        for opt in sorted(opt_rows, key=lambda r: r.strategy):
            p = opt.params or {}
            tp1_pct = int(p.get("tp1_partial_pct", 1.0) * 100)
            tp2_mult = p.get("tp2_atr_mult", 999.0)
            tp2_pct  = int(p.get("tp2_partial_pct", 0.0) * 100)
            ema      = p.get("ema_exit_period", 0)
            sl_mult  = p.get("sl_atr_mult", 1.5)
            tp1_mult = p.get("tp1_atr_mult", 3.0)
            rvol     = p.get("rvol_min", 0.0)
            combo    = opt.best_combo or "—"

            tp_desc = f"TP1={tp1_mult:.1f}×ATR {tp1_pct}%"
            if tp2_mult < 900:
                tp_desc += f"  TP2={tp2_mult:.1f}×ATR {tp2_pct}%"
            if ema:
                tp_desc += f"  EMA{ema}-exit"
            rvol_desc = f"  rvol≥{rvol:.1f}" if rvol > 0 else "  no-rvol-filter"
            print(f"  {opt.strategy:<22}  {opt.regime:<12}  combo={combo:<28}  SL={sl_mult:.1f}×ATR  {tp_desc}{rvol_desc}")
        print("=" * W)
    else:
        print("\n  (no optimise-regime results — run optimise-regime first)")


def cmd_optimise_regime(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Step 2: test TP exit combos on PASS regime pairs, find best exit structure."""
    import time
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    from core.regime import label_regime, REGIMES
    from validation.backtest import run_portfolio_backtest, _precompute_indicators
    from db.models import SessionLocal, RegimeMapModel, RegimeOptimiseModel

    market   = adapter.market_id
    today    = date.today()
    y5_start = today - timedelta(days=1825)

    db = SessionLocal()
    try:
        regime_rows = db.query(RegimeMapModel).filter(
            RegimeMapModel.market  == market,
            RegimeMapModel.calmar  >= _OPT_REGIME_MIN_CALMAR,
        ).all()
    finally:
        db.close()

    if not regime_rows:
        print(f"  No pairs with calmar >= {_OPT_REGIME_MIN_CALMAR}. Run regime first.")
        return

    pairs = [(r.strategy, r.regime, float(r.calmar or 0)) for r in regime_rows]
    strategies_map = StrategyRegistry.for_market(market)

    # Load regime labels from DB (saved by cmd_regime) to ensure identical boundaries
    from db.models import RegimeLabelModel
    _db2 = SessionLocal()
    try:
        label_rows = _db2.query(RegimeLabelModel).filter_by(market=market).all()
    finally:
        _db2.close()

    if label_rows:
        regime_date_map: dict = {row.date: row.regime for row in label_rows}
        regime_bars: dict[str, int] = {}
        for row in label_rows:
            regime_bars[row.regime] = regime_bars.get(row.regime, 0) + 1
        bm_close = None  # not needed for label assignment
    else:
        # fallback: recompute (first run before regime labels saved)
        bm_close = _fetch_benchmark(adapter, y5_start, today)
        if bm_close is None:
            print("  ERROR: no benchmark data and no saved regime labels. Run regime first.")
            return
        regime_series   = label_regime(bm_close["_bm_close"])
        regime_date_map = {
            (ts.date() if hasattr(ts, "date") else ts): r
            for ts, r in regime_series.items()
        }
        regime_bars = {}
        for ts, r in regime_series.items():
            regime_bars[r] = regime_bars.get(r, 0) + 1

    # Still need bm_close to attach benchmark to OHLCV for indicator precompute
    if bm_close is None:
        bm_close = _fetch_benchmark(adapter, y5_start, today)
        if bm_close is None:
            print("  ERROR: no benchmark data.")
            return

    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    print(f"\n  Fetching {len(universe)} symbols (5yr)...", flush=True)
    all_dfs = []
    for idx, symbol in enumerate(universe, 1):
        print(f"  [{idx}/{len(universe)}] {symbol}", flush=True)
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

    all_years = sorted({
        str(d.year if hasattr(d, "year") else int(str(d)[:4]))
        for df in all_dfs for d in df.index
    })
    YW = 8  # width per year column

    W = 116 + YW * len(all_years)
    print(f"\n{'='*W}")
    print(f"  {market.upper()} OPTIMISE REGIME — {today}")
    print(f"  {len(pairs)} pairs (calmar >= {_OPT_REGIME_MIN_CALMAR}) x {len(_OPT_REGIME_TP_COMBOS)} TP combos | SL fixed 1.5×ATR")
    print(f"{'='*W}")

    results: list[dict] = []
    n_pairs = len(pairs)
    global_t0 = time.time()

    for pair_idx, (strategy_id, target_regime, baseline_calmar) in enumerate(pairs, 1):
        strategy_cls = strategies_map.get(strategy_id)
        if strategy_cls is None:
            logger.warning("strategy %s not in registry", strategy_id)
            continue

        n_bars = max(regime_bars.get(target_regime, 1), 1)

        elapsed_global = time.time() - global_t0
        if pair_idx > 1:
            avg_pair = elapsed_global / (pair_idx - 1)
            eta_pairs = avg_pair * (n_pairs - pair_idx + 1)
            eta_global = f"{int(eta_pairs//60)}m{int(eta_pairs%60):02d}s" if eta_pairs >= 60 else f"{int(eta_pairs)}s"
            global_timing = f"  [{pair_idx}/{n_pairs} pairs | total elapsed {elapsed_global:.0f}s | eta {eta_global}]"
        else:
            global_timing = f"  [{pair_idx}/{n_pairs} pairs | starting]"
        yr_hdr = "  ".join(f"{y:>{YW}}" for y in all_years)
        print(f"\n  {strategy_id} | {target_regime}  (baseline calmar={baseline_calmar:.2f}){global_timing}", flush=True)
        print(f"  {'Combo':<32} {'Calmar':>8} {'Ret%ann':>8} {'DD':>7} {'Trades':>7} {'WR':>6}  {yr_hdr}")
        print(f"  {'-'*(W-4)}")

        combo_results: list[dict] = []
        best: dict | None = None
        n_combos = len(_OPT_REGIME_TP_COMBOS)
        pair_t0 = time.time()

        for combo_idx, combo in enumerate(_OPT_REGIME_TP_COMBOS, 1):
            label  = combo["_label"]
            params = {
                **strategy_cls().default_params,
                **_OPT_REGIME_BASE,
                **{k: v for k, v in combo.items() if not k.startswith("_")},
            }
            try:
                result = run_portfolio_backtest(all_dfs, strategy_cls(), params,
                                               initial_capital=args.capital)
                trades = [
                    t for t in result.get("trades", [])
                    if regime_date_map.get(t["entry_date"], "choppy") == target_regime
                ]
                metrics = _p3_metrics_from_trades(
                    trades, target_regime, regime_date_map, args.capital, n_bars
                )
            except Exception as exc:
                print(f"  {label:<32} ERROR: {exc}")
                continue

            metrics["params"]  = params
            metrics["_label"]  = label
            cal = float(metrics.get("calmar", 0) or 0)
            ret = float(metrics.get("annual_return", 0) or 0) * 100
            dd  = float(metrics.get("max_drawdown", 0) or 0) * 100
            tr  = int(metrics.get("trade_count", 0) or 0)
            wr  = float(metrics.get("win_rate", 0) or 0) * 100
            elapsed = time.time() - pair_t0
            avg_per = elapsed / combo_idx
            eta_secs = avg_per * (n_combos - combo_idx)
            eta_str = f"{int(eta_secs//60)}m{int(eta_secs%60):02d}s" if eta_secs >= 60 else f"{int(eta_secs)}s"
            timing = f"[{combo_idx}/{n_combos} +{elapsed:.0f}s eta {eta_str}]"
            yearly = metrics.get("yearly", {})
            yr_cols = "  ".join(
                f"{yearly[y]:>+{YW}.1f}%" if y in yearly else f"{'—':>{YW}}"
                for y in all_years
            )
            best_marker = " *" if best is None or cal > float(best.get("calmar", 0) or 0) else "  "
            print(f"  {best_marker}{label:<30} {cal:>8.2f} {ret:>+8.1f}% {dd:>6.1f}% {tr:>7d} {wr:>5.0f}%  {yr_cols}  {timing}", flush=True)
            combo_results.append(metrics)
            if best is None or cal > float(best.get("calmar", 0) or 0):
                best = metrics

        if best is None:
            continue

        results.append({
            "strategy":        strategy_id,
            "regime":          target_regime,
            "baseline_calmar": baseline_calmar,
            "best_label":      best["_label"],
            "best_calmar":     float(best.get("calmar", 0) or 0),
            "best_return":     float(best.get("annual_return", 0) or 0),
            "best_dd":         float(best.get("max_drawdown", 0) or 0),
            "best_trades":     int(best.get("trade_count", 0) or 0),
            "best_wr":         float(best.get("win_rate", 0) or 0),
            "params":          best["params"],
            "all_combos":      combo_results,
        })
        print(f"  → Best: {best['_label']} (calmar={float(best.get('calmar',0)):.2f})")

    # Save best params to DB
    db = SessionLocal()
    try:
        db.query(RegimeOptimiseModel).filter_by(market=market).delete()
        for r in results:
            db.add(RegimeOptimiseModel(
                market=market,
                strategy=r["strategy"],
                regime=r["regime"],
                params=r["params"],
                best_combo=r["best_label"],
                oos_pass=r["best_calmar"] >= 0.5,
            ))
        db.commit()
        logger.info("saved %d optimise-regime results for %s", len(results), market)
    finally:
        db.close()

    # Summary table
    print(f"\n{'='*W}")
    print(f"  SUMMARY")
    print(f"  {'Strategy':<22} {'Regime':<12} {'Baseline':>9} {'Best Cal':>9} {'Improved':>9}  Best Combo")
    print(f"  {'-'*(W-4)}")
    for r in sorted(results, key=lambda x: -x["best_calmar"]):
        delta = r["best_calmar"] - r["baseline_calmar"]
        print(f"  {r['strategy']:<22} {r['regime']:<12} {r['baseline_calmar']:>9.2f} "
              f"{r['best_calmar']:>9.2f} {delta:>+9.2f}  {r['best_label']}")
    print(f"{'='*W}\n")

    _export_optimise_regime_markdown(results, market, today)


def run(adapter: MarketAdapter, command: str, args: argparse.Namespace) -> None:
    dispatch = {
        "regime":           lambda: cmd_regime(adapter, args),
        "regime-report":    lambda: cmd_regime_report(adapter, args),
        "optimise-regime":  lambda: cmd_optimise_regime(adapter, args),
    }
    if command in dispatch:
        dispatch[command]()
    else:
        print(f"  Unknown command: {command}. Available: {list(dispatch)}")
