# AlphaEngine — System Design

> Revised: 2026-04-26
> Status: Design spec, pre-implementation
> Origin: Evolved from `breakout-signal` (single-market SET swing trader)

---

## TL;DR

Multi-market, multi-strategy signal system. Each market runs an **isolated pipeline** (its own strategies, optimisation, validation, paper trading) and writes to a **shared database**. A single dashboard reads from the DB and presents unified signals, portfolio heat, and live PnL across all markets.

**Core rule**: No market feeds the dashboard until its strategies pass three validation phases — walk-forward optimisation, consistency check (2yr vs 1yr backtest drift), and paper trading performance gate.

---

## 1. System architecture

### 1.1 High-level

```
┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│ US script│  │ AU script│  │ TH script│  │Crypto scr│  │Commod scr│
└────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘
     └─────────────┴──────┬──────┴─────────────┴─────────────┘
                          ▼
                    ┌──────────┐
                    │ Shared DB│  (signals, trades, positions, PnL)
                    └─────┬────┘
                          ▼
                    ┌──────────┐
                    │ Dashboard│  (reads only — never writes)
                    └──────────┘
```

Five independent scripts. Each runs on its own schedule (TH at SET close, US at NYSE close, Crypto on cron, etc.). Scripts never talk to each other — they only share the DB. If one crashes, the other four keep running.

### 1.2 Per-market pipeline template

Every market instantiates this same internal pipeline:

```
Market config (universe · costs · regime thresholds · broker)
      ↓
OHLCV cache + look-ahead guard
      ↓
Indicator engine (pandas-ta)         ← Regime filter (VIX · ADX)
      ↓                                     │
Regime gate  ◄────────────────────────────── (blocks signals in chop)
      ↓
Strategy registry (market-filtered)  ← Walk-forward optimiser
      ↓
Signal ranker (cost-adjusted 0–100)  ← Tx cost model
      ↓
Money management (currency · lots · heat cap)
      ↓
Order router (market entry vs pending orders)
      ↓
Paper-to-live gate
      ↓
Trade engine → market broker API
      ↓
Shared DB (signals · trades · PnL)
```

### 1.3 Global layer (reads from DB)

- **Global portfolio ledger** — aggregates positions and PnL across all markets for unified heat calculation
- **Cross-market analysis** — correlation between pipelines, alpha vs benchmark, global drawdown
- **Master dashboard** — multi-market, multi-strategy view (TradingView-style end state)

---

## 2. Three-phase validation

Every strategy passes through three phases before it feeds the dashboard. This is the quality gate that keeps the dashboard trustworthy.

### Phase 1 — Strategy finding (offline, days to weeks)

1. Collect full market history (with point-in-time index membership — no survivorship bias)
2. Run all candidate strategies against full history
3. **Walk-forward optimise** — rolling 18-month train / 6-month test windows, find best params per strategy. Params include both strategy-specific and RM params (see section 4).
4. **Consistency check** *(new layer)* — backtest 2yr period vs 1yr period, compare Sharpe/PF/win rate. Flag as fail if any key metric in the recent year is less than 60% of the older period.
5. **Selection gate** — Calmar > threshold, Sharpe > threshold, profit factor > threshold, win rate > threshold, minimum trade count

### Phase 2 — Validation (live paper, weeks to months)

1. **Paper trade** — real signals, real timing, no real money
2. **Performance gate** — minimum 3 months or 30 trades (whichever comes later). Actual win rate and drawdown must be within tolerance of backtest expectations.

### Phase 3 — Live feed

1. **Promote to live** — strategy now sizes real positions with market-specific MM rules
2. **Feed shared DB** — writes signals, fills, and PnL. Dashboard picks up automatically.

### Fail paths

- **Consistency check fail** → re-optimise on fresh data. Strategy may still work but params have drifted.
- **Performance gate fail** → re-run consistency check. If the strategy has degraded in recent data too, consider retiring it. Otherwise re-optimise and restart Phase 2.

---

## 3. Markets config

| Market | Universe | Data source | Benchmark | Broker | Currency | Special handling |
|---|---|---|---|---|---|---|
| US | S&P500 + NASDAQ100 | Polygon / yfinance | SPY | Alpaca | USD | Min ADV $5M |
| AU | ASX200 | yfinance `.AX` | ^AXJO | IBKR | AUD | Board lots, min $500K/day ADV |
| TH | SET100 | SETTRADE OpenAPI | ^SET.BK | SETTRADE | THB | Board lots (100-share units) |
| Crypto | Top 50 spot | ccxt (Binance) | BTC-USD | Binance | USDT | 24/7, funding rate awareness |
| Commodity | 10 liquid futures | yfinance (GC=F, CL=F…) | DXY | IBKR | USD | Contract roll handling |

---

## 4. Strategies

Each market enables a subset of these. Strategy-market fit is decided in Phase 1.

Walk-forward optimises **both strategy params and RM params together** — each strategy gets its own optimal SL width, TP targets, position sizing, and exit behaviour. ATR is the foundation for all price-relative params; multipliers are discovered per strategy.

### RM params (shared across all strategies, optimised per-strategy)

```
sl_atr_mult:          [1.5, 2.0, 2.5, 3.0]   ← SL width in ATRs
tp1_atr_mult:         [1.5, 2.0, 2.5, 3.0]   ← first target
tp2_atr_mult:         [3.0, 3.5, 4.0, 5.0]   ← second target
risk_pct:             [0.003, 0.005, 0.0075]  ← capital at risk per trade
max_bars:             [5, 10, 15, 20]          ← time stop (hard exit)
trail_atr_mult:       [1.5, 2.0, 2.5]         ← trailing stop distance
be_trigger_atr_mult:  [0.5, 1.0, 1.5]         ← breakeven move trigger
```

Optimisation scoring: `0.40 × Calmar + 0.30 × Sharpe + 0.20 × PF + 0.10 × win_rate`
Calmar is primary because it directly penalises drawdown — better signal for RM tuning.

### Hard exit priority order

```
1. Hard SL hit              ← always first, no override
2. TP2 hit                  ← full exit
3. TP1 hit (partial 50%)    ← move SL to entry
4. Breakeven trigger        ← move SL to entry if price > entry + be_trigger×ATR
5. Trailing stop update     ← trail highest_close - trail_atr_mult×ATR
6. max_bars reached         ← time stop, close at market open next bar
```

### 4.1 PivotBreakout (port from breakout-signal)

Entry on pivot break with volume confirmation.
Strategy params: `psth`, `rvol_min`, `rsm_min`, `lookback`

### 4.2 PullbackBuy

```
1. Stock breaks pivot → enters watchlist
2. Price pulls back to breakpoint ± 0.5×ATR
3. Reversal candle: body > wick, close in upper 30%
4. RVol < avg on pullback (healthy retrace, not panic)
5. Entry: close. SL: low of pullback candle. TP: prior swing high.
```
Strategy params: `lookback`, `pullback_atr_band`, `rvol_max_on_pullback`

### 4.3 Reversal

```
1. Stock down ≥ 3 consecutive days OR RSI(14) < 30
2. Price at key horizontal support (pivot low, last 60 bars)
3. Bullish engulfing OR hammer candle today
4. RVol ≥ 1.5× on reversal bar
5. Entry: close. SL: candle low. TP1: 1.5×ATR (tight — it's a fade).
```
Strategy params: `rsi_threshold`, `consec_down_days`, `rvol_min`, `support_lookback`

### 4.4 BBSqueeze (Carter-style)

```
1. Bollinger Bands inside Keltner Channel = squeeze
2. Squeeze releases (BB expands outside KC)
3. Momentum histogram turns positive (first bar of expansion)
4. Entry: close on release bar. SL: BB midline. TP1: 2×ATR, TP2: upper BB.
```
Strategy params: `bb_period`, `bb_std`, `kc_period`, `kc_mult`

### 4.5 NarrowRange (NR7)

```
1. Today's range = smallest of last 7 bars
2. ATR% < 1%
3. Next day: break above NR7 high + RVol ≥ 1.5×
4. Entry: NR7 high + 1 tick (pending order). SL: NR7 low. TP: 2×ATR.
```
**Requires pending order support** — different from other strategies that enter at close.
Strategy params: `nr_period`, `atr_pct_max`, `rvol_min`

### 4.6 MAcross

```
1. EMA20 crosses above EMA50 (fresh trend)
2. Price > SMA200 (long-term regime)
3. RVol on cross bar ≥ 1.5×
4. Entry: next open. SL: EMA50. TP: trail EMA20.
```
Strategy params: `fast_period`, `slow_period`, `trend_period`, `rvol_min`

---

## 5. Key abstractions

### 5.1 MarketAdapter

```python
class MarketAdapter(ABC):
    market_id: str          # "us" | "au" | "th" | "crypto" | "commodity"
    benchmark: str          # "SPY", "^AXJO", "^SET.BK", "BTC-USD"
    currency: str           # "USD", "AUD", "THB"
    min_adv: float          # min average daily volume filter
    lot_size: int           # 1 for US, 100 for TH, etc.

    @abstractmethod
    def universe(self, as_of: date) -> list[str]:
        """Point-in-time universe — no survivorship bias."""

    @abstractmethod
    def ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...

    @abstractmethod
    def tx_costs(self, symbol: str) -> dict:
        """Returns {commission_bps, spread_bps, slippage_bps}."""

    def rsm(self, symbol: str, df: pd.DataFrame, benchmark_df: pd.DataFrame) -> float:
        """Relative strength vs benchmark (shared)."""
```

### 5.2 Signal

```python
@dataclass
class Signal:
    symbol:    str
    market:    str
    strategy:  str
    direction: str          # "long" | "short"
    entry:     float
    entry_type: str         # "market_close" | "pending_stop" | "pending_limit"
    sl:        float
    tp1:       float
    tp2:       float
    tp3:       float | None
    atr:       float
    rr:        float        # reward/risk at TP1 (cost-adjusted)
    score:     float        # composite 0–100
    meta:      dict
    # RM params (optimised per strategy)
    sl_atr_mult:           float = 2.0
    tp1_atr_mult:          float = 2.0
    tp2_atr_mult:          float = 3.0
    risk_pct:              float = 0.005
    # Hard exit params
    max_bars:              int   = 10
    trail_atr_mult:        float = 2.0
    be_trigger_atr_mult:   float = 1.0
    exit_policies:         list[str] = ["hard_exit"]
    # Runtime state (updated by trade engine each bar)
    bars_held:             int   = 0
    highest_close:         float = 0.0
    sl_current:            float = 0.0
    generated_at:          date | None = None
```

### 5.3 Strategy

```python
class Strategy(ABC):
    id: str
    default_params: dict

    @abstractmethod
    def scan(self, df: pd.DataFrame, params: dict) -> list[Signal]:
        """Check last bar of look-ahead-guarded df. Return signal if qualifies."""

    @abstractmethod
    def param_space(self) -> dict:
        """Grid for optimizer — strategy params + RM params combined."""
```

### 5.4 StrategyRegistry

Decorator-based auto-discovery. New strategy = add `@StrategyRegistry.register` decorator. Zero pipeline changes.

```python
class StrategyRegistry:
    @classmethod
    def register(cls, strategy_cls): ...   # decorator

    @classmethod
    def get(cls, strategy_id: str): ...

    @classmethod
    def for_market(cls, market_id: str) -> dict[str, Strategy]: ...
```

### 5.5 ExitPolicy

New exit logic = new class. Trade engine iterates policies — never changes.

```python
class ExitPolicy(ABC):
    id: str

    @abstractmethod
    def check(self, position: Position, bar: dict, params: dict) -> ExitSignal | None:
        """Return ExitSignal to exit, None to hold."""

# Implementations
class HardExitPolicy(ExitPolicy):  # time stop + trail + breakeven (default)
    id = "hard_exit"
```

### 5.6 RiskPolicy

New sizing logic = new class. No pipeline changes required.

```python
class RiskPolicy(ABC):
    id: str

    @abstractmethod
    def size(self, capital, signal, params, ledger) -> int: ...

    @abstractmethod
    def approve(self, signal, capital, current_heat, params) -> bool: ...

# Implementations
class ATRFixedFractional(RiskPolicy):  # ATR-based fixed fractional (default)
    id = "atr_fixed_fractional"
```

### 5.7 PortfolioLedger

```python
class PortfolioLedger:
    """Shared source of truth for open state across all markets."""

    def open_positions(self) -> list[Position]: ...
    def current_heat(self, market: str | None) -> float: ...
    def correlation_matrix(self) -> pd.DataFrame: ...
    def register_fill(self, position: Position) -> None: ...
    def register_exit(self, position: Position, exit: ExitSignal, date: date) -> None: ...
    def pnl_summary(self) -> dict: ...
```

### 5.8 RiskEngine (correlation-aware heat cap)

```python
def apply_heat_limit(
    signals, current_heat, correlation_matrix=None,
    max_heat_pct=GLOBAL_HEAT_CAP, corr_penalty=0.5
) -> list[Signal]:
    """
    Heat cap with correlation penalty.
    Two positions with correlation > 0.7 contribute (1 + corr_penalty) × raw_risk each.
    """
```

---

## 6. Money management per market

| Market | Risk per trade | Max heat | Lot size | Min position | Notes |
|---|---|---|---|---|---|
| US | 0.5% | 6% | 1 share | $2,000 | Alpaca supports fractional |
| AU | 0.5% | 6% | 1 share | $2,000 | Round lots preferred |
| TH | 0.5% | 6% | 100 shares | ฿20,000 | Board lots mandatory |
| Crypto | 0.3% | 4% | 0.0001 BTC | $500 | Tighter heat (higher vol) |
| Commodity | 0.5% | 4% | 1 contract | varies | Contract value varies |

All risk percentages are **per trade**, not per market. Max heat is per market. The global ledger enforces a **cross-market heat cap of 10%** on top of per-market limits.

Default RM params above are starting points only. Walk-forward will find per-strategy optimal values within allowed ranges.

---

## 7. Project structure

```
alpha-engine/
├── main.py                    ← CLI entry (per-market runners)
├── config.py                  ← Global defaults + market configs
├── requirements.txt
│
├── markets/                   ← One adapter per market
│   ├── base.py                ← Abstract MarketAdapter
│   ├── us.py
│   ├── au.py
│   ├── th.py                  ← Primary (port from breakout-signal)
│   ├── crypto.py
│   └── commodity.py
│
├── strategies/
│   ├── base.py                ← Strategy ABC
│   ├── pivot_breakout.py
│   ├── pullback_buy.py
│   ├── reversal.py
│   ├── bb_squeeze.py
│   ├── ma_cross.py
│   └── narrow_range.py
│
├── core/
│   ├── signal.py              ← Signal, Position, ExitSignal dataclasses
│   ├── registry.py            ← StrategyRegistry (auto-discover via decorator)
│   ├── exit_policy.py         ← ExitPolicy ABC + HardExitPolicy
│   ├── risk_policy.py         ← RiskPolicy ABC + ATRFixedFractional
│   ├── indicators.py          ← ATR, RSI, BB, KC, EMA, SMA, RVol, ADX helpers
│   ├── regime.py              ← Regime filter (ADX, trend, bull check)
│   ├── universe.py            ← Point-in-time universe builder
│   ├── guard.py               ← Look-ahead guard
│   ├── tx_cost.py             ← Transaction cost model
│   ├── risk.py                ← Correlation-aware heat cap
│   ├── ledger.py              ← Portfolio ledger
│   ├── ranker.py              ← Signal composite scoring
│   ├── order_router.py        ← Market vs pending order handling
│   └── paper_trade.py         ← Paper trade simulator
│
├── validation/
│   ├── backtest.py            ← Bar-by-bar backtest runner
│   ├── optimizer.py           ← Walk-forward grid search
│   ├── consistency.py         ← 2yr vs 1yr drift check
│   └── paper_gate.py          ← Paper trading performance gate
│
├── db/
│   ├── schema.sql             ← Postgres schema
│   └── models.py              ← SQLAlchemy models
│
├── scripts/                   ← One per market — entry point for cron
│   ├── run_us.py
│   ├── run_au.py
│   ├── run_th.py
│   ├── run_crypto.py
│   └── run_commodity.py
│
├── app/                       ← FastAPI backend for dashboard
├── frontend/                  ← Alpine.js + Tailwind dashboard
├── workers/                   ← Celery tasks, scheduler
└── tests/
```

---

## 8. Tech stack

- **Data**: yfinance, ccxt, pandas-ta, Polygon, SETTRADE OpenAPI
- **Optimizer**: scikit-learn (ParameterGrid), joblib (parallel)
- **DB**: Postgres (Railway primary, SQLite for local dev)
- **Backend**: FastAPI
- **Scheduler**: Celery + Redis (replaces APScheduler — needs job persistence)
- **Frontend**: Alpine.js + Tailwind (extend current project)
- **Notifications**: LINE (signals), Discord (ops/errors)

---

## 9. Build order

Priority = known-working baseline first, then expand.

1. **Core contracts** — `core/signal.py`, `core/registry.py`, `core/exit_policy.py`, `core/risk_policy.py`, `strategies/base.py`, `markets/base.py`
2. **Core infrastructure** — indicators, regime, guard, tx_cost, risk, ledger, ranker, order_router
3. **TH + pivot breakout** — port existing `breakout-signal` to new architecture. Validate signals match existing output exactly.
4. **Walk-forward + consistency** — `validation/optimizer.py`, `validation/consistency.py` on TH+pivot as ground truth
5. **Paper trade + gate** — paper trading infrastructure and performance gate
6. **Shared DB + dashboard shell** — schema, write path from TH script, minimal read-only dashboard
7. **US market** — `markets/us.py`, port pivot breakout, run Phase 1+2
8. **Additional strategies** — one at a time, each with full Phase 1+2 validation
9. **Crypto market** — `markets/crypto.py` (fastest to add — good data, 24/7)
10. **AU + Commodity** — slowest due to data quality (AU) and roll handling (Commodity)
11. **Global ledger + cross-market analysis** — activate once ≥ 2 markets are live
12. **Full dashboard** — multi-market tabs, portfolio heat, live PnL

---

## 10. Remaining gaps (review)

These are the issues the design still does not fully solve. Ordered by severity.

### 10.1 Ongoing strategy decay monitoring

The consistency check runs upfront. But a live strategy can degrade silently over 6–12 months. Need a **rolling monitor** that runs the same 2yr-vs-1yr check on live-traded strategies weekly or monthly, and auto-flags for retirement if the recent performance drifts below the threshold.

### 10.2 Global kill switch / circuit breaker

If five correlated momentum strategies across five markets all get caught in a global risk-off event on the same day, per-market heat caps don't save you. Need a **global drawdown circuit breaker** that pauses all new entries if the portfolio-wide drawdown in any rolling 5-day window exceeds N%. Resume requires manual approval.

### 10.3 Data quality monitoring

The pipeline assumes incoming OHLCV is valid. In reality: missing bars, stale quotes, bad ticks, corporate-action-adjusted prices that change retroactively. Need a **data quality gate** that runs before indicator compute: check for gaps, verify against secondary source for large moves, halt scan if quality score is below threshold.

### 10.4 Time zone and scheduling complexity

5 markets across 3+ time zones. SET closes at 16:30 ICT, NYSE at 16:00 EST, ASX at 16:00 AEST, crypto never closes. The per-market scripts need a **robust scheduler** (Celery + Redis, not APScheduler — APScheduler has no job persistence and silently drops jobs on crash). Also need timezone-aware `as_of` timestamps throughout, not naive datetimes.

### 10.5 Corporate actions (equities)

Splits, dividends, mergers, and index reconstitutions affect historical prices. yfinance handles adjusted close, but if you rely on unadjusted prices anywhere (e.g. volume in raw shares), splits silently break indicator calculations. Need explicit **corporate action handling** per equity market, and a decision on whether to use adjusted or raw prices consistently.

### 10.6 Broker reconciliation

The portfolio ledger tracks what *should* have happened. The broker knows what *actually* happened. Fills at different prices, partial fills, rejected orders — these happen. Need a **reconciliation job** that runs after each session: compare ledger state to broker state, log discrepancies, alert on anything material. Without this, the ledger will drift from reality over time.

### 10.7 Observability / logging

When a strategy fails to signal on a day it should have, you need to know why. Need structured logging at each pipeline stage (data load, indicator compute, strategy scan, rank, risk, route), persisted long enough to debug issues 2–4 weeks later. A minimal but consistent schema: timestamp, market, stage, outcome, details.

### 10.8 Multi-objective score weights are still hyperparameters

The composite score `0.40 × Calmar + 0.30 × Sharpe + 0.20 × PF + 0.10 × win_rate` has four weights that were chosen by hand. Different weights produce different "best" params. Should be **documented as a known source of variance** and tested with 2–3 alternate weightings to ensure param stability.

### 10.9 Backtest reproducibility

Walk-forward optimiser uses random grid sampling in some configurations. Need explicit **seed control** and **data snapshots** so a backtest run today produces identical results in 6 months. Without this, "re-optimise" becomes a non-deterministic operation and you can't diagnose regressions.

### 10.10 Short signals

Entirely absent from current design. All strategies assume long-only. Short entries require: borrow cost modelling (non-trivial for TH and some AU names), short-available filter in universe builder, and inversion of every strategy's logic. Decision: either commit to short support in v1 (adds meaningful work) or explicitly scope it out until v2.

### 10.11 Intraday timeframes

Current design is daily-bar only. If you ever want 15m or 1h strategies, the entire scheduling, data volume, and regime filtering assumptions change. **Explicitly scope this out of v1** or plan for it now — retrofitting is expensive.

---

## 11. Open questions

- [ ] Short signals in v1 or deferred to v2?
- [ ] Crypto: spot only, or include futures/perps with funding rates?
- [ ] Commodity: individual futures contracts, or ETF proxies (GLD, USO, SLV)?
- [ ] Cross-market heat cap enforcement: hard stop, or soft warning?
- [ ] Paper trading minimum period: 3 months / 30 trades, or 6 months / 50 trades?
- [ ] Strategy retirement: auto-kill on consistency breach, or require manual review?
- [ ] Backtest window: fixed 5 years, or per-market based on data availability?
- [ ] Dashboard access: personal only, or shared multi-user?

---

## 12. Appendix — What this adds vs breakout-signal

| breakout-signal | AlphaEngine |
|---|---|
| 1 strategy (pivot break) | 6 strategies, validated per market |
| Thailand only | 5 markets, concurrent scans |
| Grid search on full history | Walk-forward + 2yr/1yr consistency check |
| RVOL + RSM only | 6+ params, multi-objective scoring with tx costs |
| Fixed exit logic | Per-strategy exits: time stop + trail + breakeven, all optimised |
| No RM optimisation | ATR-based RM params in walk-forward (sl/tp/risk_pct discovered per strategy) |
| No paper validation | Formal 3-phase validation gate |
| No portfolio state | Shared ledger, correlation-aware risk |
| Single-timezone scheduler | Multi-timezone, per-market schedulers |
| No survivorship protection | Point-in-time universe builder |
| Hardcoded exit logic | ExitPolicy ABC — new exit type = new class, no pipeline changes |
| Hardcoded RM | RiskPolicy ABC — new sizing logic = new class, no pipeline changes |
| No strategy discovery | StrategyRegistry — new strategy = decorator, auto-registered |
