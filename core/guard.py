from datetime import date
import pandas as pd


def apply_lookahead_guard(df: pd.DataFrame, as_of: date) -> pd.DataFrame:
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df[df.index.date <= as_of]


def validate_no_lookahead(df: pd.DataFrame, signal_date: date) -> None:
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    future = df[df.index.date > signal_date]
    if len(future) > 0:
        raise ValueError(
            f"Look-ahead bias detected: {len(future)} bars after {signal_date}"
        )
