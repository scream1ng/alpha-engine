from dataclasses import dataclass, field

@dataclass
class MarketConfig:
    market_id: str
    benchmark: str
    currency: str
    min_adv: float
    lot_size: int
    risk_per_trade: float
    max_heat_pct: float
    min_position_value: float
    timezone: str
    enabled_strategies: list[str] = field(default_factory=list)

MARKET_CONFIGS: dict[str, MarketConfig] = {
    "th": MarketConfig(
        market_id="th",
        benchmark="^SET.BK",
        currency="THB",
        min_adv=500_000,
        lot_size=100,
        risk_per_trade=0.005,
        max_heat_pct=0.06,
        min_position_value=20_000,
        timezone="Asia/Bangkok",
        enabled_strategies=[
            "pivot_breakout", "trendline_breakout", "pullback_buy", "reversal",
            "bb_squeeze", "ma_cross", "narrow_range",
        ],
    ),
    "us": MarketConfig(
        market_id="us",
        benchmark="SPY",
        currency="USD",
        min_adv=5_000_000,
        lot_size=1,
        risk_per_trade=0.005,
        max_heat_pct=0.06,
        min_position_value=2_000,
        timezone="America/New_York",
        enabled_strategies=[
            "pivot_breakout", "trendline_breakout", "pullback_buy", "reversal",
            "bb_squeeze", "ma_cross", "narrow_range",
        ],
    ),
    "au": MarketConfig(
        market_id="au",
        benchmark="^AXJO",
        currency="AUD",
        min_adv=500_000,
        lot_size=1,
        risk_per_trade=0.005,
        max_heat_pct=0.06,
        min_position_value=2_000,
        timezone="Australia/Sydney",
        enabled_strategies=[
            "pivot_breakout", "trendline_breakout", "pullback_buy", "reversal",
            "bb_squeeze", "ma_cross", "narrow_range",
        ],
    ),
    "crypto": MarketConfig(
        market_id="crypto",
        benchmark="BTC-USD",
        currency="USDT",
        min_adv=0,
        lot_size=1,
        risk_per_trade=0.003,
        max_heat_pct=0.04,
        min_position_value=500,
        timezone="UTC",
        enabled_strategies=[
            "pivot_breakout", "trendline_breakout", "pullback_buy", "reversal",
            "bb_squeeze", "ma_cross", "narrow_range",
        ],
    ),
    "commodity": MarketConfig(
        market_id="commodity",
        benchmark="GC=F",
        currency="USD",
        min_adv=0,
        lot_size=1,
        risk_per_trade=0.005,
        max_heat_pct=0.04,
        min_position_value=0,
        timezone="America/New_York",
        enabled_strategies=[
            "pivot_breakout", "trendline_breakout", "pullback_buy", "reversal",
            "bb_squeeze", "ma_cross", "narrow_range",
        ],
    ),
}

GLOBAL_HEAT_CAP = 0.10
CORR_PENALTY = 0.5
CORR_THRESHOLD = 0.7

SCORING_WEIGHTS = {
    "calmar": 0.40,
    "sharpe": 0.30,
    "profit_factor": 0.20,
    "win_rate": 0.10,
}

OPTIMIZER_OBJECTIVE = "annual_return"

WALKFORWARD_TRAIN_MONTHS = 18
WALKFORWARD_TEST_MONTHS = 6
CONSISTENCY_THRESHOLD = 0.50

PAPER_MIN_TRADES = 30
PAPER_MIN_MONTHS = 3

SELECTION_GATE = {
    "min_annual_return": 0.03,   # bootstrap gate for 6m OOS samples
    "min_sharpe": 0.0,
    "min_calmar": 0.0,
    "min_profit_factor": 1.05,
    "min_win_rate": 0.30,
    "min_trades": 3,
}
