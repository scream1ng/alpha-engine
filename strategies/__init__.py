# Import all strategies so @StrategyRegistry.register decorators fire on import.
from strategies import (  # noqa: F401
    pivot_breakout,
    trendline_breakout,
    pullback_buy,
    reversal,
    bb_squeeze,
    ma_cross,
    narrow_range,
)
