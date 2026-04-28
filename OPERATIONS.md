# Operations Guide

Step-by-step runbook — what to run, in what order, and how often.

---

## First-time setup

```bash
pip install -r requirements.txt

# Creates SQLite DB + tables (including any column migrations)
python run.py th optimise --symbols 5   # quick smoke test, 5 symbols only
```

---

## Workflow overview

```
1. diagnose   ← verify strategies fire (run before every optimise)
2. optimise   ← find best params per strategy, write to DB
3. scan       ← daily: generate live signals using approved params
```

Paper trading (`paper`) is optional validation before trusting live signals.

---

## Commands

All commands work for every market runner (`run_th.py`, `run_us.py`, `run_au.py`, `run_crypto.py`, `run_commodity.py`).

```bash
python run.py [market] [command] [--capital N] [--symbols N]
```

| Command | What it does |
|---------|-------------|
| `diagnose` | Bar-by-bar scan over 12 months. Shows how many signals each strategy fires. Run before optimise. |
| `quick-report` | Runs one fixed parameter set over the last 12 months and prints simple per-strategy results. No gate, no DB write. |
| `optimise` | Walk-forward optimisation (18m train / 6m test). Saves best params + live status to DB. |
| `report` | Print last optimise result from DB instantly — no recomputation. |
| `scan` | Generates today's signals using live-approved params from DB. |
| `paper` | Simulates paper trading over last 90 days using live params. |
| `validate` | Runs optimise then paper in sequence. |
| `live` | Same as scan (wire to broker execution when ready). |

---

## Frequency

| Task | Market | Frequency | Command |
|------|--------|-----------|---------|
| Signal scan | TH | Daily, 17:30 ICT (Mon–Fri) | `python run.py th scan` |
| Signal scan | US | Daily, 22:00 ET (Mon–Fri) | `python run.py us scan` |
| Signal scan | AU | Daily, 17:30 AEST (Mon–Fri) | `python run.py au scan` |
| Signal scan | Crypto | Daily, 00:05 UTC | `python run.py crypto scan` |
| Signal scan | Commodity | Daily, 22:00 ET (Mon–Fri) | `python run.py commodity scan` |
| Re-optimise | All | Monthly (1st of month) | `python run.py all optimise` |
| Diagnose | All | Before each optimise | `python run.py all diagnose` |

---

## Monthly re-optimise procedure

Run on the 1st of each month (or weekend closest to it):

```bash
# 1. Check signals still firing
python run.py th diagnose --symbols 50

# 2. Re-optimise (uses latest 5yr rolling history)
python run.py th optimise --symbols 50 --capital 1000000

# 3. View the result
python run.py th report
```

Repeat for each market.

## Quick fixed-setting check

Use this when you want a fast read on strategy behavior before running full optimisation:

```bash
python run.py th quick-report
python run.py all quick-report
python run.py th quick-report --symbols 20
```

`quick-report` uses one shared setting across all strategies over the last 12 months:

- `rvol_min = 2.0`
- `trend_sma_period = 50`
- `sl_atr_mult = 1.0`
- `tp1_atr_mult = 2.0`, `tp1_partial_pct = 0.3`
- `tp2_atr_mult = 2.0`, `tp2_partial_pct = 0.3`
- `ema_exit_period = 10` with hard EMA exit enabled
- move SL to breakeven after 3 bars
- `risk_pct = 0.005`

---

## Reading the optimise report

```
┌─ PIVOT_BREAKOUT  [LIVE ✓]
│  OOS Return:     +12.8%✓(≥15%)  (annualised, latest OOS window, median across sampled symbols)
│  Risk metrics :  Sharpe=1.45✓  Calmar=1.2✓  PF=2.10✓  WR=58%✓
│  Signal filter:  rvol_min=1.2  psth=0.005
│  Entry → Exit :
│    SL     = 1.5×ATR
│    Trend  = SMA100 uptrend filter
│    TP1    = 3.0×ATR  → sell 30%,  70% remains,  SL→breakeven
│    TP2    = 4.5×ATR  → sell 30% of remaining,  49% trails to stop
│    Trail  = 2.0×ATR
│    EMA    = EMA10  (hard exit if close < EMA after TP1)
│    Time   = exit after 20 bars
│  Risk     :  0.50% capital per trade  |  Raw RR = 2.0:1
└────────────────────────────────────────────────────────────
```

Status meanings:
- `LIVE ✓` — strategy approved, params active, scan will use it
- `not live — gate fail` — metrics below threshold (Annual Return, Sharpe, Calmar, PF, or WR)
- `not live — consistency fail` — metrics degraded >50% in recent 1yr vs 2yr (regime change)

`optimise` now selects one shared parameter set per strategy for the sampled market universe. Reported metrics are from the latest valid walk-forward OOS window and aggregated across the sampled symbols, not a single best stock.

---

## Gate thresholds

| Metric | Minimum | Why |
|--------|---------|-----|
| Annual Return | 3% | Bootstrap gate for small 6m OOS samples |
| Sharpe | 0.0 | Do not block early approval on unstable low-trade Sharpe |
| Calmar | 0.0 | Do not block early approval on tiny or zero drawdown windows |
| Profit Factor | 1.05 | Require gross wins to exceed gross losses |
| Win Rate | 30% | Allow lower-WR, higher-RR strategies |
| Trades | 3 | Enough to avoid pure zero-trade approvals |
| Consistency | 50% | Recent 1yr metrics ≥ 50% of 2yr metrics |

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
DATABASE_URL=postgresql://user:pass@host/dbname python scripts/run_th.py scan
```

---

## Adding a new strategy

1. Create `strategies/my_strategy.py`, subclass `Strategy`, add `@StrategyRegistry.register`
2. Implement `scan(df, params)` and `param_space()`
3. Add strategy id to `enabled_strategies` in `config.py` for target markets
4. Run `diagnose` — verify it fires signals
5. Run `optimise` — walk-forward finds best params automatically
