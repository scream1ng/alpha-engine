# Alpha Engine

Multi-market, multi-strategy algorithmic signal system. Runs isolated pipelines per market, validates strategies through walk-forward optimisation, then writes live signals to a shared database.

**Markets:** Thailand SET · US Equities · AU ASX · Crypto · Commodity

**Strategies:** Pivot Breakout · Pullback Buy · Reversal · BB Squeeze · MA Cross · Narrow Range

---

## Quick start

```bash
pip install -r requirements.txt

python run.py th diagnose                      # check strategies fire signals
python run.py th quick-report                  # fixed params over last 1 year
python run.py th optimise --capital 1000000    # walk-forward optimise
python run.py th report                        # view results instantly
python run.py th scan                          # generate today's signals

python run.py all scan                         # scan every market at once
```

> See [OPERATIONS.md](OPERATIONS.md) for full runbook, frequencies, and scheduling.

---

## How it works

```
diagnose → optimise → scan (daily)
              ↓
         walk-forward (18m train / 6m test)
        gate: Annual Return ≥3%, Sharpe ≥0.0, Calmar ≥0.0, PF ≥1.05, WR ≥30%, Trades ≥3
         consistency: recent 1yr metrics must be ≥50% of 2yr
              ↓
         params saved to DB with is_live=True
              ↓
         scan reads only live-approved strategies
```

No strategy reaches live trading without passing all gates. Re-optimise monthly to keep params current.

---

## Project structure

```
alpha-engine/
├── config.py                   ← Market configs, gate thresholds, scoring weights
├── main.py                     ← CLI entry point (delegates to scripts/)
├── OPERATIONS.md               ← Runbook: what to run, in what order, how often
│
├── scripts/
│   └── pipeline.py             ← Shared pipeline logic (all markets use this)
│
├── charts/
│   └── nr7.py                  ← NR7 candlestick chart visualiser
│
├── core/
│   ├── signal.py               ← Signal, Position, ExitSignal dataclasses
│   ├── registry.py             ← StrategyRegistry — auto-discovery via decorator
│   ├── exit_policy.py          ← HardExitPolicy: SL / TP1 partial / TP2 partial / trail / EMA exit
│   ├── indicators.py           ← ATR, RSI, BB, KC, EMA, SMA, RVol, momentum
│   ├── universe.py             ← TradingView screener (TH/US/AU) + stub fallbacks
│   ├── tx_cost.py              ← Transaction cost model per market
│   ├── risk.py                 ← Correlation-aware heat cap
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
│   ├── base.py                 ← Strategy ABC + _build_signal, _in_uptrend helpers
│   ├── pivot_breakout.py       ← Break above recent high with volume
│   ├── pullback_buy.py         ← Pullback to EMA in uptrend
│   ├── reversal.py             ← RSI oversold + engulfing/hammer + volume
│   ├── bb_squeeze.py           ← Bollinger Band squeeze release
│   ├── ma_cross.py             ← EMA fast/slow crossover with trend filter
│   └── narrow_range.py         ← NR7 pending stop-buy on volatility contraction
│
├── validation/
│   ├── backtest.py             ← Bar-by-bar backtest engine
│   ├── optimizer.py            ← Walk-forward grid search (18m train / 6m test)
│   └── consistency.py          ← 2yr vs 1yr drift check (threshold: 50%)
│
└── db/
    └── models.py               ← SQLAlchemy models (SQLite dev / Postgres prod)
```

---

## Exit structure

Each signal has a layered exit:

| Step | Condition | Action |
|------|-----------|--------|
| 1 | Price hits SL | Full close — cut loss |
| 2 | Price hits TP1 | Sell `tp1_partial_pct`% (default 30%), move SL to breakeven |
| 3 | Price hits TP2 | Sell `tp2_partial_pct`% of remainder |
| 4 | Trailing stop | Trail remainder at `trail_atr_mult × ATR` |
| 5 | EMA exit | Close < EMA5/10 after TP1 hit → exit remainder |
| 6 | Time stop | Exit if open > `max_bars` bars |

All exit params are walk-forward optimised per strategy.

---

## Risk management

Sizing is ATR-based. Walk-forward discovers optimal multipliers per strategy:

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
DATABASE_URL=postgresql://user:pass@host/dbname python scripts/run_th.py scan
```

---

## Adding a strategy

1. Create `strategies/my_strategy.py`
2. Subclass `Strategy`, add `@StrategyRegistry.register`
3. Implement `scan(df, params) -> list[Signal]` and `param_space() -> dict`
4. Add strategy id to `enabled_strategies` in `config.py`
5. Run `diagnose` → verify fires, then `optimise` → walk-forward picks it up

```python
from core.registry import StrategyRegistry
from strategies.base import Strategy

@StrategyRegistry.register
class MyStrategy(Strategy):
    id = "my_strategy"
    default_params = {"sl_atr_mult": 1.5, "tp1_atr_mult": 2.0, ...}

    def scan(self, df, params):
        ...
        return [self._build_signal(df=df, params=params, entry=price,
                                   entry_type="market_close", atr_val=atr)]

    def param_space(self):
        return {"sl_atr_mult": [1.0, 1.5, 2.0], "tp1_atr_mult": [1.5, 2.0, 2.5], ...}
```
