# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Interactive menu (market → command)
python run.py

# CLI (non-interactive)
python run.py th regime
python run.py th optimise-regime
python run.py th scan
python run.py th regime-report
python run.py th optimise-regime

# Always set encoding on Windows (Thai locale cp874 breaks Unicode arrows)
PYTHONIOENCODING=utf-8 python run.py th <command>

# Tests
pytest tests/
pytest tests/test_research.py::test_label_regime   # single test
```

Available commands: `regime`, `optimise-regime`, `scan`, `regime-report`, `research`, `optimise`, `report`, `chart`, `regime-optimise`, `paper`, `diagnose`

## Simplified Pipeline (monthly cadence)

```
regime  →  optimise-regime  →  scan
```

1. **`regime`** — 5yr backtest of all 7 strategies across 3 regimes (uptrend/choppy/downtrend). Writes `reports/th_regime_latest.md` and saves regime labels to `regime_labels` DB table.
2. **`optimise-regime`** — tests 3 TP exit combos on PASS pairs (calmar ≥ 0.3). Reads regime labels from DB (not recomputed) for consistency. Writes `reports/th_optimise_latest.md`.
3. **`scan`** — generates today's signals using live strategy+regime params.

## Architecture

### Entry & Dispatch

`run.py` — menu/CLI shell. Calls `scripts/pipeline.run(adapter, command, args)`.

`scripts/pipeline.py` — monolithic command file (~2200 lines). All `cmd_*` functions live here. Dispatch table at bottom (`run()` function). Key param constants at top (lines 18–110).

### Market Adapters (`markets/`)

`MarketAdapter` ABC in `markets/base.py`: `universe()`, `ohlcv()`, `tx_costs()`, `rsm()`, `benchmark` property. Loaded dynamically by `run.py` via importlib. Each adapter knows its own universe fetch (TradingView screener for TH).

### Strategies (`strategies/`)

`Strategy` ABC in `strategies/base.py`: `id`, `default_params`, `scan(df, params) → list[Signal]`. Registered via `@StrategyRegistry.register` decorator. Auto-loaded by `strategies/__init__.py` on import.

7 strategies: `pivot_breakout`, `trendline_breakout`, `pullback_buy`, `reversal`, `bb_squeeze`, `ma_cross`, `narrow_range`.

Strategy `default_params` define per-strategy rvol/SL/TP/trail defaults. These get **overridden** by pipeline param dicts — so check the active param dict, not just default_params, when debugging unexpected results.

### Regime System

`core/regime.py: label_regime(bm_close)` — assigns uptrend/choppy/downtrend per bar using SMA50/200 on benchmark close. Regime labels are **saved to DB** by `cmd_regime` (`RegimeLabelModel`) so `cmd_optimise_regime` uses identical boundaries. Do not recompute labels independently inside optimise — read from DB.

### Exit Policy (`core/exit_policy.py`)

`hard_stop_mode` is the gate for EMA exit — must be `"ema10"` or `"both"` for EMA exit to fire. `ema_exit_period` is the period. These are NOT interchangeable — setting period without mode does nothing.

Trail (999.0 = off), BE trigger (999.0 = off), TP2 (999.0 = no second target).

### Backtest (`validation/backtest.py`)

`run_portfolio_backtest(all_dfs, strategy, params, initial_capital)` — runs bar-by-bar across all symbols, returns `{"trades": [...], "metrics": {...}}`. Pre-computes 12+ indicators via `_precompute_indicators(df)` (ATR, RVOL, RSI, EMA5/10, SMA50/100/200, RSM, stretch).

Trade dict keys: `entry_date`, `exit_date`, `pnl`, `position_id`, `regime` (tagged post-backtest).

### Database (`db/models.py`, SQLite default)

Key models:
- `RegimeLabelModel` — date→regime mapping saved by `cmd_regime`, read by `cmd_optimise_regime`
- `RegimeMapModel` — per strategy×regime calmar/wr/dd results
- `RegimeOptimiseModel` — best params per strategy×regime after TP optimisation
- `StrategyParamsModel` — optimised params from full IS/OOS pipeline

`init_db()` creates tables + runs ALTER TABLE migrations for SQLite compatibility.

## Key Constants (scripts/pipeline.py)

| Constant | Purpose |
|---|---|
| `_REGIME_DISCOVERY_PARAMS` | Fixed params for regime discovery (SL 1.5×ATR, TP1 3×ATR full exit, no EMA/trail) |
| `_OPT_REGIME_BASE` | Base params for optimise-regime (SL 1.5×ATR, BE off, trail off) |
| `_OPT_REGIME_TP_COMBOS` | 3 exit structures: original / tp1_50pct_ema10 / tp1_30pct_tp2_30pct_ema10 |
| `_OPT_REGIME_MIN_CALMAR` | 0.3 — minimum calmar to include a pair in optimise-regime |
| `EDGE_CALMAR` | 0.5 — local to `cmd_regime`, gate for PASS classification |

## Calmar Calculation

Calmar is **annualized**: `(total_ret * 252 / n_regime_bars) / max_drawdown`. `n_regime_bars` = number of benchmark bars labeled as that regime over the 5yr window (not calendar days). Both `cmd_regime` and `_p3_metrics_from_trades` must use this formula — they were inconsistent previously (fixed).

## Reports

Each command writes a markdown report to `reports/<market>_<command>_latest.md` and a timestamped copy to `reports/history/`. The `## AI Suggestions` section is the feedback hook — AI reads the report and writes improvement suggestions there before the next run.

## Known Instabilities

- `trendline_breakout | downtrend` calmar is benchmark-boundary-sensitive — small changes in benchmark data shift regime boundaries, moving large wins across the boundary. Not a robust pair.
- Regime labels must come from DB (saved by `cmd_regime`) to ensure `optimise-regime` uses identical boundaries. If regime was never run, optimise falls back to a fresh fetch (may differ).
- Windows Thai locale (cp874) requires `PYTHONIOENCODING=utf-8` or the UTF-8 wrapper in `run.py`.
