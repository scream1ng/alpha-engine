import pandas as pd
from core.indicators import adx, sma


def is_trending(df: pd.DataFrame, threshold: float = 20.0, period: int = 14) -> bool:
    if len(df) < period * 2:
        return False
    return float(adx(df, period).iloc[-1]) >= threshold


def is_bull_regime(df: pd.DataFrame) -> bool:
    if len(df) < 200:
        return False
    return float(df["close"].iloc[-1]) > float(sma(df, 200).iloc[-1])


def regime_ok(
    df: pd.DataFrame,
    require_trend: bool = True,
    require_bull: bool = False,
) -> bool:
    if require_trend and not is_trending(df):
        return False
    if require_bull and not is_bull_regime(df):
        return False
    return True
