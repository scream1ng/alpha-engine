"""
NR7 (Narrow Range 7) chart example.
Usage: python -m scripts.chart_nr7 [--symbol DELTA.BK] [--days 120]
"""
from __future__ import annotations
import argparse
from datetime import date, timedelta

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import yfinance as yf
import pandas as pd


def find_nr7(df: pd.DataFrame, period: int = 7) -> pd.Series:
    ranges = df["High"] - df["Low"]
    is_nr = pd.Series(False, index=df.index)
    for i in range(period - 1, len(df)):
        window = ranges.iloc[i - period + 1 : i + 1]
        is_nr.iloc[i] = ranges.iloc[i] == window.min()
    return is_nr


def plot_nr7(symbol: str, days: int = 120, nr_period: int = 7) -> None:
    end = date.today()
    start = end - timedelta(days=days)

    df = yf.download(symbol, start=str(start), end=str(end), auto_adjust=True, progress=False)
    if df.empty:
        print(f"No data for {symbol}")
        return

    # Flatten MultiIndex columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    is_nr = find_nr7(df, nr_period)

    fig, (ax_main, ax_vol) = plt.subplots(
        2, 1, figsize=(16, 9),
        gridspec_kw={"height_ratios": [3, 1]},
        sharex=True,
    )
    fig.patch.set_facecolor("#0d1117")
    for ax in (ax_main, ax_vol):
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e")
        ax.spines["bottom"].set_color("#30363d")
        ax.spines["top"].set_visible(False)
        ax.spines["left"].set_color("#30363d")
        ax.spines["right"].set_visible(False)

    x = np.arange(len(df))
    dates = df.index

    # Draw candles
    for i, (idx, row) in enumerate(df.iterrows()):
        o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
        bullish = c >= o
        nr = is_nr.iloc[i]

        body_color = "#3fb950" if bullish else "#f85149"
        wick_color = "#3fb950" if bullish else "#f85149"

        if nr:
            body_color = "#f0e68c"  # yellow highlight for NR7 bar
            wick_color = "#f0e68c"
            # Draw highlight box behind NR7 bar
            ax_main.axvspan(i - 0.5, i + 0.5, alpha=0.12, color="#f0e68c", zorder=0)

        # Wick
        ax_main.plot([i, i], [l, h], color=wick_color, linewidth=1, zorder=2)
        # Body
        body_h = abs(c - o) if abs(c - o) > 0 else (h - l) * 0.01
        body_b = min(o, c)
        rect = mpatches.FancyBboxPatch(
            (i - 0.35, body_b), 0.7, body_h,
            boxstyle="square,pad=0",
            facecolor=body_color, edgecolor="none", zorder=3,
        )
        ax_main.add_patch(rect)

        # NR7: draw pending stop entry line above bar
        if nr:
            tick = row["Close"] * 0.001
            entry = h + tick
            ax_main.hlines(
                entry, i, min(i + 4, len(df) - 1),
                colors="#58a6ff", linewidths=1.2, linestyles="--", zorder=4,
            )
            ax_main.annotate(
                "NR7\nstop", xy=(i, entry),
                xytext=(i + 0.5, entry + (h - l) * 0.5),
                fontsize=7, color="#58a6ff",
                arrowprops=dict(arrowstyle="-", color="#58a6ff", lw=0.8),
            )

    # Volume bars
    for i, (idx, row) in enumerate(df.iterrows()):
        bullish = row["Close"] >= row["Open"]
        color = "#3fb950" if bullish else "#f85149"
        if is_nr.iloc[i]:
            color = "#f0e68c"
        ax_vol.bar(i, row["Volume"], color=color, alpha=0.7, width=0.8)

    # X axis labels — show every ~20 bars
    step = max(len(df) // 10, 1)
    ax_vol.set_xticks(x[::step])
    ax_vol.set_xticklabels(
        [d.strftime("%d %b") for d in dates[::step]],
        rotation=30, ha="right", fontsize=8, color="#8b949e",
    )
    ax_main.set_xlim(-1, len(df))

    # Formatting
    ax_main.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.2f}"))
    ax_main.grid(axis="y", color="#21262d", linewidth=0.5)
    ax_vol.grid(axis="y", color="#21262d", linewidth=0.5)

    nr_count = is_nr.sum()
    ax_main.set_title(
        f"{symbol}  —  NR{nr_period} Strategy  ({start} → {end})    "
        f"{nr_count} NR{nr_period} bars found",
        color="#e6edf3", fontsize=13, pad=12, loc="left",
    )
    ax_main.set_ylabel("Price", color="#8b949e", fontsize=9)
    ax_vol.set_ylabel("Volume", color="#8b949e", fontsize=9)

    # Legend
    legend_elements = [
        mpatches.Patch(facecolor="#3fb950", label="Bullish candle"),
        mpatches.Patch(facecolor="#f85149", label="Bearish candle"),
        mpatches.Patch(facecolor="#f0e68c", label=f"NR{nr_period} bar (narrowest range)"),
        plt.Line2D([0], [0], color="#58a6ff", linestyle="--", label="Pending stop-buy entry"),
    ]
    ax_main.legend(
        handles=legend_elements, loc="upper left",
        facecolor="#161b22", edgecolor="#30363d",
        labelcolor="#e6edf3", fontsize=8,
    )

    plt.tight_layout(h_pad=0.5)
    out = f"nr7_{symbol.replace('.', '_')}.png"
    plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"Saved: {out}")
    plt.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="DELTA.BK")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--period", type=int, default=7)
    args = parser.parse_args()
    plot_nr7(args.symbol, args.days, args.period)
