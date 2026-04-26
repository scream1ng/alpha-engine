from __future__ import annotations
from datetime import date
import numpy as np
import pandas as pd
from strategies.base import Strategy
from core.signal import Position
from core.exit_policy import get_exit_policies
from core.order_router import check_pending_triggered, is_pending_order

MIN_BARS_WARMUP = 50


def _precompute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add param-independent indicator columns once — slices inherit them."""
    from core.indicators import (
        atr, rvol, rsi, ema, candle_body_pct, close_position_in_range,
        momentum_histogram, rsm,
    )
    df = df.copy()
    df["_atr"] = atr(df)
    df["_rvol"] = rvol(df)
    df["_rsi"] = rsi(df)
    df["_body_pct"] = candle_body_pct(df)
    df["_close_pos"] = close_position_in_range(df)
    df["_momentum"] = momentum_histogram(df)
    df["_ema5"] = ema(df, 5)
    df["_ema10"] = ema(df, 10)
    df["_rsm"] = rsm(df)  # needs _bm_close col; NaN if benchmark not attached
    return df


def run_backtest(
    df: pd.DataFrame,
    strategy: Strategy,
    params: dict,
    initial_capital: float = 100_000,
) -> dict:
    """
    Bar-by-bar backtest. df must have attrs['symbol'] and attrs['market'].
    Returns metrics dict including raw trades list.
    """
    saved_attrs = df.attrs
    df = _precompute_indicators(df)
    df.attrs = saved_attrs

    trades: list[dict] = []
    open_positions: list[Position] = []
    pending_signals = []
    capital = initial_capital

    for i in range(MIN_BARS_WARMUP, len(df)):
        bar_df = df.iloc[: i + 1].copy()
        bar_df.attrs = df.attrs
        bar = df.iloc[i]
        bar_date: date = bar.name.date() if hasattr(bar.name, "date") else bar.name
        bar_dict = {
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "ema5":  float(bar["_ema5"])  if "_ema5"  in df.columns else None,
            "ema10": float(bar["_ema10"]) if "_ema10" in df.columns else None,
        }

        # Trigger pending orders
        still_pending = []
        for sig in pending_signals:
            if check_pending_triggered(sig, bar_dict):
                size = _calc_size(capital, sig, params)
                if size > 0:
                    pos = Position(
                        signal=sig,
                        entry_price=sig.entry,
                        entry_date=bar_date,
                        size=size,
                    )
                    open_positions.append(pos)
            else:
                still_pending.append(sig)
        pending_signals = still_pending

        # Process exits
        still_open: list[Position] = []
        for pos in open_positions:
            pos.bars_held += 1
            policies = get_exit_policies(pos.signal.exit_policies)
            exited = False
            for policy in policies:
                exit_sig = policy.check(pos, bar_dict, params)
                if exit_sig:
                    size = (
                        int(pos.size * exit_sig.partial_pct) if exit_sig.partial else pos.size
                    )
                    if pos.signal.direction == "long":
                        pnl = (exit_sig.price - pos.entry_price) * size
                    else:
                        pnl = (pos.entry_price - exit_sig.price) * size
                    capital += pnl
                    trades.append(
                        {
                            "symbol": pos.signal.symbol,
                            "strategy": pos.signal.strategy,
                            "direction": pos.signal.direction,
                            "entry_date": pos.entry_date,
                            "exit_date": bar_date,
                            "entry_price": pos.entry_price,
                            "exit_price": exit_sig.price,
                            "exit_reason": exit_sig.reason,
                            "size": size,
                            "pnl": pnl,
                            "bars_held": pos.bars_held,
                        }
                    )
                    if exit_sig.partial:
                        pos.size -= size
                        still_open.append(pos)
                    exited = not exit_sig.partial
                    break
            if not exited:
                still_open.append(pos)
        open_positions = still_open

        # Generate new signals (last-bar only)
        open_syms = {p.signal.symbol for p in open_positions}
        signals = strategy.scan(bar_df, params)
        for sig in signals:
            if sig.symbol in open_syms:
                continue
            if is_pending_order(sig):
                pending_signals.append(sig)
            else:
                size = _calc_size(capital, sig, params)
                if size > 0:
                    pos = Position(
                        signal=sig,
                        entry_price=sig.entry,
                        entry_date=bar_date,
                        size=size,
                    )
                    open_positions.append(pos)

    n_bars = len(df) - MIN_BARS_WARMUP
    return compute_metrics(trades, initial_capital, n_bars=n_bars)


def _calc_size(capital: float, sig, params: dict) -> int:
    from config import MARKET_CONFIGS
    cfg = MARKET_CONFIGS.get(sig.market)
    lot_size = cfg.lot_size if cfg else 1
    risk_pct = params.get("risk_pct", sig.risk_pct)
    sl_dist = abs(sig.entry - sig.sl)
    if sl_dist == 0:
        return 0
    raw = (capital * risk_pct) / sl_dist
    return int(raw // lot_size) * lot_size


def compute_metrics(trades: list[dict], initial_capital: float, n_bars: int = 252) -> dict:
    if not trades:
        return {
            "sharpe": 0.0,
            "calmar": 0.0,
            "annual_return": 0.0,
            "profit_factor": 0.0,
            "win_rate": 0.0,
            "trade_count": 0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_drawdown": 0.0,
            "total_pnl": 0.0,
            "trades": [],
        }

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls)
    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 1e-9
    profit_factor = min(gross_profit / gross_loss, 100.0)  # cap at 100 — no-loss runs inflate unrealistically

    equity = initial_capital
    peak = initial_capital
    max_dd = 0.0
    daily_pnl: dict = {}
    for t in trades:
        exit_date = t["exit_date"]
        daily_pnl[exit_date] = daily_pnl.get(exit_date, 0.0) + t["pnl"]
        equity += t["pnl"]
        peak = max(peak, equity)
        dd = (peak - equity) / peak
        max_dd = max(max_dd, dd)

    # Sharpe on daily returns (zeros for non-trade days) — avoids per-trade sqrt(252) distortion
    equity_series = initial_capital
    daily_returns: list[float] = []
    for d in sorted(daily_pnl):
        pnl = daily_pnl[d]
        ret = pnl / equity_series if equity_series > 0 else 0.0
        daily_returns.append(ret)
        equity_series += pnl

    # Fill non-trade days with 0 for proper annualization denominator
    n_trading_days = max(n_bars, 1)
    n_zero_days = max(n_trading_days - len(daily_returns), 0)
    all_returns = np.array(daily_returns + [0.0] * n_zero_days)

    sharpe = float(
        all_returns.mean() / all_returns.std() * np.sqrt(252)
        if all_returns.std() > 0
        else 0.0
    )

    # Calmar: annualize raw return by actual bars in sample
    annualization = 252 / n_trading_days
    annual_return = (sum(pnls) / initial_capital) * annualization
    calmar = annual_return / max_dd if max_dd > 0 else annual_return

    # avg win/loss as % of position value (entry_price × size)
    def _pct(t: dict) -> float:
        pos_val = t.get("entry_price", 0) * t.get("size", 0)
        return t["pnl"] / pos_val if pos_val > 0 else 0.0

    pct_pnls = [_pct(t) for t in trades]
    wins_pct   = [p for p in pct_pnls if p > 0]
    losses_pct = [p for p in pct_pnls if p < 0]
    avg_win  = float(sum(wins_pct)   / len(wins_pct))   if wins_pct   else 0.0
    avg_loss = float(sum(losses_pct) / len(losses_pct)) if losses_pct else 0.0

    return {
        "sharpe": sharpe,
        "calmar": float(calmar),
        "annual_return": float(annual_return),
        "profit_factor": float(profit_factor),
        "win_rate": float(win_rate),
        "trade_count": len(trades),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_drawdown": float(max_dd),
        "total_pnl": float(sum(pnls)),
        "trades": trades,
    }
