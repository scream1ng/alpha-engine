import pandas as pd
from core.indicators import adx, sma

REGIMES = ["uptrend", "choppy", "downtrend"]


def label_regime(bm_close: pd.Series) -> pd.Series:
    """Bar-by-bar regime label using SMA50/SMA200 on benchmark close.

    uptrend:   price > SMA50 > SMA200
    downtrend: price < SMA50 < SMA200
    choppy:    everything else (incl. bars with insufficient SMA history)
    """
    sma50  = bm_close.rolling(50,  min_periods=50).mean()
    sma200 = bm_close.rolling(200, min_periods=200).mean()
    labels = pd.Series("choppy", index=bm_close.index, dtype=object)
    labels[(bm_close > sma50) & (sma50 > sma200)] = "uptrend"
    labels[(bm_close < sma50) & (sma50 < sma200)] = "downtrend"
    return labels


def regime_windows(regime_series: pd.Series, window_bars: int = 63) -> list[tuple]:
    """Split regime series into fixed-size windows, label each by dominant regime.

    Returns list of (start_date, end_date_exclusive, regime_label).
    """
    from datetime import timedelta
    windows = []
    n = len(regime_series)
    for i in range(0, n, window_bars):
        chunk = regime_series.iloc[i : i + window_bars]
        if len(chunk) < 30:
            continue
        dominant = chunk.value_counts().idxmax()
        start = chunk.index[0].date()
        end   = chunk.index[-1].date() + timedelta(days=1)
        windows.append((start, end, dominant))
    return windows


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
