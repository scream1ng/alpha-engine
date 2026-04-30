# Operations Guide

Step-by-step runbook — what to run, in what order, and how often.

---

## First-time setup

```bash
pip install -r requirements.txt

# Quick smoke test — 5 symbols only
python run.py th optimise-filter --symbols 5
```

---

## Workflow overview

```
1. diagnose         ← verify strategies fire signals
2. quick-report     ← review filter impact across 3 years (6 progressive phases)
3. optimise-filter  ← grid-search best entry filters (SMA, RVol, STR, RSM)
4. optimise-risk    ← grid-search best risk params (SL/TP multipliers)
5. scan             ← daily: generate live signals using approved params
```

Paper trading (`paper`) is optional validation before trusting live signals.

---

## Commands

```bash
python run.py [market] [command] [--capital N] [--symbols N]
```

| Command | What it does |
|---------|-------------|
| `diagnose` | Bar-by-bar scan over 12 months. Shows how many signals each strategy fires per day. Run before optimise. |
| `quick-report` | 6-phase progressive filter study × 3 years. Shows what each filter layer adds. No DB write. |
| `optimise-filter` | Y1 grid search over SMA / RVol / STR / RSM params. Ranked by annual_return. Saves best to DB. |
| `optimise-risk` | Y1 grid search over SL / TP / trail / partial params. Ranked by annual_return. Saves best to DB. |
| `report` | Print last optimise result from DB instantly — no recomputation. |
| `scan` | Generates today's signals using live-approved params from DB. |
| `paper` | Simulates paper trading over last 90 days using live params. |
| `validate` | Runs optimise-filter + optimise-risk then paper in sequence. |

---

## Frequency

| Task | Market | Frequency | Command |
|------|--------|-----------|---------|
| Signal scan | TH | Daily, 17:30 ICT (Mon–Fri) | `python run.py th scan` |
| Signal scan | US | Daily, 22:00 ET (Mon–Fri) | `python run.py us scan` |
| Signal scan | AU | Daily, 17:30 AEST (Mon–Fri) | `python run.py au scan` |
| Signal scan | Crypto | Daily, 00:05 UTC | `python run.py crypto scan` |
| Signal scan | Commodity | Daily, 22:00 ET (Mon–Fri) | `python run.py commodity scan` |
| Re-optimise | All | Monthly (1st of month) | `python run.py all optimise-filter && python run.py all optimise-risk` |
| Diagnose | All | Before each optimise | `python run.py all diagnose` |

---

## Monthly re-optimise procedure

Run on the 1st of each month (or weekend closest to it):

```bash
# 1. Check signals still firing
python run.py th diagnose --symbols 50

# 2. Review filter behavior across 3 years
python run.py th quick-report --symbols 50

# 3. Optimise filters (SMA, RVol, STR, RSM)
python run.py th optimise-filter --symbols 50 --capital 1000000

# 4. Optimise risk params (SL/TP/trail)
python run.py th optimise-risk --symbols 50 --capital 1000000

# 5. View the result
python run.py th report
```

Repeat for each market.

---

## Quick-report explained

```bash
python run.py th quick-report
python run.py all quick-report
python run.py th quick-report --symbols 20
```

Shows 6 progressive filter phases across 3 years (Y1 = most recent, Y3 = oldest):

| Phase | Filters active |
|-------|---------------|
| EMA10 exit only | Bare strategy — no entry filters, EMA10 hard exit |
| + SMA50 trend filter | Close must be above SMA50 |
| + RVol ≥1.5× filter | Relative volume ≥ 1.5× 20-day average |
| + STR ≤4.0 filter | Stretch ratio ≤ 4.0 (not overextended above SMA50) |
| + RSM ≥75 filter | RS Momentum rating ≥ 75 vs benchmark |
| + TP/BE risk mgmt | TP1=2×ATR (30% sell), TP2=4×ATR (30% sell), bars-based breakeven |

Each phase stacks on the previous. Compare Y1/Y2/Y3 columns to see if filters hold across regimes.

**Metrics per cell:** `Ret` (annual return %), `DD` (max drawdown %), `Tr` (trade count), `WR` (win rate %)

---

## STR (Stretch) indicator

`STR = (close − SMA50) / ATR`

Measures how many ATRs price has extended above its 50-day SMA.

| STR value | Interpretation |
|-----------|---------------|
| ≤ 4.0 | Acceptable stretch — eligible for entry |
| > 4.0 | Overextended — signal blocked |

Controlled by `str_max` param (0 = disabled). Default in all strategies: `str_max = 0`.

---

## RSM (RS Momentum) filter

Rolling relative strength rating (1–99) vs market benchmark. Computed over a 21-bar window.

- `rsm_min = 0` → disabled
- `rsm_min = 75` → only stocks outperforming 75% of the market
- Not applied to crypto or commodity markets (no benchmark)

---

## Gate thresholds (optimise)

| Metric | Minimum | Why |
|--------|---------|-----|
| Annual Return | 3% | Bootstrap gate for Y1 sample |
| Sharpe | 0.0 | Not blocked on low-trade windows |
| Calmar | 0.0 | Not blocked on tiny/zero drawdown |
| Profit Factor | 1.05 | Gross wins must exceed gross losses |
| Win Rate | 30% | Allows low-WR / high-RR strategies |
| Trades | 3 | Avoid zero-trade approvals |

---

## Trade counting

Partial exits (TP1 sell + TP2 sell + final exit) count as **one trade**, grouped by `position_id`. Metrics (win rate, profit factor, avg win/loss) are all per-entry, not per-exit-event.

---

## When to re-optimise early

- Strategy drops from `LIVE ✓` to `not live` after a scan cycle
- Annual return on live trades diverges significantly from backtest
- Market regime shift (index drops >20% in 3 months)
- New strategy added — run optimise to get its params before scanning

---

## Inspect the database

```bash
# Quick status — which strategies are live right now
sqlite3 alpha_engine_dev.db \
  "SELECT market, strategy, printf('%.1f%%', backtest_annual_return*100) as ann_ret, is_live FROM strategy_params ORDER BY market, is_live DESC;"
```

```python
# In Python
from db.models import SessionLocal, StrategyParamsModel
db = SessionLocal()
for r in db.query(StrategyParamsModel).filter_by(is_live=True).all():
    print(r.market, r.strategy, f"{(r.backtest_annual_return or 0)*100:.1f}%")
```

---

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `DATABASE_URL` | `sqlite:///alpha_engine_dev.db` | Override for Postgres in prod |

```bash
# Production
DATABASE_URL=postgresql://user:pass@host/dbname python run.py th scan
```

---

## Adding a new strategy

1. Create `strategies/my_strategy.py`, subclass `Strategy`, add `@StrategyRegistry.register`
2. Implement `scan(df, params)` and `param_space()`
3. Include `rsm_min: 0` and `str_max: 0` in `default_params`
4. Add `_rsm_ok` and `_stretch_ok` checks in `scan()` after trend/volume checks
5. Add `rsm_min` and `str_max` to `param_space()`
6. Add strategy id to `enabled_strategies` in `config.py` for target markets
7. Run `diagnose` — verify it fires signals
8. Run `optimise-filter` — grid search finds best params automatically
