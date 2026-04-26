import pandas as pd
import numpy as np


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df["close"].ewm(span=period, adjust=False).mean()


def sma(df: pd.DataFrame, period: int) -> pd.Series:
    return df["close"].rolling(period).mean()


def bollinger_bands(
    df: pd.DataFrame, period: int = 20, std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(df, period)
    sigma = df["close"].rolling(period).std()
    return mid + std * sigma, mid, mid - std * sigma


def keltner_channel(
    df: pd.DataFrame, period: int = 20, mult: float = 1.5
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = ema(df, period)
    _atr = atr(df, period)
    return mid + mult * _atr, mid, mid - mult * _atr


def rvol(df: pd.DataFrame, period: int = 20) -> pd.Series:
    avg = df["volume"].rolling(period).mean()
    return df["volume"] / avg.replace(0, np.nan)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0)
    _atr = atr(df, period)
    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / _atr
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / _atr
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return dx.ewm(span=period, adjust=False).mean()


def momentum_histogram(df: pd.DataFrame, period: int = 12) -> pd.Series:
    fast = ema(df, period)
    slow = ema(df, period * 2)
    signal_line = (fast - slow).ewm(span=9, adjust=False).mean()
    return (fast - slow) - signal_line


def candle_body_pct(df: pd.DataFrame) -> pd.Series:
    total = (df["high"] - df["low"]).replace(0, np.nan)
    return (df["close"] - df["open"]).abs() / total


def close_position_in_range(df: pd.DataFrame) -> pd.Series:
    """Where close sits within high-low range, 0=bottom 1=top."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return (df["close"] - df["low"]) / rng
