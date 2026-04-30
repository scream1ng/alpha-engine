# Alpha Engine

Multi-market, multi-strategy algorithmic signal system. Runs isolated pipelines per market, validates strategies through grid-search optimisation, then writes live signals to a shared database.

**Markets:** Thailand SET · US Equities · AU ASX · Crypto · Commodity

**Strategies:** Pivot Breakout · Trendline Breakout · Pullback Buy · Reversal · BB Squeeze · MA Cross · Narrow Range

---

## Quick start

```bash
pip install -r requirements.txt

python run.py th diagnose                      # check strategies fire signals
python run.py th quick-report                  # progressive filter study over 3 years
python run.py th optimise-filter               # grid-search best entry filters
python run.py th optimise-risk                 # grid-search best risk params
python run.py th report                        # view last optimise result from DB
python run.py th scan                          # generate today's signals

python run.py all scan                         # scan every market at once
```

> See [OPERATIONS.md](OPERATIONS.md) for full runbook, frequencies, and scheduling.

---

## How it works

```
diagnose → optimise-filter → optimise-risk → scan (daily)
                ↓
         Y1 grid search (last 365 days)
         ranked by annual_return
         best params saved to DB with is_live=True
                ↓
         scan reads only live-approved strategies
```

No strategy reaches live trading without passing all gates. Re-optimise monthly to keep params current.

---

## Project structure

```
alpha-engine/
├── config.py                   ← Market configs, gate thresholds, scoring weights
├── run.py                      ← CLI entry point (delegates to scripts/)
├── OPERATIONS.md               ← Runbook: what to run, in what order, how often
│
├── scripts/
│   └── pipeline.py             ← Shared pipeline logic (all markets use this)
│
├── core/
│   ├── signal.py               ← Signal, Position, ExitSignal dataclasses (position_id for trade counting)
│   ├── registry.py             ← StrategyRegistry — auto-discovery via decorator
│   ├── exit_policy.py          ← SL / TP1 partial / TP2 partial / trail / EMA exit
│   ├── indicators.py           ← ATR, RSI, BB, KC, EMA, SMA, RVol, ADX, RSM, STR, momentum
│   ├── universe.py             ← TradingView screener (TH/US/AU) + stub fallbacks
│   ├── tx_cost.py              ← Transaction cost model per market
│   ├── risk_policy.py          ← Portfolio heat cap + position sizing
│   ├── ledger.py               ← Portfolio-level trade ledger (groups partials by position_id)
│   ├── ranker.py               ← Signal composite scorer
│   ├── guard.py                ← Look-ahead bias guard
│   └── paper_trade.py          ← Paper trading simulator
│
├── markets/
│   ├── base.py                 ← MarketAdapter ABC
│   ├── th.py                   ← Thailand SET (yfinance)
│   ├── us.py                   ← US equities (yfinance)
│   ├── au.py                   ← AU ASX (yfinance)
│   ├── crypto.py               ← Crypto spot (yfinance)
│   └── commodity.py            ← Commodity futures (yfinance)
│
├── strategies/
│   ├── base.py                 ← Strategy ABC: _build_signal, _in_uptrend, _rsm_ok, _stretch_ok
│   ├── pivot_breakout.py       ← Break above recent high with volume
│   ├── trendline_breakout.py   ← Descending trendline fan breakout
│   ├── pullback_buy.py         ← Pullback to breakpoint in uptrend
│   ├── reversal.py             ← RSI oversold + engulfing/hammer + volume
│   ├── bb_squeeze.py           ← Bollinger Band squeeze release
│   ├── ma_cross.py             ← EMA fast/slow crossover with trend filter
│   └── narrow_range.py         ← NR7 pending stop-buy on volatility contraction
│
├── validation/
│   ├── backtest.py             ← Bar-by-bar backtest engine (partials grouped by position_id)
│   ├── optimizer.py            ← Y1 grid search ranked by annual_return
│   └── consistency.py          ← 2yr vs 1yr drift check (threshold: 50%)
│
└── db/
    └── models.py               ← SQLAlchemy models (SQLite dev / Postgres prod)
```

---

## Filter stack

Every strategy applies filters in this order (each param defaults to 0 = disabled):

| Filter | Param | Description |
|--------|-------|-------------|
| Trend | `trend_filter` / `trend_sma_period` | Close must be above SMA(N). Multi-SMA via `"50_100"` syntax. |
| Volume | `rvol_min` | Relative volume must exceed threshold (e.g. 1.5× 20-day avg). |
| Stretch | `str_max` | `STR = (close − SMA50) / ATR`. Blocks signals where price is overextended (e.g. STR > 4). |
| RS Momentum | `rsm_min` | Rolling RS rating 1–99 vs benchmark. Not applied to crypto/commodity. |

---

## Exit structure

Each signal has a layered exit:

| Step | Condition | Action |
|------|-----------|--------|
| 1 | Price hits SL | Full close — cut loss |
| 2 | Price hits TP1 | Sell `tp1_partial_pct`% (default 30%), move SL to breakeven |
| 3 | Price hits TP2 | Sell `tp2_partial_pct`% of remainder |
| 4 | Trailing stop | Trail remainder at `trail_atr_mult × ATR` |
| 5 | EMA exit | Close < EMA10 after TP1 hit → exit remainder |
| 6 | Time stop | Exit if open > `max_bars` bars |

TP1 + TP2 + final exit all count as **one trade** (grouped by `position_id`).

---

## Risk management

Sizing is ATR-based. Grid search discovers optimal multipliers per strategy:

| Param | Controls | Optimised range |
|-------|----------|----------------|
| `sl_atr_mult` | Stop loss width | 0.75–2.0× ATR |
| `tp1_atr_mult` | First target | 1.0–3.0× ATR |
| `tp2_atr_mult` | Second target | 3.0–5.0× ATR |
| `tp1_partial_pct` | Fraction sold at TP1 | 20–50% |
| `tp2_partial_pct` | Fraction of remainder sold at TP2 | 20–50% |
| `trail_atr_mult` | Trailing stop distance | 1.0–2.0× ATR |
| `risk_pct` | Capital at risk per trade | 0.3–0.5% |
| `max_bars` | Time stop | strategy-dependent |

---

## Environment

| Variable | Default | Notes |
|----------|---------|-------|
| `DATABASE_URL` | `sqlite:///alpha_engine_dev.db` | Override for Postgres in prod |

```bash
# Production
DATABASE_URL=postgresql://user:pass@host/dbname python run.py th scan
```

---

## Adding a strategy

1. Create `strategies/my_strategy.py`
2. Subclass `Strategy`, add `@StrategyRegistry.register`
3. Implement `scan(df, params) -> list[Signal]` and `param_space() -> dict`
4. Include `rsm_min`, `str_max` in `default_params` and `param_space()`
5. Add `if not self._rsm_ok(df, p): return []` and `if not self._stretch_ok(df, p): return []` in `scan()`
6. Add strategy id to `enabled_strategies` in `config.py`
7. Run `diagnose` → verify fires, then `optimise-filter` → picks it up

```python
from core.registry import StrategyRegistry
from strategies.base import Strategy

@StrategyRegistry.register
class MyStrategy(Strategy):
    id = "my_strategy"
    default_params = {
        "sl_atr_mult": 1.5, "tp1_atr_mult": 2.0,
        "rsm_min": 0, "str_max": 0, ...
    }

    def scan(self, df, params):
        p = {**self.default_params, **params}
        if not self._in_uptrend(df, p): return []
        if not self._rsm_ok(df, p): return []
        if not self._stretch_ok(df, p): return []
        ...
        return [self._build_signal(df=df, params=p, entry=price,
                                   entry_type="market_close", atr_val=atr)]

    def param_space(self):
        return {
            "sl_atr_mult": [1.0, 1.5, 2.0],
            "tp1_atr_mult": [1.5, 2.0, 2.5],
            "rsm_min": [0, 75, 80],
            "str_max": [0, 3, 4, 5],
            ...
        }
```
