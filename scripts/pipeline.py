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

import pandas as pd
from joblib import Parallel, delayed

from core.active_roster import (
    diff_active_roster as _diff_active_roster,
    sync_active_roster_file_to_db,
    write_active_roster_snapshot,
)
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

_OPT_REGIME_MIN_CALMAR = 0.3   # minimum baseline calmar to include pair in regime-optimise

_OPT_REGIME_BASE = {
    "sl_atr_mult": 1.5,
    "tp1_atr_mult": 3.0,
    "be_trigger_atr_mult": 999.0,
    "trail_atr_mult": 999.0,
    "be_after_bars": 0,
    "max_bars": 0,
    "risk_pct": 0.005,
}

# Pivot breakout uses tighter 1:2 RR to match intraday limit entry mechanics
_PIVOT_OPT_BASE = {
    **_OPT_REGIME_BASE,
    "sl_atr_mult": 1.0,
    "tp1_atr_mult": 2.0,
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

_OPT_REGIME_RVOL_VALUES = [0.0, 1.5, 2.0]
_RSM_VALUES             = [0, 75]    # 0=off, 75=top-25% RS rating
_STR_VALUES             = [0, 4]     # 0=off, 4=max ATRs above SMA50

# Per-strategy signal-quality param × 3 TP exit structures = 9 combos each.
# rvol kept at strategy default — not swept here.
_PIVOT_LOOKBACK_VALUES = [3, 5, 7]          # bars the close must clear to signal breakout
_BB_KC_VALUES          = [1.5, 2.0, 2.5]   # KC width: tighter = rarer, higher-quality squeeze
_MA_PAIRS              = [(10, 20), (20, 50)]  # (fast, slow) — must be co-varied
_TL_SWING_VALUES       = [3, 5, 7]          # bars each side to confirm a trendline pivot
_PB_BAND_VALUES        = [0.3, 0.4, 0.5]   # pullback ATR band width around breakpoint
_REV_RSI_VALUES        = [30, 35, 40]       # RSI oversold threshold
_NR_PERIOD_VALUES      = [5, 6, 7]          # bars to find narrowest range


def _make_combos(param_key: str, param_values: list, label_fn) -> list[dict]:
    """signal_param × 3 TP structures = 9 combos."""
    return [
        {"_label": f"{label_fn(v)}_{tp['_label']}", param_key: v,
         **{k: val for k, val in tp.items() if k != "_label"}}
        for v in param_values
        for tp in _OPT_REGIME_TP_COMBOS
    ]


def _add_filters(combos: list[dict]) -> list[dict]:
    """Cross-product combos with RSM/STR filter values → 4× combo count."""
    result = []
    for c in combos:
        for rsm in _RSM_VALUES:
            for str_max in _STR_VALUES:
                suffix = (f"_r{rsm}" if rsm > 0 else "") + (f"_s{str_max}" if str_max > 0 else "")
                result.append({**c, "rsm_min": rsm, "str_max": str_max,
                                "_label": c["_label"] + suffix})
    return result


_PIVOT_OPT_COMBOS: list[dict] = _add_filters(_make_combos("lookback",          _PIVOT_LOOKBACK_VALUES, lambda v: f"lb{v}"))
_BB_OPT_COMBOS:    list[dict] = _add_filters(_make_combos("kc_mult",           _BB_KC_VALUES,          lambda v: f"kc{str(v).replace('.','p')}"))
_TL_OPT_COMBOS:    list[dict] = _add_filters(_make_combos("swing_period",      _TL_SWING_VALUES,       lambda v: f"sw{v}"))
_PB_OPT_COMBOS:    list[dict] = _add_filters(_make_combos("pullback_atr_band", _PB_BAND_VALUES,        lambda v: f"pb{str(v).replace('.','p')}"))
_REV_OPT_COMBOS:   list[dict] = _add_filters(_make_combos("rsi_threshold",     _REV_RSI_VALUES,        lambda v: f"rsi{v}"))
_NR_OPT_COMBOS:    list[dict] = _add_filters(_make_combos("nr_period",         _NR_PERIOD_VALUES,      lambda v: f"nr{v}"))

# ma_cross: fast+slow must be co-varied — sweeping slow alone changes the ratio unpredictably
_MA_OPT_COMBOS: list[dict] = _add_filters([
    {"_label": f"f{f}s{s}_{tp['_label']}", "fast_period": f, "slow_period": s,
     **{k: v for k, v in tp.items() if k != "_label"}}
    for f, s in _MA_PAIRS
    for tp in _OPT_REGIME_TP_COMBOS
])

_EDGE_CALMAR = 0.5  # unified gate — pairs below this after optimise are dropped

def _build_opt_combos() -> list[dict]:
    """Cross-product of TP combos × RVOL values."""
    combos = []
    for tp in _OPT_REGIME_TP_COMBOS:
        for rvol in _OPT_REGIME_RVOL_VALUES:
            rvol_tag = f"_rv{str(rvol).replace('.', 'p')}" if rvol > 0 else ""
            combos.append({**tp, "_label": tp["_label"] + rvol_tag, "rvol_min": rvol})
    return combos


def _sync_active_roster_from_optimise(market: str, as_of: date) -> dict:
    from db.models import RegimeOptimiseModel, SessionLocal

    db = SessionLocal()
    try:
        opt_rows = (
            db.query(RegimeOptimiseModel)
            .filter_by(market=market)
            .order_by(RegimeOptimiseModel.strategy, RegimeOptimiseModel.regime)
            .all()
        )
    finally:
        db.close()

    desired_payload = [
        {
            "strategy": row.strategy,
            "regime": row.regime,
            "params": row.params or {},
            "best_combo": row.best_combo or "",
            "is_calmar": float(row.is_calmar or 0.0),
        }
        for row in opt_rows
    ]
    write_active_roster_snapshot(market, as_of, desired_payload)
    result_map = sync_active_roster_file_to_db(
        as_of=as_of,
        source="optimise",
        record_noop=True,
        market_filter=market,
    )
    result = result_map.get(market, {
        "active_count": 0,
        "added": [],
        "changed": [],
        "retired": [],
    })
    summary_lines = [
        f"active roster synced automatically: active={result['active_count']} +{len(result['added'])} ~{len(result['changed'])} -{len(result['retired'])}",
    ]
    for row in result["added"]:
        summary_lines.append(f"+ {row['strategy']}|{row['regime']} -> `{row['new_combo'] or 'n/a'}`")
    for row in result["changed"]:
        summary_lines.append(
            f"~ {row['strategy']}|{row['regime']} `{row['old_combo'] or 'n/a'}` -> `{row['new_combo'] or 'n/a'}`"
        )
    for row in result["retired"]:
        summary_lines.append(f"- {row['strategy']}|{row['regime']} `{row['old_combo'] or 'n/a'}`")

    if len(summary_lines) > 9:
        remaining = len(summary_lines) - 9
        summary_lines = summary_lines[:9] + [f"... and {remaining} more roster changes"]

    return {
        "active_count": result["active_count"],
        "added": result["added"],
        "changed": result["changed"],
        "retired": result["retired"],
        "summary_lines": summary_lines,
    }


def _merge_chart_export_payload(existing_payload: dict | None, market_output: dict) -> dict:
    """Merge one market snapshot into docs/chart_data.json multi-market payload."""
    market_key = str(market_output.get("market") or "").upper()
    markets: dict[str, dict] = {}

    if isinstance(existing_payload, dict):
        existing_markets = existing_payload.get("markets")
        if isinstance(existing_markets, dict):
            for key, payload in existing_markets.items():
                if not isinstance(payload, dict):
                    continue
                normalized_key = str(payload.get("market") or key).upper()
                if normalized_key:
                    markets[normalized_key] = payload
        elif existing_payload.get("market"):
            normalized_key = str(existing_payload.get("market") or "").upper()
            if normalized_key:
                markets[normalized_key] = existing_payload

    if market_key:
        markets[market_key] = market_output

    return {
        "version": 2,
        "default_market": market_key or None,
        "markets": {key: markets[key] for key in sorted(markets)},
    }


def _split_chart_market_payload(payload: dict, fallback_key: str | None = None) -> tuple[str | None, dict | None, dict | None]:
    """Extract one compact market snapshot plus any inline OHLCV blob."""
    if not isinstance(payload, dict):
        return None, None, None

    market_key = str(payload.get("market") or fallback_key or "").upper() or None
    if not market_key:
        return None, None, None

    snapshot = dict(payload)
    inline_ohlcv = snapshot.pop("ohlcv", None)
    if inline_ohlcv is not None:
        snapshot["ohlcv_file"] = snapshot.get("ohlcv_file") or f"./ohlcv_{market_key}.json"
        if isinstance(inline_ohlcv, dict):
            snapshot["ohlcv_symbol_count"] = len(inline_ohlcv)

    return market_key, snapshot, inline_ohlcv if isinstance(inline_ohlcv, dict) else None


def _extract_chart_export_payload(existing_payload: dict | None) -> tuple[dict[str, dict], dict[str, dict]]:
    """Normalize legacy/current chart exports into compact market snapshots."""
    markets: dict[str, dict] = {}
    ohlcv_payloads: dict[str, dict] = {}

    if not isinstance(existing_payload, dict):
        return markets, ohlcv_payloads

    existing_markets = existing_payload.get("markets")
    if isinstance(existing_markets, dict):
        for key, payload in existing_markets.items():
            market_key, snapshot, inline_ohlcv = _split_chart_market_payload(payload, fallback_key=str(key))
            if not market_key or snapshot is None:
                continue
            markets[market_key] = snapshot
            if inline_ohlcv:
                ohlcv_payloads[market_key] = inline_ohlcv
        return markets, ohlcv_payloads

    market_key, snapshot, inline_ohlcv = _split_chart_market_payload(existing_payload)
    if market_key and snapshot is not None:
        markets[market_key] = snapshot
    if market_key and inline_ohlcv:
        ohlcv_payloads[market_key] = inline_ohlcv
    return markets, ohlcv_payloads



def _print_regime_summary(regime_results: list[dict], all_years: list[int], W: int, YC: int) -> None:
    """Print PASS strategies summary + combined yearly totals."""
    passing = [r for r in regime_results if r["acceptable"]]
    REGC = 12  # regime col
    WC   = 7   # wr col
    CC   = 8   # calmar col
    RTC  = 7   # ret% col
    DC   = 7   # dd col
    print("\n" + "=" * W)
    print("  REGIME SUMMARY — COMBINED PORTFOLIO")
    print("=" * W)
    year_hdr = "  ".join(f"{y:>{YC}}" for y in all_years)
    print(f"  {'Strategy':<22}  {'Regime':<{REGC}}  {'WR':>{WC}}  {'Calmar':>{CC}}  {'Ret%':>{RTC}}  {'DD':>{DC}}  {year_hdr}")
    print("  " + "-" * (W - 4))
    for row in passing:
        wr_str  = f"{row['wr']:.0f}%✓"
        cal_str = f"{row['calmar']:.2f}"
        ret_str = f"{row.get('annual_ret', row['calmar'] * row['max_dd']):+.1f}%"
        dd_str  = f"{row['max_dd']:.1f}%"
        yearly  = row.get("yearly") or {}
        year_cols = [_fmt_regime_cell(yearly.get(str(y)), YC) for y in all_years]
        print(f"  {row['strategy']:<22}  {row['regime']:<{REGC}}  {wr_str:>{WC}}  {cal_str:>{CC}}  {ret_str:>{RTC}}  {dd_str:>{DC}}  {'  '.join(year_cols)}")
    print("  " + "-" * (W - 4))
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
    print(f"  {'TOTAL':<22}  {'all':<{REGC}}  {'':>{WC}}  {'':>{CC}}  {'':>{RTC}}  {'':>{DC}}  {'  '.join(total_cells)}")
    print("=" * W)


def _p3_metrics_from_trades(
    trades: list[dict],
    target_regime: str,
    regime_date_map: dict,
    capital: float,
    n_bars: int,
) -> dict:
    """Compute annualized calmar/return/DD/WR/yearly for regime-filtered trades."""
    if not trades:
        return {"calmar": 0.0, "annual_return": 0.0, "max_drawdown": 0.0,
                "trade_count": 0, "win_rate": 0.0, "yearly": {}}

    total_ret = sum(t["pnl"] for t in trades) / capital
    annual_ret = total_ret * (252 / max(n_bars, 1))

    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = wins / len(trades)

    equity = 0.0
    peak   = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x["entry_date"]):
        equity += t["pnl"] / capital
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    calmar = annual_ret / max_dd if max_dd > 0 else annual_ret

    yearly: dict[str, float] = {}
    for t in trades:
        ed = t["entry_date"]
        yr = str(ed.year if hasattr(ed, "year") else str(ed)[:4])
        yearly[yr] = yearly.get(yr, 0.0) + t["pnl"] / capital * 100

    return {
        "calmar":        calmar,
        "annual_return": annual_ret,
        "max_drawdown":  max_dd,
        "trade_count":   len(trades),
        "win_rate":      win_rate,
        "yearly":        yearly,
    }


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
    """Load best params for strategy+regime from regime-optimise results."""
    from db.models import SessionLocal, RegimeOptimiseModel
    db = SessionLocal()
    try:
        row = db.query(RegimeOptimiseModel).filter_by(
            market=market, strategy=strategy_id, regime=regime
        ).first()
        return dict(row.params) if row else None
    finally:
        db.close()


def _run_regime_strategy_par(
    strategy_id: str, strategy_cls, all_dfs: list, params: dict, capital: float,
) -> "tuple[str, list[dict]]":
    """Worker: single-strategy regime backtest for parallel execution."""
    from validation.backtest import run_portfolio_backtest
    try:
        result = run_portfolio_backtest(all_dfs, strategy_cls(), params, initial_capital=capital)
        return strategy_id, result.get("trades", [])
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("regime backtest error %s: %s", strategy_id, exc)
        return strategy_id, []


def _run_backtest_par(strategy_cls, params: dict, all_dfs: list, capital: float) -> list:
    """Worker: single strategy backtest for parallel stability/chart-export sweeps."""
    from validation.backtest import run_portfolio_backtest
    try:
        result = run_portfolio_backtest(all_dfs, strategy_cls(), params, initial_capital=capital)
        return result.get("trades", [])
    except Exception:
        return []


def _run_combo_par(
    combo: dict, strategy_cls, all_dfs: list, opt_base: dict,
    capital: float, regime_date_map: dict, target_regime: str, n_bars: int,
) -> dict:
    """Worker: single combo backtest for parallel optimise-regime sweep."""
    from validation.backtest import run_portfolio_backtest
    label = combo["_label"]
    params = {
        **strategy_cls().default_params,
        **opt_base,
        **{k: v for k, v in combo.items() if not k.startswith("_")},
    }
    try:
        result = run_portfolio_backtest(all_dfs, strategy_cls(), params, initial_capital=capital)
        trades = [
            t for t in result.get("trades", [])
            if regime_date_map.get(t["entry_date"], "choppy") == target_regime
        ]
        metrics = _p3_metrics_from_trades(trades, target_regime, regime_date_map, capital, n_bars)
    except Exception as exc:
        return {
            "_label": label, "params": params, "calmar": 0.0,
            "annual_return": 0.0, "max_drawdown": 0.0,
            "trade_count": 0, "win_rate": 0.0, "yearly": {}, "_error": str(exc),
        }
    metrics["params"] = params
    metrics["_label"] = label
    return metrics


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

    # Save regime labels to DB so regime-optimise uses identical boundaries
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

    print(f"  Fetching {len(universe)} symbols (5yr, batch)...", flush=True)
    bulk = adapter.ohlcv_bulk(universe, y5_start, today)
    all_dfs = []
    for symbol in universe:
        df = bulk.get(symbol, pd.DataFrame())
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

    print(f"\n  {len(all_dfs)}/{len(universe)} symbols ready. Running {len(strategies_map)} strategies in parallel...\n", flush=True)

    EDGE_CALMAR = 0.5

    # Run all strategies in parallel, collect tagged trades
    par_strategy_results = Parallel(n_jobs=min(len(strategies_map), 6), backend="loky")(
        delayed(_run_regime_strategy_par)(
            strategy_id, strategy_cls, all_dfs,
            {**strategy_cls().default_params, **_REGIME_DISCOVERY_PARAMS},
            args.capital,
        )
        for strategy_id, strategy_cls in strategies_map.items()
    )
    all_strategy_trades: dict[str, list[dict]] = {}
    for strategy_id, trades in par_strategy_results:
        tagged = [
            {**t, "regime": regime_date_map.get(t["entry_date"], "choppy"),
             "year": t["entry_date"].year}
            for t in trades
        ]
        all_strategy_trades[strategy_id] = tagged
        print(f"  {strategy_id} — {len(trades)} trades", flush=True)

    all_years     = sorted({t["year"] for trades in all_strategy_trades.values() for t in trades})
    partial_years = {y5_start.year, today.year}
    YC  = 11   # ret%/trades per year cell
    WC  = 7    # WR col
    CC  = 8    # Calmar col
    RC  = 7    # Ret% col
    DC  = 7    # DD col
    WP  = 4 + 22 + 2 + WC + 2 + CC + 2 + RC + 2 + DC + 2 + len(all_years) * (YC + 2)
    W   = WP + 13                                                                 # summary (+Regime col)

    dist_str = "  |  ".join(
        f"{r}: {regime_dist.get(r, 0) / total_bars * 100:.0f}% ({regime_dist.get(r, 0)}d)"
        for r in REGIMES
    )

    print("\n" + "=" * W)
    print(f"  {market.upper()} REGIME DISCOVERY — {today}  |  5yr: {y5_start} → {today}")
    print(f"  Universe: {len(all_dfs)} symbols  |  Strategies: {len(strategies_map)}  |  "
          f"SL=1.5×ATR  TP=3×ATR  (2:1 RR  edge=Calmar>{EDGE_CALMAR:.1f})")
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
                max_dd_5yr, calmar_5yr, annual_ret = 0.0, 0.0, 0.0
            # gate: calmar > EDGE_CALMAR
            acc = bool(r_trades) and calmar_5yr > EDGE_CALMAR
            regime_results.append({
                "strategy":    strategy_id,
                "regime":      regime,
                "wr":          round(agg_wr, 2),
                "calmar":      round(calmar_5yr, 2),
                "annual_ret":  round(annual_ret * 100, 1),
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
        print(f"  {'Strategy':<22}  {'WR':>{WC}}  {'Calmar':>{CC}}  {'Ret%':>{RC}}  {'DD':>{DC}}  {year_hdr}")
        print("  " + "-" * (WP - 4))
        for row in by_regime_disc[regime]:
            if not row["yearly"]:
                blanks = "  ".join(f"{'—':>{YC}}" for _ in all_years)
                print(f"  {row['strategy']:<22}  {'—':>{WC}}  {'—':>{CC}}  {'—':>{RC}}  {'—':>{DC}}  {blanks}")
                continue
            wr_str  = f"{row['wr']:.0f}%{'✓' if row['acceptable'] else '✗'}"
            cal_str = f"{row['calmar']:.2f}"
            ret_str = f"{row['annual_ret']:+.1f}%"
            dd_str  = f"{row['max_dd']:.1f}%"
            year_cols = [_fmt_regime_cell(row["yearly"].get(str(y)), YC) for y in all_years]
            print(f"  {row['strategy']:<22}  {wr_str:>{WC}}  {cal_str:>{CC}}  {ret_str:>{RC}}  {dd_str:>{DC}}  {'  '.join(year_cols)}")

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
        f"- Run `python run.py {market} regime-optimise` on PASS pairs to tune rvol + TP.",
        "- Change one thing at a time.",
        "",
    ])

    content = "\n".join(lines)
    for path in (latest_path, history_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    regime_summary = []
    if passing:
        pairs_str = ", ".join(
            f"{r['strategy']}|{r['regime']} cal={r['calmar']:.2f} dd={r['max_dd']:.1f}%"
            for r in sorted(passing, key=lambda r: -(r.get("calmar") or 0))
        )
        regime_summary.append(f"PASS ({len(passing)}): {pairs_str}")
    else:
        regime_summary.append("PASS (0): no pairs exceeded gate")
    regime_summary.append(f"FAIL: {len(regime_results) - len(passing)} pairs below {edge_calmar} gate")
    regime_summary.append(f"Universe: {n_symbols} symbols | {n_strategies} strategies")
    _write_run_log(market, "regime", regime_summary,
                   next_step="regime-optimise on all PASS pairs")

    print(f"  Markdown: {latest_path}")
    return str(latest_path)


def _print_saved_report(path: str, missing_hint: str) -> None:
    from pathlib import Path

    report_path = Path(path)
    if not report_path.exists():
        print(f"  {missing_hint}")
        return
    print()
    print(report_path.read_text(encoding="utf-8"))


def _parse_stability_winners(path: str) -> list[dict]:
    from pathlib import Path

    report_path = Path(path)
    if not report_path.exists():
        return []

    lines = report_path.read_text(encoding="utf-8").splitlines()
    rows: list[dict] = []
    in_table = False
    for line in lines:
        if line.strip() == "## Optimised Winners":
            in_table = True
            continue
        if not in_table:
            continue
        if not line.strip():
            if rows:
                break
            continue
        if not line.startswith("|"):
            continue
        if line.startswith("| Strategy |") or line.startswith("|---|"):
            continue
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(parts) != 11:
            continue
        rows.append({
            "strategy": parts[0],
            "regime": parts[1],
            "base_cal": float(parts[2]),
            "opt_cal": float(parts[3]),
            "episodes": int(parts[4]),
            "active": int(parts[5]),
            "positive": int(parts[6]),
            "median_episode": parts[7],
            "worst_episode": parts[8],
            "best_share": parts[9],
            "verdict": parts[10],
        })
    return rows


def _export_research_report(market: str, as_of) -> str:
    from pathlib import Path
    from datetime import datetime
    from db.models import SessionLocal, RegimeMapModel, RegimeOptimiseModel

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    latest_path = Path("reports") / f"{market}_report_latest.md"
    history_path = Path("reports") / "history" / f"{timestamp}_{market}_report.md"

    stability_rows = _parse_stability_winners(f"reports/{market}_stability_latest.md")

    db = SessionLocal()
    try:
        pass_rows = db.query(RegimeMapModel).filter_by(market=market, acceptable=True).all()
        opt_rows = db.query(RegimeOptimiseModel).filter_by(market=market).all()
    finally:
        db.close()

    opt_map = {(row.strategy, row.regime): row for row in opt_rows}
    approved = [row for row in stability_rows if row["verdict"] == "stable"]
    watchlist = [row for row in stability_rows if row["verdict"] == "mixed"]
    rejected = [row for row in stability_rows if row["verdict"] == "fragile"]

    lines = [
        f"# {market.upper()} Research Report",
        "",
        f"- As of: `{as_of}`",
        f"- Saved regime winners: `{len(pass_rows)}`",
        f"- Saved optimised winners: `{len(opt_rows)}`",
        f"- Stability-reviewed winners: `{len(stability_rows)}`",
        "",
        "## Current Conclusion",
        "",
    ]

    if approved:
        lines.append(f"- Approved now: `{', '.join(f'{row['strategy']}|{row['regime']}' for row in approved)}`")
    else:
        lines.append("- Approved now: `_none_`")
    if watchlist:
        lines.append(f"- Watchlist: `{', '.join(f'{row['strategy']}|{row['regime']}' for row in watchlist)}`")
    else:
        lines.append("- Watchlist: `_none_`")
    unstable_regimes = sorted({row["regime"] for row in rejected})
    lines.append(
        "- Interpretation: "
        + (
            "current robust edge is concentrated in downtrend; uptrend and choppy are not approved yet."
            if unstable_regimes
            else "approved winners are holding across the reviewed regime episodes."
        )
    )

    lines.extend([
        "",
        "## Approved",
        "",
    ])
    if approved:
        for row in approved:
            opt = opt_map.get((row["strategy"], row["regime"]))
            combo = opt.best_combo if opt and opt.best_combo else "—"
            lines.append(
                f"- `{row['strategy']} | {row['regime']}` — verdict=`{row['verdict']}` "
                f"opt-cal=`{row['opt_cal']:.2f}` active=`{row['active']}/{row['episodes']}` "
                f"median-ep=`{row['median_episode']}` worst-ep=`{row['worst_episode']}` combo=`{combo}`"
            )
    else:
        lines.append("_None._")

    lines.extend([
        "",
        "## Watchlist",
        "",
    ])
    if watchlist:
        for row in watchlist:
            opt = opt_map.get((row["strategy"], row["regime"]))
            combo = opt.best_combo if opt and opt.best_combo else "—"
            lines.append(
                f"- `{row['strategy']} | {row['regime']}` — verdict=`{row['verdict']}` "
                f"opt-cal=`{row['opt_cal']:.2f}` active=`{row['active']}/{row['episodes']}` "
                f"median-ep=`{row['median_episode']}` worst-ep=`{row['worst_episode']}` combo=`{combo}`"
            )
    else:
        lines.append("_None._")

    lines.extend([
        "",
        "## Rejected For Now",
        "",
    ])
    if rejected:
        for row in rejected:
            lines.append(
                f"- `{row['strategy']} | {row['regime']}` — verdict=`{row['verdict']}` "
                f"opt-cal=`{row['opt_cal']:.2f}` best-share=`{row['best_share']}` "
                f"median-ep=`{row['median_episode']}`"
            )
    else:
        lines.append("_None._")

    lines.extend([
        "",
        "## Next Step",
        "",
        "- Run `chart-export` and inspect the approved list first, then the watchlist.",
        "- Compare best and worst regime episodes before changing strategy rules again.",
        "",
        "## Sources",
        "",
        f"- `reports/{market}_regime_latest.md`",
        f"- `reports/{market}_optimise_latest.md`",
        f"- `reports/{market}_stability_latest.md`",
    ])

    content = "\n".join(lines) + "\n"
    for path in (latest_path, history_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return str(latest_path)


def cmd_regime_report(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Print the latest saved regime markdown report."""
    market = adapter.market_id
    _print_saved_report(
        f"reports/{market}_regime_latest.md",
        "No saved regime report. Run regime first.",
    )


def cmd_optimise_report(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Print the latest saved regime-optimise markdown report with full grid results."""
    market = adapter.market_id
    _print_saved_report(
        f"reports/{market}_optimise_latest.md",
        "No saved optimise report. Run regime-optimise first.",
    )


def cmd_report(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Build and print the latest research summary report."""
    market = adapter.market_id
    report_path = _export_research_report(market, date.today())
    _write_run_log(
        market,
        "report",
        [f"rebuilt `reports/{market}_report_latest.md` from latest regime, optimise, and stability artifacts"],
        next_step="chart-export, then inspect approved and watchlist pairs visually",
    )
    _print_saved_report(report_path, "No saved report.")


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

    strategy_filter = getattr(args, "strategy_filter", None)
    if strategy_filter:
        pairs = [(s, r, c) for s, r, c in pairs if s == strategy_filter]
        if not pairs:
            print(f"  No PASS pairs found for strategy '{strategy_filter}'. Run regime first.")
            return
        print(f"  Filtering to strategy: {strategy_filter}")

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
    print(f"\n  Fetching {len(universe)} symbols (5yr, batch)...", flush=True)
    bulk = adapter.ohlcv_bulk(universe, y5_start, today)
    all_dfs = []
    for symbol in universe:
        df = bulk.get(symbol, pd.DataFrame())
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
    if strategy_filter:
        print(f"  Strategy filter: {strategy_filter}")
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

        pair_combos = {
            "pivot_breakout":      _PIVOT_OPT_COMBOS,
            "bb_squeeze":          _BB_OPT_COMBOS,
            "ma_cross":            _MA_OPT_COMBOS,
            "pullback_buy":        _PB_OPT_COMBOS,
            "reversal":            _REV_OPT_COMBOS,
        }.get(strategy_id, _build_opt_combos())
        n_combos = len(pair_combos)
        pair_t0 = time.time()
        opt_base = _OPT_REGIME_BASE

        n_workers = min(n_combos, 16)
        print(f"  Running {n_combos} combos ({n_workers} parallel workers)...", flush=True)
        raw_metrics = Parallel(n_jobs=n_workers, backend="loky")(
            delayed(_run_combo_par)(
                combo, strategy_cls, all_dfs, opt_base,
                args.capital, regime_date_map, target_regime, n_bars,
            )
            for combo in pair_combos
        )
        elapsed_par = time.time() - pair_t0
        print(f"  Done in {elapsed_par:.0f}s", flush=True)

        combo_results: list[dict] = []
        best: dict | None = None
        for metrics in raw_metrics:
            if "_error" in metrics:
                print(f"  {metrics['_label']:<32} ERROR: {metrics['_error']}")
                continue
            cal = float(metrics.get("calmar", 0) or 0)
            ret = float(metrics.get("annual_return", 0) or 0) * 100
            dd  = float(metrics.get("max_drawdown", 0) or 0) * 100
            tr  = int(metrics.get("trade_count", 0) or 0)
            wr  = float(metrics.get("win_rate", 0) or 0) * 100
            yearly = metrics.get("yearly", {})
            yr_cols = "  ".join(
                f"{yearly[y]:>+{YW}.1f}%" if y in yearly else f"{'—':>{YW}}"
                for y in all_years
            )
            best_marker = " *" if best is None or cal > float(best.get("calmar", 0) or 0) else "  "
            print(f"  {best_marker}{metrics['_label']:<30} {cal:>8.2f} {ret:>+8.1f}% {dd:>6.1f}% {tr:>7d} {wr:>5.0f}%  {yr_cols}", flush=True)
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

    # Save best params to DB — only pairs that beat the gate
    passing = [r for r in results if r["best_calmar"] >= _EDGE_CALMAR]
    dropped = [r for r in results if r["best_calmar"] < _EDGE_CALMAR]
    if dropped:
        for r in dropped:
            print(f"  ✗ DROP {r['strategy']}|{r['regime']} — best calmar {r['best_calmar']:.2f} < gate {_EDGE_CALMAR}")
    db = SessionLocal()
    try:
        db.query(RegimeOptimiseModel).filter_by(market=market).delete()
        for r in passing:
            db.add(RegimeOptimiseModel(
                market=market,
                strategy=r["strategy"],
                regime=r["regime"],
                params=r["params"],
                best_combo=r["best_label"],
                is_calmar=r["best_calmar"],
                is_annual_return=r["best_return"],
                is_win_rate=r["best_wr"],
                is_trade_count=r["best_trades"],
                oos_pass=True,
            ))
        db.commit()
        logger.info("saved %d passing regime-optimise results for %s (dropped %d)", len(passing), market, len(dropped))
    finally:
        db.close()

    # Summary table
    print(f"\n{'='*W}")
    print(f"  SUMMARY")
    print(f"  {'Strategy':<22} {'Regime':<12} {'Baseline':>9} {'Best Cal':>9} {'Best Ret%':>10} {'Best DD':>8} {'Improved':>9}  Best Combo")
    print(f"  {'-'*(W-4)}")
    for r in sorted(results, key=lambda x: -x["best_calmar"]):
        delta   = r["best_calmar"] - r["baseline_calmar"]
        ret_pct = r["best_return"] * 100
        dd_pct  = r["best_dd"] * 100
        print(f"  {r['strategy']:<22} {r['regime']:<12} {r['baseline_calmar']:>9.2f} "
              f"{r['best_calmar']:>9.2f} {ret_pct:>+9.1f}% {dd_pct:>7.1f}% {delta:>+9.2f}  {r['best_label']}")
    print(f"{'='*W}\n")

    report_path, summary_lines = _export_optimise_regime_markdown(results, market, today)
    if strategy_filter:
        summary_lines.append(f"active roster sync skipped because optimise ran with --strategy={strategy_filter}")
    else:
        sync_result = _sync_active_roster_from_optimise(market, today)
        summary_lines.extend(sync_result["summary_lines"])
    _write_run_log(
        market,
        "optimise",
        summary_lines,
        next_step="review active history, then run scan to generate live signals",
    )
    print(f"\n  Markdown: {report_path}")


def _export_optimise_regime_markdown(results: list[dict], market: str, as_of) -> tuple[str, list[str]]:
    from pathlib import Path
    from datetime import datetime

    timestamp   = datetime.now().strftime("%Y-%m-%d_%H%M")
    latest_path = Path("reports") / f"{market}_optimise_latest.md"
    history_dir = Path("reports") / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / f"{timestamp}_{market}_optimise.md"

    lines = [
        f"# {market.upper()} Optimise Regime",
        "",
        f"- As of: `{as_of}`",
        f"- Gate: `calmar ≥ {_EDGE_CALMAR}` to save params",
        f"- Grids: pivot=lookback×rvol | bb=kc_mult×rvol | ma=slow_period×rvol",
        f"- Pairs tested: `{len(results)}`",
        "",
        "## Results",
        "",
        "| Strategy | Regime | Baseline Cal | Best Cal | Best Ret | Best DD | Best Combo |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda x: -x["best_calmar"]):
        lines.append(
            f"| {r['strategy']} | {r['regime']} | {r['baseline_calmar']:.2f} | "
            f"{r['best_calmar']:.2f} | {r['best_return']*100:+.1f}% | "
            f"{r['best_dd']*100:.1f}% | `{r['best_label']}` |"
        )

    for r in results:
        all_years_md = sorted({y for m in r["all_combos"] for y in (m.get("yearly") or {})})
        yr_hdr = " | ".join(str(y) for y in all_years_md)
        lines += [
            "",
            f"## {r['strategy']} | {r['regime']}  (baseline calmar={r['baseline_calmar']:.2f})",
            "",
            f"| Combo | Calmar | Ret%ann | DD | Trades | WR | {yr_hdr} |",
            "|---|---|---|---|---|---|" + "---|" * len(all_years_md),
        ]
        for m in r["all_combos"]:
            label   = m.get("_label", "?")
            cal     = m.get("calmar", 0) or 0
            ret     = (m.get("annual_return", 0) or 0) * 100
            dd      = (m.get("max_drawdown", 0) or 0) * 100
            trades  = m.get("trade_count", 0) or 0
            wr      = (m.get("win_rate", 0) or 0) * 100
            best_mk = " ✓" if label == r["best_label"] else ""
            yearly  = m.get("yearly") or {}
            yr_cells = " | ".join(
                f"{yearly[y]:+.1f}%" if y in yearly else "—"
                for y in all_years_md
            )
            lines.append(
                f"| `{label}`{best_mk} | {cal:.2f} | {ret:+.1f}% | {dd:.1f}% | {trades} | {wr:.0f}% | {yr_cells} |"
            )

    lines += [
        "",
        "## AI Suggestions",
        "",
        "<!-- AI reads this file and writes specific next-run suggestions here -->",
        "",
        "## Next Loop",
        "",
        "- Run `python run.py th regime-optimise` after changing TP combos, RVOL values, or strategy rules.",
        "- Next step after good params: add STR filter to rule out overextended entries.",
        "- Change one thing at a time.",
    ]

    suggestions = _rule_based_suggestions(results)
    suggestion_block = "\n".join(suggestions)
    content = "\n".join(lines).replace(
        "<!-- AI reads this file and writes specific next-run suggestions here -->",
        suggestion_block,
    ) + "\n"
    latest_path.write_text(content, encoding="utf-8")
    history_path.write_text(content, encoding="utf-8")

    summary_lines = []
    for r in sorted(results, key=lambda x: -x["best_calmar"]):
        gate = "PASS" if r["best_calmar"] >= _EDGE_CALMAR else "DROP"
        summary_lines.append(
            f"{r['strategy']}|{r['regime']}: `{r['best_label']}` "
            f"cal={r['best_calmar']:.2f} ret={r['best_return']*100:+.1f}% "
            f"dd={r['best_dd']*100:.1f}% [{gate}]"
        )
    return str(latest_path), summary_lines


def _build_regime_episodes(label_rows: list) -> dict[str, list[dict]]:
    from core.regime import REGIMES

    episodes: dict[str, list[dict]] = {regime: [] for regime in REGIMES}
    if not label_rows:
        return episodes

    ordered = sorted(label_rows, key=lambda row: row.date)
    current_regime = ordered[0].regime
    start_date = ordered[0].date
    end_date = ordered[0].date
    bars = 1

    def _flush(regime: str, start, end, count: int) -> None:
        idx = len(episodes[regime]) + 1
        episodes[regime].append({
            "label": f"{regime[:2]}{idx}",
            "regime": regime,
            "start_date": start,
            "end_date": end,
            "bars": count,
        })

    for row in ordered[1:]:
        if row.regime == current_regime:
            end_date = row.date
            bars += 1
            continue
        _flush(current_regime, start_date, end_date, bars)
        current_regime = row.regime
        start_date = row.date
        end_date = row.date
        bars = 1

    _flush(current_regime, start_date, end_date, bars)
    return episodes


def _flatten_regime_periods(label_rows: list) -> list[dict]:
    periods: list[dict] = []
    episodes = _build_regime_episodes(label_rows)
    for rows in episodes.values():
        for row in rows:
            start = row["start_date"]
            end = row["end_date"]
            periods.append({
                "label": row["label"],
                "regime": row["regime"],
                "start_date": start.isoformat() if hasattr(start, "isoformat") else str(start),
                "end_date": end.isoformat() if hasattr(end, "isoformat") else str(end),
                "bars": int(row["bars"]),
            })
    periods.sort(key=lambda row: row["start_date"])
    return periods


def _episode_rows_from_trades(
    trades: list[dict],
    episodes: list[dict],
    capital: float,
) -> list[dict]:
    rows = []
    for episode in episodes:
        episode_trades = [
            t for t in trades
            if episode["start_date"] <= t["entry_date"] <= episode["end_date"]
        ]
        ret_pct = sum(float(t["pnl"]) for t in episode_trades) / capital * 100 if episode_trades else 0.0
        win_count = sum(1 for t in episode_trades if float(t["pnl"]) > 0)
        trade_count = len(episode_trades)
        rows.append({
            **episode,
            "trade_count": trade_count,
            "ret_pct": round(ret_pct, 2),
            "win_rate": round(win_count / trade_count * 100, 1) if trade_count else None,
        })
    return rows


def _stability_summary(
    strategy: str,
    regime: str,
    episode_rows: list[dict],
) -> dict:
    import statistics

    active = [row for row in episode_rows if row["trade_count"] > 0]
    active_rets = [row["ret_pct"] for row in active]
    total_ret = round(sum(active_rets), 2)
    best_ret = max(active_rets) if active_rets else 0.0
    worst_ret = min(active_rets) if active_rets else 0.0
    median_ret = statistics.median(active_rets) if active_rets else 0.0
    positive_count = sum(1 for value in active_rets if value > 0)
    episode_count = len(episode_rows)
    active_count = len(active)
    active_ratio = active_count / episode_count if episode_count else 0.0
    positive_ratio = positive_count / active_count if active_count else 0.0
    best_share = (best_ret / total_ret * 100.0) if total_ret > 0 and best_ret > 0 else None
    total_trades = sum(row["trade_count"] for row in active)

    if active_count == 0 or total_trades == 0:
        verdict = "inactive"
    elif total_ret <= 0:
        verdict = "fragile"
    elif positive_ratio >= 0.60 and median_ret >= 0 and (best_share is None or best_share <= 70):
        verdict = "stable"
    elif positive_ratio >= 0.50 and median_ret >= -1.0 and (best_share is None or best_share <= 100):
        verdict = "mixed"
    else:
        verdict = "fragile"

    return {
        "strategy": strategy,
        "regime": regime,
        "episode_count": episode_count,
        "active_episode_count": active_count,
        "positive_episode_count": positive_count,
        "active_episode_ratio": round(active_ratio * 100, 1),
        "positive_episode_ratio": round(positive_ratio * 100, 1),
        "total_ret_pct": total_ret,
        "median_episode_ret_pct": round(median_ret, 2),
        "best_episode_ret_pct": round(best_ret, 2),
        "worst_episode_ret_pct": round(worst_ret, 2),
        "best_episode_share_pct": round(best_share, 1) if best_share is not None else None,
        "trade_count": total_trades,
        "verdict": verdict,
        "episodes": episode_rows,
    }


def _export_stability_markdown(
    market: str,
    as_of,
    regime_episode_counts: dict[str, int],
    baseline_rows: list[dict],
    optimised_rows: list[dict],
) -> str:
    from pathlib import Path
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    latest_path = Path("reports") / f"{market}_stability_latest.md"
    history_path = Path("reports") / "history" / f"{timestamp}_{market}_stability.md"

    shortlist = [row for row in optimised_rows if row["verdict"] in {"stable", "mixed"}]
    lines = [
        f"# {market.upper()} Stability Report",
        "",
        f"- As of: `{as_of}`",
        f"- Regime episodes: `uptrend={regime_episode_counts.get('uptrend', 0)}` "
        f"`choppy={regime_episode_counts.get('choppy', 0)}` "
        f"`downtrend={regime_episode_counts.get('downtrend', 0)}`",
        f"- Baseline pairs reviewed: `{len(baseline_rows)}`",
        f"- Optimised pairs reviewed: `{len(optimised_rows)}`",
        "",
        "## Shortlist",
        "",
    ]

    if shortlist:
        for row in sorted(shortlist, key=lambda item: (item["verdict"] != "stable", -item["total_ret_pct"])):
            share = f"{row['best_episode_share_pct']:.0f}%" if row["best_episode_share_pct"] is not None else "—"
            lines.append(
                f"- `{row['strategy']} | {row['regime']}` — verdict=`{row['verdict']}` "
                f"episodes=`{row['positive_episode_count']}/{row['active_episode_count']}` "
                f"median=`{row['median_episode_ret_pct']:+.1f}%` worst=`{row['worst_episode_ret_pct']:+.1f}%` "
                f"best-share=`{share}`"
            )
    else:
        lines.append("_No optimised pairs cleared the stability shortlist yet._")

    lines.extend([
        "",
        "## Optimised Winners",
        "",
        "| Strategy | Regime | Base Cal | Opt Cal | Episodes | Active | Pos | Median Ep | Worst Ep | Best Share | Verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ])
    for row in sorted(optimised_rows, key=lambda item: (item["verdict"] != "stable", -item["opt_calmar"], -item["total_ret_pct"])):
        share = f"{row['best_episode_share_pct']:.0f}%" if row["best_episode_share_pct"] is not None else "—"
        lines.append(
            f"| {row['strategy']} | {row['regime']} | {row['base_calmar']:.2f} | {row['opt_calmar']:.2f} | "
            f"{row['episode_count']} | {row['active_episode_count']} | {row['positive_episode_count']} | "
            f"{row['median_episode_ret_pct']:+.1f}% | {row['worst_episode_ret_pct']:+.1f}% | {share} | {row['verdict']} |"
        )

    lines.extend([
        "",
        "## Baseline Map",
        "",
        "| Strategy | Regime | Calmar | Episodes | Active | Pos | Median Ep | Worst Ep | Best Share | Verdict |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ])
    for row in sorted(baseline_rows, key=lambda item: (item["regime"], item["verdict"] != "stable", -item["baseline_calmar"], -item["total_ret_pct"])):
        share = f"{row['best_episode_share_pct']:.0f}%" if row["best_episode_share_pct"] is not None else "—"
        lines.append(
            f"| {row['strategy']} | {row['regime']} | {row['baseline_calmar']:.2f} | {row['episode_count']} | "
            f"{row['active_episode_count']} | {row['positive_episode_count']} | {row['median_episode_ret_pct']:+.1f}% | "
            f"{row['worst_episode_ret_pct']:+.1f}% | {share} | {row['verdict']} |"
        )

    for row in sorted(optimised_rows, key=lambda item: (item["verdict"] != "stable", -item["opt_calmar"], -item["total_ret_pct"])):
        lines.extend([
            "",
            f"## {row['strategy']} | {row['regime']}",
            "",
            f"- Base calmar: `{row['base_calmar']:.2f}`",
            f"- Optimised calmar: `{row['opt_calmar']:.2f}`",
            f"- Verdict: `{row['verdict']}`",
            f"- Active episodes: `{row['active_episode_count']}/{row['episode_count']}`",
            "",
            "| Episode | Dates | Bars | Trades | Ret% | WR |",
            "|---|---|---|---|---|---|",
        ])
        for episode in row["episodes"]:
            wr = f"{episode['win_rate']:.0f}%" if episode["win_rate"] is not None else "—"
            lines.append(
                f"| `{episode['label']}` | `{episode['start_date']}` → `{episode['end_date']}` | "
                f"{episode['bars']} | {episode['trade_count']} | {episode['ret_pct']:+.1f}% | {wr} |"
            )

    lines.extend([
        "",
        "## AI Suggestions",
        "",
        "- Promote pairs with `stable` verdict first; `mixed` pairs need chart review before trust.",
        "- Drop pairs where one episode explains most of the total return; that is concentrated edge, not stability.",
        "- Use `chart-export` after this report to inspect the stable shortlist visually.",
        "",
    ])

    content = "\n".join(lines) + "\n"
    for path in (latest_path, history_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return str(latest_path)


def cmd_stability_report(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Evaluate regime-specific stability across separate benchmark regime episodes."""
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    from core.regime import REGIMES
    from validation.backtest import run_portfolio_backtest, _precompute_indicators
    from db.models import SessionLocal, RegimeLabelModel, RegimeMapModel, RegimeOptimiseModel

    market = adapter.market_id
    today = date.today()
    y5_start = today - timedelta(days=1825)

    db = SessionLocal()
    try:
        label_rows = db.query(RegimeLabelModel).filter_by(market=market).all()
        map_rows = db.query(RegimeMapModel).filter_by(market=market).all()
        opt_rows = db.query(RegimeOptimiseModel).filter_by(market=market).all()
    finally:
        db.close()

    if not label_rows or not map_rows:
        print("  No regime data. Run regime first.")
        return

    episode_map = _build_regime_episodes(label_rows)
    regime_episode_counts = {regime: len(episode_map.get(regime, [])) for regime in REGIMES}
    if not any(regime_episode_counts.values()):
        print("  No regime episodes available. Run regime first.")
        return

    bm_close = _fetch_benchmark(adapter, y5_start, today)
    if bm_close is None:
        print("  ERROR: no benchmark data.")
        return

    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    print(f"\n  Fetching {len(universe)} symbols (5yr, bulk) ...", flush=True)
    bulk = adapter.ohlcv_bulk(universe, y5_start, today)
    all_dfs = []
    for symbol in universe:
        df = bulk.get(symbol, pd.DataFrame())
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

    strategies_map = StrategyRegistry.for_market(market)
    regime_map = {(row.strategy, row.regime): row for row in map_rows}

    print("\n  Running baseline stability checks (parallel) ...", flush=True)
    baseline_items = list(strategies_map.items())
    base_trades_list = Parallel(n_jobs=min(len(baseline_items), 8), backend="loky")(
        delayed(_run_backtest_par)(
            strategy_cls,
            {**strategy_cls().default_params, **_REGIME_DISCOVERY_PARAMS},
            all_dfs, args.capital,
        )
        for _, strategy_cls in baseline_items
    )
    baseline_rows = []
    for (strategy_id, _), trades in zip(baseline_items, base_trades_list):
        for regime in REGIMES:
            episode_rows = _episode_rows_from_trades(trades, episode_map.get(regime, []), args.capital)
            summary = _stability_summary(strategy_id, regime, episode_rows)
            base_row = regime_map.get((strategy_id, regime))
            summary["baseline_calmar"] = round(float(base_row.calmar or 0), 2) if base_row else 0.0
            baseline_rows.append(summary)

    print("\n  Running optimised winner stability checks (parallel) ...", flush=True)
    valid_opts = [(opt, strategies_map[opt.strategy]) for opt in opt_rows if opt.strategy in strategies_map]
    opt_trades_list = Parallel(n_jobs=min(len(valid_opts), 16), backend="loky")(
        delayed(_run_backtest_par)(cls, dict(opt.params or {}), all_dfs, args.capital)
        for opt, cls in valid_opts
    ) if valid_opts else []
    optimised_rows = []
    for (opt, _), trades in zip(valid_opts, opt_trades_list):
        episode_rows = _episode_rows_from_trades(trades, episode_map.get(opt.regime, []), args.capital)
        summary = _stability_summary(opt.strategy, opt.regime, episode_rows)
        base_row = regime_map.get((opt.strategy, opt.regime))
        summary["base_calmar"] = round(float(base_row.calmar or 0), 2) if base_row else 0.0
        summary["opt_calmar"] = round(float(opt.is_calmar or 0), 2)
        summary["best_combo"] = opt.best_combo or ""
        optimised_rows.append(summary)

    report_path = _export_stability_markdown(
        market=market,
        as_of=today,
        regime_episode_counts=regime_episode_counts,
        baseline_rows=baseline_rows,
        optimised_rows=optimised_rows,
    )

    summary_lines = []
    shortlist = [row for row in optimised_rows if row["verdict"] in {"stable", "mixed"}]
    summary_lines.append(
        "episodes: "
        + ", ".join(f"{regime}={regime_episode_counts.get(regime, 0)}" for regime in REGIMES)
    )
    if shortlist:
        for row in sorted(shortlist, key=lambda item: (item["verdict"] != "stable", -item["opt_calmar"])):
            summary_lines.append(
                f"{row['strategy']}|{row['regime']}: verdict={row['verdict']} "
                f"active={row['active_episode_count']}/{row['episode_count']} "
                f"median={row['median_episode_ret_pct']:+.1f}% "
                f"worst={row['worst_episode_ret_pct']:+.1f}%"
            )
    else:
        summary_lines.append("no optimised pairs passed the stability shortlist")
    _write_run_log(
        market,
        "stability-report",
        summary_lines,
        next_step="chart-export on stable shortlist, then inspect best and worst regime episodes",
    )
    print(f"\n  Markdown: {report_path}")


def _rule_based_suggestions(results: list[dict]) -> list[str]:
    """Generate actionable next-step bullets from regime-optimise results."""
    lines = []
    for r in results:
        strat      = r["strategy"]
        regime     = r["regime"]
        best_cal   = r["best_calmar"]
        baseline   = r["baseline_calmar"]
        best_label = r["best_label"]
        combos     = r.get("all_combos", [])

        if best_cal < _EDGE_CALMAR:
            lines.append(
                f"- **DROP** `{strat}|{regime}` — best calmar {best_cal:.2f} still below gate "
                f"{_EDGE_CALMAR}. Not viable with current params."
            )
            continue

        delta = best_cal - baseline
        confidence = "HIGH" if best_cal >= 1.5 else "MEDIUM" if best_cal >= 0.8 else "MARGINAL"
        lines.append(
            f"- **LOCK ({confidence})** `{strat}|{regime}` → `{best_label}` "
            f"(calmar={best_cal:.2f}, Δ{delta:+.2f} vs baseline)"
        )

        # Yearly stability check
        best_m = next((m for m in combos if m.get("_label") == best_label), None)
        if best_m:
            yearly = best_m.get("yearly") or {}
            neg_years = sorted(y for y, v in yearly.items() if v < -2)
            if len(neg_years) >= 2:
                lines.append(
                    f"  - ⚠ Negative in {', '.join(neg_years)} — structural drag, "
                    f"check regime boundary or strategy logic for those years."
                )

        # RVOL helps or hurts?
        no_rvol_cals = [
            (m.get("calmar") or 0)
            for m in combos
            if "rv0p0" in m.get("_label", "") or m.get("_label", "").endswith("_rv0p0")
        ]
        rvol_cals = [
            (m.get("calmar") or 0)
            for m in combos
            if "rv1p5" in m.get("_label", "") or "rv2p0" in m.get("_label", "")
        ]
        if no_rvol_cals and rvol_cals:
            best_no_rvol = max(no_rvol_cals)
            best_with_rvol = max(rvol_cals)
            if best_no_rvol > best_with_rvol * 1.15:
                lines.append(
                    f"  - RVOL filter hurts `{strat}` — no-rvol combo scores higher. "
                    f"Strategy fires in low-volume setups."
                )

    if not lines:
        lines.append("- No pairs passed the gate. Loosen _OPT_REGIME_MIN_CALMAR or re-run regime.")
    return lines


def _write_agent_snapshot(
    market: str,
    command: str,
    summary_lines: list[str],
    next_step: str | None,
    timestamp: str,
    pass_rows: list,
    opt_map: dict,
    entry_map: dict,
) -> None:
    from pathlib import Path

    reports_dir = Path("reports")
    review_files = [
        "CLAUDE.md",
        str(reports_dir / "run_log.md"),
        str(reports_dir / "agent_context.md"),
        str(reports_dir / "agent_state.json"),
    ]
    for suffix in ("regime", "optimise", "stability", "report", "chart_export"):
        path = reports_dir / f"{market}_{suffix}_latest.md"
        if path.exists():
            review_files.append(str(path))

    current_best = []
    for row in sorted(pass_rows, key=lambda x: -(x.calmar or 0)):
        opt = opt_map.get((row.strategy, row.regime))
        ent = entry_map.get((row.strategy, row.regime))
        current_best.append({
            "strategy": row.strategy,
            "regime": row.regime,
            "base_calmar": round(float(row.calmar or 0), 2),
            "opt_calmar": round(float(opt.is_calmar or 0), 2) if opt and opt.is_calmar is not None else None,
            "opt_ret_ann_pct": round(float(opt.is_annual_return or 0) * 100, 1) if opt and opt.is_annual_return is not None else None,
            "max_dd_pct": round(float(row.max_dd or 0), 1) if row.max_dd is not None else None,
            "best_combo": opt.best_combo if opt and opt.best_combo else None,
            "entry_mode": ent.best_entry if ent else None,
        })

    state = {
        "generated_at": timestamp,
        "market": market,
        "last_command": command,
        "next_step": next_step,
        "summary_lines": summary_lines,
        "review_files": review_files,
        "current_best": current_best,
    }
    (reports_dir / "agent_state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    md_lines = [
        "# Agent Context",
        "",
        f"- Generated: `{timestamp}`",
        f"- Market: `{market}`",
        f"- Last command: `{command}`",
        f"- Next step: `{next_step or 'review latest report'}`",
        "",
        "## Required Reads",
        "",
        "- `CLAUDE.md`",
        "- `reports/run_log.md`",
        "- `reports/agent_state.json`",
    ]
    for path in review_files[4:]:
        md_lines.append(f"- `{path}`")

    md_lines.extend([
        "",
        "## Last Run Summary",
        "",
    ])
    md_lines.extend(f"- {line}" for line in summary_lines)
    md_lines.extend([
        "",
        "## Current Best",
        "",
        "| Strategy | Regime | Base Cal | Opt Cal | Ret%ann | DD | Combo | Entry |",
        "|---|---|---|---|---|---|---|---|",
    ])

    if current_best:
        for row in current_best:
            opt_cal = f"{row['opt_calmar']:.2f}" if row["opt_calmar"] is not None else "—"
            opt_ret = f"{row['opt_ret_ann_pct']:+.1f}%" if row["opt_ret_ann_pct"] is not None else "—"
            dd = f"{row['max_dd_pct']:.1f}%" if row["max_dd_pct"] is not None else "—"
            combo = f"`{row['best_combo']}`" if row["best_combo"] else "—"
            entry = row["entry_mode"] or "—"
            md_lines.append(
                f"| {row['strategy']} | {row['regime']} | {row['base_calmar']:.2f} | "
                f"{opt_cal} | {opt_ret} | {dd} | {combo} | {entry} |"
            )
    else:
        md_lines.append("| _none_ | | | | | | | |")

    md_lines.extend([
        "",
        "## Working Rules",
        "",
        "- Start every session by reading the files above before changing code.",
        "- After each pipeline run, compare the newest `_latest.md` report with the previous file in `reports/history/`.",
        "- Use `reports/run_log.md` as the human narrative and `reports/agent_state.json` as the machine-readable checkpoint.",
    ])
    (reports_dir / "agent_context.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")


def _write_run_log(
    market: str,
    command: str,
    summary_lines: list[str],
    next_step: str | None = None,
) -> None:
    """Rebuild run_log.md and companion agent snapshot from DB + latest run summary."""
    from pathlib import Path
    from datetime import datetime
    from db.models import SessionLocal, RegimeMapModel, RegimeOptimiseModel, RegimeEntryModel

    log_path  = Path("reports") / "run_log.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Current Best from DB ──────────────────────────────────────────────
    db = SessionLocal()
    try:
        pass_rows  = db.query(RegimeMapModel).filter_by(market=market, acceptable=True).all()
        opt_rows   = db.query(RegimeOptimiseModel).filter_by(market=market).all()
        entry_rows = db.query(RegimeEntryModel).filter_by(market=market).all()
    finally:
        db.close()

    opt_map   = {(r.strategy, r.regime): r for r in opt_rows}
    entry_map = {(r.strategy, r.regime): r for r in entry_rows}

    best_lines = [
        "## Current Best",
        "",
        f"_Rebuilt from DB — {timestamp}_",
        "",
        "| Strategy | Regime | Base Cal | Opt Cal | Ret%ann | DD | Combo | Entry |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(pass_rows, key=lambda x: -(x.calmar or 0)):
        opt      = opt_map.get((r.strategy, r.regime))
        ent      = entry_map.get((r.strategy, r.regime))
        base_cal = f"{r.calmar:.2f}"
        opt_cal  = f"{opt.is_calmar:.2f}"                     if opt and opt.is_calmar is not None else "—"
        opt_ret  = f"{opt.is_annual_return * 100:+.1f}%"      if opt and opt.is_annual_return is not None else "—"
        dd_str   = f"{r.max_dd:.1f}%"                         if r.max_dd is not None else "—"
        combo    = f"`{opt.best_combo}`"                       if opt and opt.best_combo else "_(not optimised)_"
        ent_str  = ent.best_entry                              if ent else "—"
        best_lines.append(
            f"| {r.strategy} | {r.regime} | {base_cal} | {opt_cal} | {opt_ret} | {dd_str} | {combo} | {ent_str} |"
        )

    # ── New history entry ─────────────────────────────────────────────────
    entry_parts = [f"### [{timestamp}]  `{command}`  ·  {market.upper()}"]
    for line in summary_lines:
        entry_parts.append(f"- {line}")
    if next_step:
        entry_parts.append(f"\n**Next →** {next_step}")
    new_entry = "\n".join(entry_parts)

    # ── Preserve existing history ─────────────────────────────────────────
    marker   = "## Session Log"
    existing = ""
    if log_path.exists():
        content = log_path.read_text(encoding="utf-8")
        if marker in content:
            existing = content[content.index(marker) + len(marker):]

    # ── Write full file ───────────────────────────────────────────────────
    header  = "\n".join(best_lines)
    history = f"{marker}\n\n---\n{new_entry}\n{existing.lstrip()}"
    full    = f"# Alpha Engine — Run Log\n\n{header}\n\n---\n\n{history}\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(full, encoding="utf-8")
    _write_agent_snapshot(
        market=market,
        command=command,
        summary_lines=summary_lines,
        next_step=next_step,
        timestamp=timestamp,
        pass_rows=pass_rows,
        opt_map=opt_map,
        entry_map=entry_map,
    )
    logger.info("run log updated: %s", log_path)


_ENTRY_VARIANTS = [
    {"_label": "market_close",      "entry_mode": "market_close"},
    {"_label": "intraday_breakout", "entry_mode": "intraday_breakout"},
]

# intraday_breakout fills same bar at prev_high+tick (not close) — tighter RR justified by better entry price
_INTRADAY_ENTRY_BASE = {"sl_atr_mult": 1.0, "tp1_atr_mult": 2.0}
_INTRADAY_STRATEGIES = {"pivot_breakout"}


def cmd_intraday_entry_test(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Compare intraday breakout entry vs EOD close on last 60 days of signals."""
    import pandas as pd
    import strategies  # noqa: F401
    from datetime import timedelta
    from core.registry import StrategyRegistry
    from validation.backtest import _precompute_indicators
    from validation.intraday_entry import compare_entries
    from db.models import SessionLocal, RegimeOptimiseModel

    market = adapter.market_id
    today  = date.today()
    start  = today - timedelta(days=90)   # 90d daily for lookback buffer; 15m only last 60d

    # Load best pivot_breakout params
    db = SessionLocal()
    try:
        opt_rows = db.query(RegimeOptimiseModel).filter_by(
            market=market, strategy="pivot_breakout"
        ).all()
    finally:
        db.close()

    if not opt_rows:
        print("  No optimised params for pivot_breakout. Run regime-optimise first.")
        return

    best_row = max(opt_rows, key=lambda r: float(r.oos_calmar or 0))
    params = {**dict(best_row.params), "entry_mode": "market_close"}
    rvol_min = float(params.get("rvol_min", 1.1))

    strategy_cls = StrategyRegistry.for_market(market).get("pivot_breakout")
    if strategy_cls is None:
        print("  pivot_breakout not found in registry.")
        return

    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    print(f"\n  Fetching {len(universe)} symbols (90d daily)...")
    daily_dfs: dict[str, pd.DataFrame] = {}
    bm_close = _fetch_benchmark(adapter, start, today)

    for idx, symbol in enumerate(universe, 1):
        print(f"  [{idx}/{len(universe)}] {symbol}", flush=True)
        df = adapter.ohlcv(symbol, start, today)
        if df.empty or len(df) < 30:
            continue
        df = _attach_benchmark(df, bm_close)
        df.attrs = {"symbol": symbol, "market": market}
        pdf = _precompute_indicators(df)
        pdf.attrs = {"symbol": symbol, "market": market}
        daily_dfs[symbol] = pdf

    # Scan last 60 days for pivot_breakout signals
    cutoff      = today - timedelta(days=60)
    fwd_buffer  = today - timedelta(days=5)   # need 5 trading days forward
    signals_info: list[dict] = []

    print(f"\n  Scanning signals (last 60d)...")
    for symbol, df in daily_dfs.items():
        bar_dates = [
            ts for ts in df.index
            if pd.Timestamp(cutoff) <= ts <= pd.Timestamp(fwd_buffer)
        ]
        for ts in bar_dates:
            idx = df.index.get_loc(ts)
            if idx < 50:
                continue
            bar_df = df.iloc[: idx + 1].copy()
            bar_df.attrs = df.attrs
            sigs = strategy_cls().scan(bar_df, params)
            for sig in sigs:
                signals_info.append({
                    "symbol":         symbol,
                    "signal_date":    ts.date(),
                    "breakout_level": sig.meta.get("prev_high", sig.entry),
                    "eod_fill":       sig.entry,
                    "rvol_min":       rvol_min,
                })

    if not signals_info:
        print("  No signals found in last 60 days. Market may be in choppy/down regime.")
        return

    print(f"  Found {len(signals_info)} signals. Fetching 15m data for each...")
    results = compare_entries(signals_info, adapter, daily_dfs)

    triggered     = [r for r in results if r["triggered"]]
    not_triggered = [r for r in results if not r["triggered"]]

    W = 80
    print(f"\n{'='*W}")
    print(f"  TH INTRADAY ENTRY ANALYSIS  (pivot_breakout, last 60 days)")
    print(f"  Regime used: {best_row.regime}  |  Params: {best_row.params}")
    print(f"{'='*W}")
    print(f"  Total signals:        {len(results)}")
    print(f"  Intraday triggered:   {len(triggered)} ({100*len(triggered)/max(len(results),1):.0f}%)")
    print(f"  Not triggered:        {len(not_triggered)}")

    if triggered:
        advantages  = [r["price_advantage"] for r in triggered if r["price_advantage"] is not None]
        fill_bars   = [r["fill_bar"] for r in triggered if r["fill_bar"] is not None]
        avg_adv     = sum(advantages)  / len(advantages)  if advantages  else 0
        avg_bar     = sum(fill_bars)   / len(fill_bars)   if fill_bars   else 0

        eod_rets   = [r["fwd_ret_eod"]   for r in triggered if r["fwd_ret_eod"]   is not None]
        intra_rets = [r["fwd_ret_intra"] for r in triggered if r["fwd_ret_intra"] is not None]

        print(f"\n  Entry price advantage (intraday vs EOD close):")
        print(f"  Avg advantage:  {avg_adv:+.2f}%")
        print(f"  Avg fill bar:   {avg_bar:.1f} / 20  (~{avg_bar*15:.0f} min into day)")
        print(f"  Positive adv:   {sum(1 for a in advantages if a > 0)}/{len(advantages)} signals")

        print(f"\n  5-day forward returns (signals that triggered intraday):")
        print(f"  {'Entry type':<24} {'N':>5} {'Win%':>7} {'Avg Ret':>9} {'Med Ret':>9}")
        print(f"  {'-'*56}")

        import statistics
        if eod_rets:
            wr  = sum(1 for x in eod_rets if x > 0) / len(eod_rets)
            med = statistics.median(eod_rets) * 100
            avg = sum(eod_rets) / len(eod_rets) * 100
            print(f"  {'market_close (EOD)':<24} {len(eod_rets):>5} {wr:>7.0%} {avg:>+9.2f}% {med:>+9.2f}%")
        if intra_rets:
            wr  = sum(1 for x in intra_rets if x > 0) / len(intra_rets)
            med = statistics.median(intra_rets) * 100
            avg = sum(intra_rets) / len(intra_rets) * 100
            print(f"  {'intraday_breakout':<24} {len(intra_rets):>5} {wr:>7.0%} {avg:>+9.2f}% {med:>+9.2f}%")

        print(f"\n  Signal detail (first 25):")
        print(f"  {'Date':<12} {'Symbol':<12} {'Level':>8} {'EOD':>8} {'Intra':>9} {'Adv%':>7} {'Bar':>4} {'pRVOL':>6}")
        print(f"  {'-'*72}")
        for r in sorted(triggered, key=lambda x: x["date"])[:25]:
            print(
                f"  {str(r['date']):<12} {r['symbol']:<12} "
                f"{r['breakout_level']:>8.2f} {r['eod_fill']:>8.2f} "
                f"{r['intraday_fill']:>9.2f} {r['price_advantage']:>+7.2f}% "
                f"{r['fill_bar']:>4} {r['proj_rvol']:>6.1f}x"
            )

    print(f"{'='*W}\n")


def cmd_intraday_fakeout_study(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """
    Fakeout study: scan ALL days where high > prev_high (not close > prev_high).
    Classify each intraday breakout attempt as: not_triggered / fakeout / genuine.
    Shows expected value of intraday entry accounting for fakeout rate and cost.
    """
    import pandas as pd
    import statistics
    import strategies  # noqa: F401
    from datetime import timedelta
    from db.models import SessionLocal, RegimeOptimiseModel
    from validation.backtest import _precompute_indicators
    from validation.intraday_entry import run_fakeout_study

    market = adapter.market_id
    today  = date.today()
    start  = today - timedelta(days=90)

    db = SessionLocal()
    try:
        opt_rows = db.query(RegimeOptimiseModel).filter_by(
            market=market, strategy="pivot_breakout"
        ).all()
    finally:
        db.close()

    if not opt_rows:
        print("  No optimised params for pivot_breakout. Run regime-optimise first.")
        return

    best_row = max(opt_rows, key=lambda r: float(r.oos_calmar or 0))
    params   = dict(best_row.params)
    rvol_min = float(params.get("rvol_min", 1.1))
    lookback = int(params.get("lookback", 5))
    psth     = float(params.get("psth", 0.005))

    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    print(f"\n  Fetching {len(universe)} symbols (90d daily)...")
    bm_close = _fetch_benchmark(adapter, start, today)
    daily_dfs: dict[str, pd.DataFrame] = {}

    for idx, symbol in enumerate(universe, 1):
        print(f"  [{idx}/{len(universe)}] {symbol}", flush=True)
        df = adapter.ohlcv(symbol, start, today)
        if df.empty or len(df) < 30:
            continue
        df = _attach_benchmark(df, bm_close)
        df.attrs = {"symbol": symbol, "market": market}
        pdf = _precompute_indicators(df)
        pdf.attrs = {"symbol": symbol, "market": market}
        daily_dfs[symbol] = pdf

    # Build candidates: all days where high > prev_high (with uptrend + psth filter)
    cutoff     = today - timedelta(days=60)
    fwd_buffer = today - timedelta(days=5)
    candidates: list[dict] = []

    print(f"\n  Scanning all intraday breakout attempts (last 60d, high > prev_high)...")
    for symbol, df in daily_dfs.items():
        bar_dates = [
            ts for ts in df.index
            if pd.Timestamp(cutoff) <= ts <= pd.Timestamp(fwd_buffer)
        ]
        for ts in bar_dates:
            idx = df.index.get_loc(ts)
            if idx < lookback + 50:
                continue
            bar = df.iloc[idx]

            # prev_high: max close of lookback window (same as strategy)
            prev_high = float(df["close"].iloc[max(0, idx - lookback):idx].max())
            if prev_high <= 0:
                continue

            # Must have crossed prev_high intraday
            if float(bar["high"]) <= prev_high:
                continue

            # Minimum breakout extent (using high)
            if (float(bar["high"]) - prev_high) / prev_high < psth:
                continue

            # Uptrend filter: close > SMA50
            if "_sma50" in df.columns:
                sma50 = df["_sma50"].iloc[idx]
                if pd.notna(sma50) and float(bar["close"]) < float(sma50):
                    continue

            candidates.append({
                "symbol":      symbol,
                "date":        ts.date(),
                "prev_high":   prev_high,
                "daily_close": float(bar["close"]),
                "rvol_min":    rvol_min,
            })

    if not candidates:
        print("  No intraday breakout attempts found in last 60 days.")
        return

    print(f"  Found {len(candidates)} breakout attempts. Fetching 15m data...")
    results = run_fakeout_study(candidates, adapter, daily_dfs)

    genuine      = [r for r in results if r["outcome"] == "genuine"]
    fakeout      = [r for r in results if r["outcome"] == "fakeout"]
    not_trig     = [r for r in results if r["outcome"] == "not_triggered"]
    triggered    = genuine + fakeout

    n = len(results)
    W = 80
    print(f"\n{'='*W}")
    print(f"  TH INTRADAY FAKEOUT STUDY — pivot_breakout, last 60 days")
    print(f"  Uptrend filter: SMA50  |  RVOL gate: {rvol_min}x projected  |  Lookback: {lookback}")
    print(f"{'='*W}")
    print(f"  Total breakout attempts (high > prev_high):  {n}")
    print(f"  Not triggered (RVOL gate failed):            {len(not_trig)} ({100*len(not_trig)/max(n,1):.0f}%)")
    print(f"  Triggered:                                   {len(triggered)} ({100*len(triggered)/max(n,1):.0f}%)")
    print(f"    ├─ Genuine  (close ≥ prev_high):           {len(genuine)} ({100*len(genuine)/max(len(triggered),1):.0f}% of triggered)")
    print(f"    └─ Fakeout  (close < prev_high):           {len(fakeout)} ({100*len(fakeout)/max(len(triggered),1):.0f}% of triggered)")

    if triggered:
        # Per-outcome P&L
        genuine_rets = [r["pnl_pct"] for r in genuine  if r["pnl_pct"] is not None]
        fakeout_rets = [r["pnl_pct"] for r in fakeout  if r["pnl_pct"] is not None]

        print(f"\n  P&L per outcome (5-day fwd for genuine, same-day close for fakeout):")
        print(f"  {'Outcome':<14} {'N':>5} {'Win%':>7} {'Avg':>8} {'Median':>8} {'Best':>8} {'Worst':>8}")
        print(f"  {'-'*60}")

        def _row(label, rets):
            if not rets:
                return f"  {label:<14} {'0':>5}"
            wr  = sum(1 for x in rets if x > 0) / len(rets)
            avg = statistics.mean(rets) * 100
            med = statistics.median(rets) * 100
            best  = max(rets) * 100
            worst = min(rets) * 100
            return f"  {label:<14} {len(rets):>5} {wr:>7.0%} {avg:>+8.2f}% {med:>+8.2f}% {best:>+8.2f}% {worst:>+8.2f}%"

        print(_row("genuine",  genuine_rets))
        print(_row("fakeout",  fakeout_rets))

        # Expected value per triggered attempt
        g_rate = len(genuine) / len(triggered)
        f_rate = len(fakeout) / len(triggered)
        g_avg  = statistics.mean(genuine_rets) if genuine_rets else 0
        f_avg  = statistics.mean(fakeout_rets) if fakeout_rets else 0
        ev     = g_rate * g_avg + f_rate * f_avg

        print(f"\n  Expected value per triggered attempt:")
        print(f"    {g_rate:.0%} genuine × {g_avg*100:+.2f}%  +  {f_rate:.0%} fakeout × {f_avg*100:+.2f}%  =  EV {ev*100:+.2f}%")

        # Fill bar distribution
        fill_bars = [r["fill_bar"] for r in triggered if r["fill_bar"]]
        if fill_bars:
            avg_bar = statistics.mean(fill_bars)
            early   = sum(1 for b in fill_bars if b <= 5)
            print(f"\n  Fill timing:  avg bar {avg_bar:.1f}/20 (~{avg_bar*15:.0f}min)  |  early (≤bar5): {early}/{len(fill_bars)} ({100*early/len(fill_bars):.0f}%)")

        # Fakeout detail
        if fakeout:
            print(f"\n  Fakeout detail (first 20):")
            print(f"  {'Date':<12} {'Symbol':<12} {'Level':>8} {'Fill':>8} {'DayClose':>9} {'Loss%':>7} {'Bar':>4}")
            print(f"  {'-'*64}")
            for r in sorted(fakeout, key=lambda x: x["date"])[:20]:
                loss = r["pnl_pct"] * 100 if r["pnl_pct"] else 0
                print(
                    f"  {str(r['date']):<12} {r['symbol']:<12} "
                    f"{r['prev_high']:>8.2f} {r['fill_price']:>8.2f} "
                    f"{r['daily_close']:>9.2f} {loss:>+7.2f}% {r['fill_bar']:>4}"
                )

    print(f"{'='*W}\n")


def _learn_representative_score(pos: dict) -> float:
    """Mirror of learningRealRepresentativeScore in index.html."""
    import re as _re
    bars_held = pos.get("bars_held") or 0
    exits = pos.get("exits", [])
    last_exit = exits[-1] if exits else {}
    final_reason = str(last_exit.get("exit_reason") or "").lower()
    has_runner = any(_re.search(r"ema|trail|tp2", str(e.get("exit_reason") or "").lower()) for e in exits)
    entry_price = pos.get("entry_price") or 0
    score = 100.0 if pos.get("pnl", 0) > 0 else -40.0
    score += 10 if pos.get("stop_price") is not None else 0
    score += 10 if pos.get("tp1_price") is not None else 0
    score += 50 if has_runner else 0
    if _re.search(r"ema|trail", final_reason):
        score += 70
    elif final_reason == "tp2":
        score += 35
    elif final_reason == "tp1":
        score -= 30
    score -= abs(bars_held - 22) * 0.9
    score -= 20 if 0 < entry_price < 0.1 else 0
    score -= 10 if entry_price > 300 else 0
    return score


def _write_learn_ohlcv(
    strategies_out: list,
    ohlcv_cache: dict,
    docs_dir: str,
    market_key: str,
    today,
) -> str:
    """Slice ohlcv_cache to learning-section symbols only and write ohlcv_<MKT>_learn.json.

    Returns the filename (relative to docs_dir).
    """
    # Candidates mirror learningRealCandidates() in index.html
    _LEARN_CANDIDATES = [
        {"id": "pivot_breakout",     "regimes": ["uptrend", "downtrend"]},
        {"id": "pullback_buy",       "regimes": ["uptrend", "downtrend"]},
        {"id": "ma_cross",           "regimes": ["uptrend", "downtrend"]},
        {"id": "reversal",           "regimes": ["choppy", "downtrend"]},
        {"id": "bb_squeeze",         "regimes": ["choppy", "uptrend", "downtrend"]},
    ]
    strat_map = {(s.get("id", ""), s.get("regime", "")): s for s in strategies_out}

    needed: dict = {}
    for spec in _LEARN_CANDIDATES:
        # For each candidate pick the best (regime, position) pair — same as collectLearningLessons
        best_score = None
        best_pos = None
        for reg in spec["regimes"]:
            strat = strat_map.get((spec["id"], reg))
            if not strat:
                continue
            pos_map: dict = {}
            for sym_row in strat.get("symbols", []):
                sym = sym_row.get("symbol", "")
                for t in sym_row.get("trades", []):
                    pid = t.get("position_id") or f"{t.get('entry_date','')}_{t.get('entry_price','')}_{sym}"
                    if pid not in pos_map:
                        pos_map[pid] = {
                            "symbol": sym,
                            "pnl": 0.0,
                            "entry_date": t.get("entry_date", ""),
                            "exit_date": t.get("exit_date", ""),
                            "entry_price": t.get("entry_price"),
                            "stop_price": t.get("sl_price"),
                            "tp1_price": t.get("tp1_price"),
                            "bars_held": 0,
                            "exits": [],
                        }
                    row = pos_map[pid]
                    row["pnl"] += float(t.get("pnl", 0))
                    if t.get("exit_date", "") > row["exit_date"]:
                        row["exit_date"] = t.get("exit_date", "")
                    if row["stop_price"] is None and t.get("sl_price") is not None:
                        row["stop_price"] = t.get("sl_price")
                    if row["tp1_price"] is None and t.get("tp1_price") is not None:
                        row["tp1_price"] = t.get("tp1_price")
                    row["bars_held"] = max(row["bars_held"], int(t.get("bars_held") or 0))
                    row["exits"].append({"exit_date": t.get("exit_date", ""), "exit_reason": t.get("exit_reason", "")})
            if not pos_map:
                continue
            for row in pos_map.values():
                row["exits"].sort(key=lambda e: e["exit_date"])
            regime_best = max(pos_map.values(), key=_learn_representative_score)
            sc = _learn_representative_score(regime_best)
            if best_score is None or sc > best_score:
                best_score = sc
                best_pos = regime_best
        if not best_pos:
            continue
        sym = best_pos["symbol"]
        entry_date = best_pos["entry_date"]
        exit_date = best_pos["exit_date"]
        if sym not in needed:
            needed[sym] = (entry_date, exit_date)
        else:
            cur_entry, cur_exit = needed[sym]
            needed[sym] = (min(cur_entry, entry_date), max(cur_exit, exit_date))

    result: dict = {}
    for sym, (entry_date, exit_date) in needed.items():
        rows = ohlcv_cache.get(sym, [])
        dates = sorted(b["time"] for b in rows)
        entry_idx = next((i for i, d in enumerate(dates) if d >= entry_date), len(dates))
        start_idx = max(0, entry_idx - 120)
        start_date = dates[start_idx] if dates else entry_date
        exit_idx = next((i for i, d in enumerate(dates) if d > exit_date), len(dates))
        end_idx = min(len(dates) - 1, exit_idx + 20)
        end_date = dates[end_idx] if dates else exit_date
        result[sym] = [b for b in rows if start_date <= b["time"] <= end_date]

    learn_filename = f"ohlcv_{market_key}_learn.json"
    learn_path = os.path.join(docs_dir, learn_filename)
    with open(learn_path, "w", encoding="utf-8") as fh:
        json.dump({"generated": str(today), "market": market_key, "ohlcv": result}, fh, default=str, separators=(",", ":"))
    logger.info("chart-export: wrote %s (%d symbols)", learn_filename, len(result))
    return learn_filename


def cmd_chart_export(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Export per-symbol candlestick + trade data for optimised strategies → docs/chart_data.json."""
    import strategies  # noqa: F401
    import pandas as pd
    from core.registry import StrategyRegistry
    from validation.backtest import run_portfolio_backtest, _precompute_indicators
    from db.models import SessionLocal, RegimeOptimiseModel, RegimeLabelModel, RegimeMapModel

    market   = adapter.market_id
    today    = date.today()
    y5_start = today - timedelta(days=1825)
    y2_start = today - timedelta(days=730)

    db = SessionLocal()
    try:
        label_rows = db.query(RegimeLabelModel).filter_by(market=market).all()
        opt_rows   = db.query(RegimeOptimiseModel).filter_by(market=market).all()
        map_rows   = db.query(RegimeMapModel).filter_by(market=market).all()
    finally:
        db.close()

    if not label_rows:
        print("  No regime labels. Run 'regime' first.")
        return
    if not opt_rows:
        print("  No optimised params. Run 'regime-optimise' first.")
        return

    regime_date_map: dict = {row.date: row.regime for row in label_rows}

    # Build regime overview map (all strategy×regime pairs from base run)
    regime_map: dict = {}
    for row in map_rows:
        key = f"{row.strategy}|{row.regime}"
        regime_map[key] = {
            "calmar":      round(float(row.calmar or 0), 2),
            "wr":          round(float(row.wr or 0), 1),
            "max_dd":      round(float(row.max_dd or 0), 1),
            "trade_count": row.trade_count or 0,
            "yearly":      row.yearly or {},
            "pass":        (row.calmar or 0) >= _EDGE_CALMAR,
        }

    # Current regime = regime label for the most recent bar date
    latest_label = max(label_rows, key=lambda r: r.date)
    current_regime = latest_label.regime

    bm_close = _fetch_benchmark(adapter, y5_start, today)

    universe = adapter.universe(today, top_n=getattr(args, "symbols", None))
    print(f"\n  Fetching {len(universe)} symbols (5yr, bulk) ...", flush=True)
    bulk = adapter.ohlcv_bulk(universe, y5_start, today)

    all_dfs: list = []
    symbol_df_map: dict = {}
    for idx, symbol in enumerate(universe, 1):
        print(f"  [{idx}/{len(universe)}] {symbol}", flush=True)
        df = bulk.get(symbol, pd.DataFrame())
        if bm_close is not None:
            df = _attach_benchmark(df, bm_close)
        if df.empty or len(df) < 60:
            continue
        df.attrs = {"symbol": symbol, "market": market}
        pdf = _precompute_indicators(df)
        pdf.attrs = {"symbol": symbol, "market": market}
        all_dfs.append(pdf)
        symbol_df_map[symbol] = pdf

    if not all_dfs:
        print("  No data.")
        return

    strategies_map = StrategyRegistry.for_market(market)

    # Build date→ATR lookup per symbol (for SL/TP price computation)
    symbol_atr: dict = {}
    for sym, df in symbol_df_map.items():
        if "_atr" in df.columns:
            symbol_atr[sym] = {
                (ts.date() if hasattr(ts, "date") else ts): float(v)
                for ts, v in df["_atr"].items()
                if v == v  # skip NaN
            }

    valid_opt_rows = [(opt, strategies_map[opt.strategy]) for opt in opt_rows if opt.strategy in strategies_map]
    print(f"\n  Running {len(valid_opt_rows)} optimised backtests (parallel) ...", flush=True)
    trades_list = Parallel(n_jobs=min(len(valid_opt_rows), 16), backend="loky")(
        delayed(_run_backtest_par)(cls, opt.params or {}, all_dfs, args.capital)
        for opt, cls in valid_opt_rows
    ) if valid_opt_rows else []

    strategies_out: list = []

    for (opt_row, _), raw_trades in zip(valid_opt_rows, trades_list):
        s_id   = opt_row.strategy
        regime = opt_row.regime
        params = opt_row.params or {}
        print(f"  {s_id}|{regime} [{opt_row.best_combo}]", flush=True)

        # All regime trades — full 5yr window
        trades = [
            t for t in raw_trades
            if regime_date_map.get(t["entry_date"], "choppy") == regime
        ]

        # Group by symbol
        by_symbol: dict = {}
        for t in trades:
            sym = t["symbol"]
            by_symbol.setdefault(sym, []).append(t)

        sl_mult  = float(params.get("sl_atr_mult",  1.5))
        tp1_mult = float(params.get("tp1_atr_mult", 3.0))
        tp2_mult = float(params.get("tp2_atr_mult", 999.0))
        ema_p    = int(params.get("ema_exit_period", 0))

        symbols_out: list = []
        for sym, sym_trades in sorted(by_symbol.items(),
                                       key=lambda x: sum(t["pnl"] for t in x[1]),
                                       reverse=True):
            total_pnl  = sum(t["pnl"] for t in sym_trades)
            # Group partial exits by position_id — 1 entry = 1 trade regardless of partial count
            from collections import defaultdict as _dd
            _by_pid: dict = _dd(float)
            for _t in sym_trades:
                _by_pid[_t.get("position_id", id(_t))] += float(_t["pnl"])
            wins = sum(1 for v in _by_pid.values() if v > 0)
            trade_count_pos = len(_by_pid)  # positions, not exit events
            atr_map    = symbol_atr.get(sym, {})
            trade_list = []
            for t in sorted(sym_trades, key=lambda x: x["entry_date"]):
                ed  = t["entry_date"]
                xd  = t["exit_date"]
                ep  = float(t["entry_price"])
                lng = t.get("direction", "long") == "long"
                atr = atr_map.get(ed, 0.0)
                sl_p  = round(ep - sl_mult  * atr if lng else ep + sl_mult  * atr, 2)
                tp1_p = round(ep + tp1_mult * atr if lng else ep - tp1_mult * atr, 2)
                tp2_p = round(ep + tp2_mult * atr if lng else ep - tp2_mult * atr, 2) if tp2_mult < 900 else None
                trade_list.append({
                    "entry_date":   ed.isoformat() if hasattr(ed, "isoformat") else str(ed),
                    "exit_date":    xd.isoformat() if hasattr(xd, "isoformat") else str(xd),
                    "entry_price":  round(ep, 2),
                    "exit_price":   round(float(t["exit_price"]), 2),
                    "sl_price":     sl_p,
                    "tp1_price":    tp1_p,
                    "tp2_price":    tp2_p,
                    "direction":    t.get("direction", "long"),
                    "pnl":          round(float(t["pnl"]), 2),
                    "exit_reason":  t.get("exit_reason", ""),
                    "bars_held":    t.get("bars_held", 0),
                    "position_id":  str(t.get("position_id", "")),
                })
            symbols_out.append({
                "symbol":      sym,
                "total_pnl":   round(total_pnl, 2),
                "trade_count": trade_count_pos,
                "win_count":   wins,
                "trades":      trade_list,
            })
        # indicator params so the chart knows what to render
        indicator_params: dict = {"ema_exit": ema_p}
        if s_id == "ma_cross":
            indicator_params["ma_fast"] = int(params.get("fast_period", 10))
            indicator_params["ma_slow"] = int(params.get("slow_period", 20))
        elif s_id == "bb_squeeze":
            indicator_params["bb_period"] = 20
            indicator_params["bb_mult"]   = 2.0
            indicator_params["kc_mult"]   = float(params.get("kc_mult", 2.0))
        elif s_id == "reversal":
            indicator_params["rsi_period"]    = 14
            indicator_params["rsi_threshold"] = int(params.get("rsi_threshold", 40))

        total_wins_all   = sum(s["win_count"] for s in symbols_out)
        total_trades_all = sum(s["trade_count"] for s in symbols_out)
        win_rate_out = round(total_wins_all / total_trades_all * 100, 1) if total_trades_all else 0.0
        max_dd_out   = round(float(regime_map.get(f"{s_id}|{regime}", {}).get("max_dd", 0.0)), 1)

        # Export key risk/signal params so the chart UI can display accurate explanations
        exported_params = {
            "sl_atr_mult":      float(params.get("sl_atr_mult",  1.5)),
            "tp1_atr_mult":     float(params.get("tp1_atr_mult", 3.0)),
            "tp2_atr_mult":     float(params.get("tp2_atr_mult", 999.0)) if float(params.get("tp2_atr_mult", 999.0)) < 900 else None,
            "tp1_partial_pct":  float(params.get("tp1_partial_pct", 1.0)),
            "tp2_partial_pct":  float(params.get("tp2_partial_pct", 0.0)) if float(params.get("tp2_atr_mult", 999.0)) < 900 else None,
            "ema_exit_period":  int(params.get("ema_exit_period", 0)) or None,
            "hard_stop_mode":   params.get("hard_stop_mode", ""),
            "rvol_min":         float(params.get("rvol_min", 0.0)) or None,
            "rsm_min":          float(params.get("rsm_min",  0.0)) or None,
            "swing_period":     int(params.get("swing_period", 0)) or None,
            "rsi_threshold":    int(params.get("rsi_threshold", 0)) or None,
            "fast_period":      int(params.get("fast_period",  0)) or None,
            "slow_period":      int(params.get("slow_period",  0)) or None,
            "kc_mult":          float(params.get("kc_mult", 2.0)),
        }

        strategies_out.append({
            "id":               s_id,
            "regime":           regime,
            "combo":            opt_row.best_combo or "",
            "calmar":           round(float(opt_row.is_calmar or 0), 2),
            "ret_pct":          round(float(opt_row.is_annual_return or 0) * 100, 1),
            "win_rate":         win_rate_out,
            "max_dd":           max_dd_out,
            "params":           exported_params,
            "indicator_params": indicator_params,
            "symbols":          symbols_out,
        })

    total_trades = sum(len(s["trades"]) for st in strategies_out for s in st["symbols"])
    docs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
    os.makedirs(docs_dir, exist_ok=True)
    out_path = os.path.join(docs_dir, "chart_data.json")
    existing_payload = None
    if os.path.exists(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as fh:
                existing_payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("chart-export could not read existing payload %s: %s", out_path, exc)

    market_key = market.upper()

    from collections import defaultdict
    _ryc: dict = defaultdict(lambda: defaultdict(int))
    for _d, _r in regime_date_map.items():
        _ryc[str(_d)[:4]][_r] += 1
    regime_year_weights = {
        yr: {r: round(c / sum(counts.values()), 4) for r, c in counts.items()}
        for yr, counts in _ryc.items()
    }
    regime_periods = _flatten_regime_periods(label_rows)

    compact_output = {
        "generated":      str(today),
        "market":         market_key,
        "current_regime": current_regime,
        "regime_map":     regime_map,
        "regime_periods": regime_periods,
        "regime_year_weights": regime_year_weights,
        "strategies":     strategies_out,
    }

    merged_output = _merge_chart_export_payload(existing_payload, compact_output)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(merged_output, fh, default=str, separators=(",", ":"))
    print(f"\n  {len(strategies_out)} strategies · {total_trades} trades → {out_path}")
    _export_chart_export_markdown(
        market=market,
        as_of=today,
        current_regime=current_regime,
        strategies_out=strategies_out,
        total_symbols=len(symbol_df_map),
        total_trades=total_trades,
        output_path=out_path,
    )
    _write_run_log(
        market,
        "chart-export",
        [
            f"exported {len(strategies_out)} strategy-regime pairs to `docs/chart_data.json`",
            f"symbols={len(symbol_df_map)} trades={total_trades} current_regime={current_regime}",
        ],
        next_step="serve viewer and inspect charts for rule fidelity",
    )
    print("  Serve: python -m http.server 8080 --directory docs")


def _export_chart_export_markdown(
    market: str,
    as_of,
    current_regime: str,
    strategies_out: list[dict],
    total_symbols: int,
    total_trades: int,
    output_path: str,
) -> None:
    from pathlib import Path
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    latest_path = Path("reports") / f"{market}_chart_export_latest.md"
    history_path = Path("reports") / "history" / f"{timestamp}_{market}_chart_export.md"

    lines = [
        f"# {market.upper()} Chart Export",
        "",
        f"- As of: `{as_of}`",
        f"- Current regime: `{current_regime}`",
        f"- Strategy-regime pairs exported: `{len(strategies_out)}`",
        f"- Symbols exported: `{total_symbols}`",
        f"- Trades exported: `{total_trades}`",
        f"- Output: `{output_path}`",
        "",
        "## Exported Strategies",
        "",
        "| Strategy | Regime | Combo | Calmar | Ret% | DD | Symbols | Trades |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for row in sorted(strategies_out, key=lambda x: -(x.get("calmar") or 0)):
        symbol_count = len(row.get("symbols") or [])
        trade_count = sum(s.get("trade_count", 0) for s in row.get("symbols") or [])
        lines.append(
            f"| {row['id']} | {row['regime']} | `{row.get('combo') or ''}` | "
            f"{row.get('calmar', 0):.2f} | {row.get('ret_pct', 0):+.1f}% | "
            f"{row.get('max_dd', 0):.1f}% | {symbol_count} | {trade_count} |"
        )

    lines.extend([
        "",
        "## AI Suggestions",
        "",
        "- Compare the viewer against the latest optimise winners and confirm exits match the chosen combo labels.",
        "- Inspect top-PnL and worst-PnL symbols first; chart fidelity problems usually show up there.",
        "- If a chart looks wrong, trace the pair back to `reports/run_log.md`, then `reports/history/` for the prior optimise run.",
        "",
    ])

    content = "\n".join(lines) + "\n"
    for path in (latest_path, history_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


_PAPER_CAPITAL         = 100_000.0
_PAPER_MAX_OPEN        = 10
_PAPER_MAX_PER_STRATEGY = 4
_PAPER_RISK_PCT        = 0.01


def cmd_paper_update(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Daily paper portfolio update: process exits then open new positions."""
    from core.exit_policy import HardExitPolicy
    from core.indicators import ema as _ema
    from core.signal import Signal, Position
    from db.models import (
        ActiveStrategyModel, ScanSignalModel,
        PaperPositionModel, PaperTradeModel, SessionLocal,
    )

    market = adapter.market_id
    today  = date.today()
    start  = today - timedelta(days=60)

    db = SessionLocal()
    try:
        open_positions = (
            db.query(PaperPositionModel)
            .filter_by(market=market, status="open")
            .all()
        )
        new_signals = (
            db.query(ScanSignalModel)
            .filter_by(market=market, scan_date=today, status="new")
            .all()
        )
    finally:
        db.close()

    symbols_needed = {p.symbol for p in open_positions} | {s.symbol for s in new_signals}
    if not symbols_needed:
        print("  Nothing to do — no open positions and no new signals.")
        return

    print(f"  Fetching {len(symbols_needed)} symbols...", flush=True)
    bulk = adapter.ohlcv_bulk(list(symbols_needed), start, today)

    bars: dict[str, dict] = {}
    for symbol, df in bulk.items():
        if df.empty or len(df) < 15:
            continue
        ema10 = _ema(df, 10)
        last  = df.iloc[-1]
        bars[symbol] = {
            "close": float(last["close"]),
            "high":  float(last["high"]),
            "low":   float(last["low"]),
            "open":  float(last["open"]),
            "ema10": float(ema10.iloc[-1]),
        }

    exit_policy    = HardExitPolicy()
    exits_count    = 0
    opened_count   = 0

    # ── 1. Process exits ──────────────────────────────────────────────────────
    db = SessionLocal()
    try:
        for pos_rec in open_positions:
            bar = bars.get(pos_rec.symbol)
            if bar is None:
                continue

            ep  = pos_rec.exit_params or {}
            atr = ep.get("atr_at_entry") or max(pos_rec.entry_price - pos_rec.sl_price, 1e-6) / 1.5
            tp2_entry = pos_rec.tp2_price if pos_rec.tp2_price else pos_rec.entry_price * 9999

            sig = Signal(
                symbol=pos_rec.symbol, market=market, strategy=pos_rec.strategy,
                direction="long", entry=pos_rec.entry_price, entry_type="market_close",
                sl=pos_rec.sl_price, tp1=pos_rec.tp1_price, tp2=tp2_entry, tp3=None,
                atr=atr, rr=2.0, score=100.0,
                sl_atr_mult=ep.get("sl_atr_mult", 1.5),
                tp1_atr_mult=ep.get("tp1_atr_mult", 3.0),
                tp2_atr_mult=ep.get("tp2_atr_mult", 999.0),
                trail_atr_mult=ep.get("trail_atr_mult", 999.0),
                be_trigger_atr_mult=ep.get("be_trigger_atr_mult", 999.0),
                tp1_partial_pct=ep.get("tp1_partial_pct", 1.0),
                tp2_partial_pct=ep.get("tp2_partial_pct", 0.0),
                ema_exit_period=ep.get("ema_exit_period", 0),
                hard_stop_mode=ep.get("hard_stop_mode", "trail"),
                risk_pct=_PAPER_RISK_PCT,
                max_bars=ep.get("max_bars", 0),
            )
            position = Position(
                signal=sig, entry_price=pos_rec.entry_price,
                entry_date=pos_rec.entry_date, size=int(pos_rec.remaining_shares),
            )
            position.bars_held     = pos_rec.bars_held or 0
            position.sl_current    = pos_rec.sl_current or pos_rec.sl_price
            position.highest_close = pos_rec.highest_close or pos_rec.entry_price
            position.tp1_hit       = bool(pos_rec.tp1_hit)
            position.tp2_hit       = bool(pos_rec.tp2_hit)

            exit_sig = exit_policy.check(position, bar, ep)

            # Persist mutated state (trail/BE moves)
            pos_rec.sl_current     = position.sl_current
            pos_rec.highest_close  = position.highest_close
            pos_rec.tp1_hit        = position.tp1_hit
            pos_rec.tp2_hit        = position.tp2_hit
            pos_rec.bars_held      = (pos_rec.bars_held or 0) + 1

            if exit_sig:
                shares_out = (
                    pos_rec.remaining_shares * exit_sig.partial_pct
                    if exit_sig.partial else pos_rec.remaining_shares
                )
                pnl = (exit_sig.price - pos_rec.entry_price) * shares_out
                db.add(PaperTradeModel(
                    position_id=pos_rec.id,
                    exit_date=today,
                    exit_price=exit_sig.price,
                    exit_reason=exit_sig.reason,
                    shares=shares_out,
                    pnl=pnl,
                ))
                pos_rec.pnl              = (pos_rec.pnl or 0.0) + pnl
                pos_rec.remaining_shares -= shares_out
                if pos_rec.remaining_shares <= 0 or not exit_sig.partial:
                    pos_rec.status    = "closed"
                    pos_rec.exit_date = today
                    pos_rec.remaining_shares = 0.0
                exits_count += 1
                print(f"  EXIT  {pos_rec.symbol:<12} {exit_sig.reason:<14} pnl={pnl:>+.2f}")

        db.commit()

        # ── 2. Equity (realized) ─────────────────────────────────────────────
        total_pnl = sum(t.pnl or 0.0 for t in db.query(PaperTradeModel).all())
        equity    = _PAPER_CAPITAL + total_pnl

        # ── 3. Open new positions ────────────────────────────────────────────
        open_count = db.query(PaperPositionModel).filter_by(market=market, status="open").count()
        strat_counts: dict[str, int] = {}
        for p in db.query(PaperPositionModel).filter_by(market=market, status="open").all():
            strat_counts[p.strategy] = strat_counts.get(p.strategy, 0) + 1

        for sig_rec in new_signals:
            if open_count >= _PAPER_MAX_OPEN:
                break
            if strat_counts.get(sig_rec.strategy, 0) >= _PAPER_MAX_PER_STRATEGY:
                continue
            bar = bars.get(sig_rec.symbol)
            if bar is None:
                continue
            sl_dist = sig_rec.entry_price - sig_rec.sl_price
            if sl_dist <= 0:
                continue
            shares = (equity * _PAPER_RISK_PCT) / sl_dist
            if shares < 0.01:
                continue

            active = (
                db.query(ActiveStrategyModel)
                .filter_by(id=sig_rec.source_active_id)
                .first()
            )
            ep = dict(active.params) if active else {}
            ep["atr_at_entry"] = sig_rec.atr_at_entry or sl_dist / 1.5

            db.add(PaperPositionModel(
                market=market, symbol=sig_rec.symbol,
                strategy=sig_rec.strategy, regime=sig_rec.regime_at_signal,
                entry_date=today, entry_price=sig_rec.entry_price,
                sl_price=sig_rec.sl_price, sl_current=sig_rec.sl_price,
                tp1_price=sig_rec.tp1_price, tp2_price=sig_rec.tp2_price,
                shares=shares, remaining_shares=shares,
                highest_close=sig_rec.entry_price,
                tp1_hit=False, tp2_hit=False, bars_held=0,
                exit_params=ep,
                source_signal_id=sig_rec.id,
                status="open", pnl=0.0,
            ))
            sig_rec.status = "opened"
            open_count += 1
            strat_counts[sig_rec.strategy] = strat_counts.get(sig_rec.strategy, 0) + 1
            opened_count += 1
            print(f"  OPEN  {sig_rec.symbol:<12} {sig_rec.strategy:<22} entry={sig_rec.entry_price:.4f}  shares={shares:.2f}")

        db.commit()
    finally:
        db.close()

    print(f"\n  Equity: {equity:,.2f}  |  Opened: {opened_count}  |  Exits: {exits_count}")


def cmd_scan(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Daily scan: generate signals from active roster for current regime."""
    import strategies  # noqa: F401
    from core.registry import StrategyRegistry
    from core.regime import label_regime
    from validation.backtest import _precompute_indicators
    from db.models import ActiveStrategyModel, ScanSignalModel, SessionLocal

    market = adapter.market_id
    today  = date.today()
    start  = today - timedelta(days=365)  # 250+ bars for SMA200 warmup

    # 1. Current regime from latest benchmark bar
    bm_close = _fetch_benchmark(adapter, start, today)
    if bm_close is None:
        print("  ERROR: no benchmark data — cannot determine regime.")
        return
    regime_series  = label_regime(bm_close["_bm_close"])
    current_regime = str(regime_series.iloc[-1])
    print(f"\n  Scan date: {today}  |  Regime: {current_regime.upper()}")

    # 2. Active roster — only pairs matching current regime
    db = SessionLocal()
    try:
        active_rows = (
            db.query(ActiveStrategyModel)
            .filter_by(market=market, status="active")
            .all()
        )
    finally:
        db.close()

    matching = [r for r in active_rows if r.regime == current_regime]
    if not matching:
        print(f"  No active strategies for regime={current_regime}.")
        return
    print(f"  Active pairs in scope: {len(matching)}")

    # 3. Fetch OHLCV + precompute indicators
    universe = adapter.universe(today)
    print(f"  Fetching {len(universe)} symbols...", flush=True)
    bulk = adapter.ohlcv_bulk(universe, start, today)

    all_dfs: dict[str, "pd.DataFrame"] = {}
    for symbol in universe:
        df = bulk.get(symbol, pd.DataFrame())
        if df.empty or len(df) < 60:
            continue
        df = _attach_benchmark(df, bm_close)
        df.attrs = {"symbol": symbol, "market": market}
        df = _precompute_indicators(df)
        df.attrs = {"symbol": symbol, "market": market}
        all_dfs[symbol] = df

    if not all_dfs:
        print("  No symbol data loaded.")
        return
    print(f"  {len(all_dfs)}/{len(universe)} symbols ready.")

    # 4. Run scan for each active pair
    strategies_map = StrategyRegistry.for_market(market)
    new_signals: list[dict] = []

    for active in matching:
        strategy_cls = strategies_map.get(active.strategy)
        if strategy_cls is None:
            logger.warning("Strategy %s not in registry — skipping.", active.strategy)
            continue

        strategy_obj = strategy_cls()
        params = {**strategy_obj.default_params, **active.params}
        tp2_off = params.get("tp2_atr_mult", 999.0) >= 900

        for symbol, df in all_dfs.items():
            try:
                sigs = strategy_obj.scan(df, params)
            except Exception as exc:
                logger.debug("scan error %s/%s: %s", active.strategy, symbol, exc)
                continue
            for sig in sigs:
                rvol_val = float(df["_rvol"].iloc[-1]) if "_rvol" in df.columns else None
                new_signals.append({
                    "market":            market,
                    "scan_date":         today,
                    "symbol":            symbol,
                    "strategy":          active.strategy,
                    "regime_at_signal":  current_regime,
                    "direction":         sig.direction,
                    "entry_price":       sig.entry,
                    "sl_price":          sig.sl,
                    "tp1_price":         sig.tp1,
                    "tp2_price":         None if tp2_off else sig.tp2,
                    "atr_at_entry":      sig.atr,
                    "rvol_at_entry":     rvol_val,
                    "source_active_id":  active.id,
                    "status":            "new",
                })

    # 5. Upsert (idempotent on market+scan_date+symbol+strategy)
    db = SessionLocal()
    try:
        inserted = 0
        updated  = 0
        for s in new_signals:
            existing = (
                db.query(ScanSignalModel)
                .filter_by(
                    market=s["market"], scan_date=s["scan_date"],
                    symbol=s["symbol"],  strategy=s["strategy"],
                )
                .first()
            )
            if existing:
                for k, v in s.items():
                    setattr(existing, k, v)
                updated += 1
            else:
                db.add(ScanSignalModel(**s))
                inserted += 1
        db.commit()
    finally:
        db.close()

    W = 60
    print(f"\n  {'='*W}")
    print(f"  Signals: {len(new_signals)} total  ({inserted} new, {updated} updated)")
    if new_signals:
        print(f"  {'Symbol':<12} {'Strategy':<22} {'Entry':>9} {'SL':>9} {'TP1':>9}")
        print(f"  {'-'*W}")
        for s in sorted(new_signals, key=lambda x: (x["strategy"], x["symbol"])):
            print(
                f"  {s['symbol']:<12} {s['strategy']:<22} "
                f"{s['entry_price']:>9.4f} {s['sl_price']:>9.4f} {s['tp1_price']:>9.4f}"
            )
    print(f"  {'='*W}\n")


def cmd_promote(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Interactive picker: promote optimised pairs to ActiveStrategyModel."""
    from datetime import date as _date
    from db.models import ActiveStrategyModel, RegimeOptimiseModel, SessionLocal

    market = adapter.market_id
    today = _date.today()
    db = SessionLocal()
    try:
        rows = (
            db.query(RegimeOptimiseModel)
            .filter_by(market=market)
            .order_by(RegimeOptimiseModel.strategy, RegimeOptimiseModel.regime)
            .all()
        )
        if not rows:
            print(f"  No optimise results for {market}. Run `optimise` first.")
            return

        W = 72
        print(f"\n{'='*W}")
        print(f"  PROMOTE — {market.upper()}  ({today})")
        print(f"{'='*W}")
        print(f"  {'#':>3}  {'Strategy':<22} {'Regime':<12} {'IS Cal':>7} {'OOS Cal':>8} {'OOS':>5}  Combo")
        print(f"  {'-'*W}")
        for i, r in enumerate(rows, 1):
            verdict = "PASS" if r.oos_pass else "fail"
            print(
                f"  {i:>3}.  {r.strategy:<22} {r.regime:<12} "
                f"{(r.is_calmar or 0):>7.2f} {(r.oos_calmar or 0):>8.2f} "
                f"{verdict:>5}  {r.best_combo or ''}"
            )
        print(f"  {'-'*W}\n")

        raw = input("  Numbers to promote (e.g. 1 3, blank=none): ").strip()
        if not raw:
            print("  Nothing promoted.")
            return

        indices = []
        for token in raw.replace(",", " ").split():
            try:
                idx = int(token) - 1
                if 0 <= idx < len(rows):
                    indices.append(idx)
            except ValueError:
                pass

        if not indices:
            print("  No valid selections.")
            return

        promoted = 0
        for idx in indices:
            r = rows[idx]
            existing = (
                db.query(ActiveStrategyModel)
                .filter_by(market=market, strategy=r.strategy, regime=r.regime, status="active")
                .all()
            )
            for old in existing:
                old.retired_at = today
                old.status = "retired"

            db.add(ActiveStrategyModel(
                market=market,
                strategy=r.strategy,
                regime=r.regime,
                params=r.params,
                promoted_at=today,
                status="active",
                source_combo=r.best_combo,
            ))
            promoted += 1
            print(f"  + promoted  {r.strategy} | {r.regime}")

        db.commit()
        print(f"\n  {promoted} pair(s) promoted. Run `active` to verify.")
    finally:
        db.close()


def cmd_active(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Print current active roster for market."""
    from db.models import ActiveStrategyModel, SessionLocal

    market = adapter.market_id
    db = SessionLocal()
    try:
        rows = (
            db.query(ActiveStrategyModel)
            .filter_by(market=market, status="active")
            .order_by(ActiveStrategyModel.strategy, ActiveStrategyModel.regime)
            .all()
        )
        W = 72
        print(f"\n{'='*W}")
        print(f"  ACTIVE ROSTER — {market.upper()}")
        print(f"{'='*W}")
        if not rows:
            print("  (empty — run `promote` to add pairs)")
        else:
            print(f"  {'#':>3}  {'Strategy':<22} {'Regime':<12} {'Promoted':<12}  Combo")
            print(f"  {'-'*W}")
            for i, r in enumerate(rows, 1):
                print(
                    f"  {i:>3}.  {r.strategy:<22} {r.regime:<12} "
                    f"{str(r.promoted_at):<12}  {r.source_combo or ''}"
                )
        print(f"{'='*W}\n")
    finally:
        db.close()


def cmd_history(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Print recent active-roster sync history for market."""
    from db.models import PipelineLog, SessionLocal

    market = adapter.market_id
    db = SessionLocal()
    try:
        rows = (
            db.query(PipelineLog)
            .filter_by(market=market, stage="active-sync")
            .order_by(PipelineLog.logged_at.desc())
            .limit(10)
            .all()
        )
    finally:
        db.close()

    W = 88
    print(f"\n{'='*W}")
    print(f"  ACTIVE HISTORY — {market.upper()}")
    print(f"{'='*W}")
    if not rows:
        print("  (empty — run `optimise` first)")
        print(f"{'='*W}\n")
        return

    for row in rows:
        details = row.details or {}
        added = details.get("added") or []
        changed = details.get("changed") or []
        retired = details.get("retired") or []
        active_count = details.get("active_count", 0)
        print(
            f"  {row.logged_at:%Y-%m-%d %H:%M}  "
            f"active={active_count} +{len(added)} ~{len(changed)} -{len(retired)}"
        )
        for item in added:
            print(f"    + {item['strategy']}|{item['regime']} -> {item['new_combo'] or 'n/a'}")
        for item in changed:
            print(
                f"    ~ {item['strategy']}|{item['regime']} "
                f"{item['old_combo'] or 'n/a'} -> {item['new_combo'] or 'n/a'}"
            )
        for item in retired:
            print(f"    - {item['strategy']}|{item['regime']} {item['old_combo'] or 'n/a'}")
        print("  " + "-" * (W - 2))
    print(f"{'='*W}\n")


def cmd_optimise_full(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Optimise pipeline: regime → optimise → stability → chart-export."""
    _banner = lambda n, t: print(f"\n{'='*60}\n  STEP {n}/4 — {t}\n{'='*60}")
    _banner(1, "regime");          cmd_regime(adapter, args)
    _banner(2, "optimise");        cmd_optimise_regime(adapter, args)
    _banner(3, "stability");       cmd_stability_report(adapter, args)
    _banner(4, "chart-export");    cmd_chart_export(adapter, args)


def cmd_run_all(adapter: MarketAdapter, args: argparse.Namespace) -> None:
    """Full pipeline: regime → optimise → stability → report → chart-export."""
    _banner = lambda n, t: print(f"\n{'='*60}\n  STEP {n}/5 — {t}\n{'='*60}")
    _banner(1, "regime");          cmd_regime(adapter, args)
    _banner(2, "optimise");        cmd_optimise_regime(adapter, args)
    _banner(3, "stability");       cmd_stability_report(adapter, args)
    _banner(4, "report");          cmd_report(adapter, args)
    _banner(5, "chart-export");    cmd_chart_export(adapter, args)


def run(adapter: MarketAdapter, command: str, args: argparse.Namespace) -> None:
    dispatch = {
        "run-all":        lambda: cmd_run_all(adapter, args),
        "regime":         lambda: cmd_regime(adapter, args),
        "research":       lambda: cmd_optimise_full(adapter, args),
        "optimise":       lambda: cmd_optimise_regime(adapter, args),
        "stability":      lambda: cmd_stability_report(adapter, args),
        "report":         lambda: cmd_report(adapter, args),
        "regime-report":  lambda: cmd_regime_report(adapter, args),
        "optimise-report":lambda: cmd_optimise_report(adapter, args),
        "regime-optimise":lambda: cmd_optimise_regime(adapter, args),
        "stability-report":lambda: cmd_stability_report(adapter, args),
        "chart-export":   lambda: cmd_chart_export(adapter, args),
        "intraday-entry-test":       lambda: cmd_intraday_entry_test(adapter, args),
        "intraday-fakeout-study":    lambda: cmd_intraday_fakeout_study(adapter, args),
        "paper-update":              lambda: cmd_paper_update(adapter, args),
        "scan":                      lambda: cmd_scan(adapter, args),
        "promote":                   lambda: cmd_promote(adapter, args),
        "active":                    lambda: cmd_active(adapter, args),
        "history":                   lambda: cmd_history(adapter, args),
    }
    if command in dispatch:
        dispatch[command]()
    else:
        print(f"  Unknown command: {command}. Available: {list(dispatch)}")
