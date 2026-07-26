#!/usr/bin/env python3
"""Build every report figure from the persisted backtest outputs.

Each figure answers one question a reader of `results/report.md` will ask, in
the order the report asks it: what are the rules, what does one trade look
like, what did the account do, does the edge survive its controls, and how
fragile is it.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import FIGURES_DIR, PARQUET_DIR  # noqa: E402

# Validated categorical palette: slots are assigned in this fixed order and
# never cycled. Contrast of the aqua and yellow slots against the surface is
# below 3:1, so every series that uses them is also directly labeled.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
AQUA = "#1baf7a"
YELLOW = "#eda100"
RED = "#d03b3b"
GREEN = "#0ca30c"
NEUTRAL_FILL = "#f0efec"

DIVERGING = LinearSegmentedColormap.from_list(
    "loss_gain", [RED, NEUTRAL_FILL, BLUE]
)

MODE_ORDER = [
    "full",
    "passive_limit",
    "positive_momentum_correction",
    "momentum_only",
]
MODE_COLORS = {
    "full": BLUE,
    "momentum_only": ORANGE,
    "positive_momentum_correction": AQUA,
    "passive_limit": YELLOW,
}
SHORT_LABELS = {
    "full": "Momentum + correction",
    "momentum_only": "Momentum, next open",
    "positive_momentum_correction": "Positive momentum",
    "passive_limit": "Passive limits",
}

def median_by_return(
    frame: pd.DataFrame, column: str = "gain", target: float | None = None
) -> list[int]:
    """Row positions ordered by distance from the median of `column`.

    Worked examples must not be chosen for their outcome. Ranking candidates by
    how close they sit to the median return means the illustration is typical
    of the run by construction, and the caller can walk the list until it finds
    one whose bars are available rather than reaching for a better trade.
    """
    if frame.empty:
        return []
    goal = float(frame[column].median()) if target is None else float(target)
    order = (frame[column] - goal).abs().sort_values(kind="stable")
    return [frame.index.get_loc(label) for label in order.index]


def use_style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "axes.edgecolor": AXIS,
            "axes.labelcolor": INK_SECONDARY,
            "axes.titlecolor": INK,
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.titlepad": 10,
            "axes.labelsize": 9.5,
            "axes.linewidth": 0.8,
            "axes.grid": True,
            "axes.axisbelow": True,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "grid.color": GRID,
            "grid.linewidth": 0.7,
            "font.family": "sans-serif",
            "font.size": 9.5,
            "legend.frameon": False,
            "legend.fontsize": 9,
            "legend.labelcolor": INK_SECONDARY,
            "lines.linewidth": 2.0,
            "text.color": INK,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "figure.dpi": 110,
        }
    )


def save(fig: plt.Figure, name: str) -> str:
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIGURES_DIR / name
    fig.savefig(path, dpi=170, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {path.relative_to(PROJECT)}", flush=True)
    return name


def head(ax: plt.Axes, title: str, caption: str, size: float = 12) -> None:
    """Bold title with one recessive caption line between it and the axes."""
    wrapped = "\n".join(textwrap.wrap(caption, width=118))
    ax.set_title(title, pad=30 + 13 * wrapped.count("\n"), fontsize=size)
    ax.text(
        0.0,
        1.014,
        wrapped,
        transform=ax.transAxes,
        fontsize=9.5,
        color=INK_SECONDARY,
        va="bottom",
        ha="left",
    )


def figure_head(fig: plt.Figure, title: str, caption: str, width: int = 128) -> None:
    """The same pairing for a multi-panel figure.

    The caption is wrapped: a long single line would otherwise stretch the
    saved figure well past the width of the panels underneath it.
    """
    wrapped = "\n".join(textwrap.wrap(caption, width=width))
    lines = wrapped.count("\n") + 1
    fig.suptitle(
        title,
        fontsize=13.5,
        fontweight="bold",
        color=INK,
        x=0.0,
        ha="left",
        y=1.055 + 0.033 * lines,
    )
    fig.text(
        0.0,
        1.028 + 0.033 * (lines - 1),
        wrapped,
        fontsize=9.5,
        color=INK_SECONDARY,
        ha="left",
        va="top",
    )


def spread_labels(values: list[float], gap: float) -> list[float]:
    """Nudge label positions apart, preserving order, inside [0, 1]."""
    placed = np.array(values, dtype=float)
    ordered = sorted(range(len(values)), key=lambda i: values[i])
    for position, index in enumerate(ordered):
        if position == 0:
            continue
        previous = placed[ordered[position - 1]]
        if placed[index] - previous < gap:
            placed[index] = previous + gap
    overflow = placed.max() - 1.0
    if overflow > 0:
        placed -= overflow
    return list(placed)


# --------------------------------------------------------------------------
# 1. The rules
# --------------------------------------------------------------------------


def stage_box(
    ax: plt.Axes,
    x: float,
    y: float,
    width: float,
    height: float,
    heading: str,
    body: str,
    accent: str,
) -> None:
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            width,
            height,
            boxstyle="round,pad=0.006,rounding_size=0.012",
            linewidth=1.2,
            edgecolor=accent,
            facecolor=SURFACE,
            zorder=2,
        )
    )
    ax.add_patch(
        FancyBboxPatch(
            (x, y + height - 0.052),
            width,
            0.052,
            boxstyle="square,pad=0",
            linewidth=0,
            facecolor=accent,
            alpha=0.14,
            zorder=3,
        )
    )
    ax.text(
        x + 0.012,
        y + height - 0.026,
        heading,
        fontsize=10,
        fontweight="bold",
        color=INK,
        va="center",
        ha="left",
        zorder=4,
    )
    ax.text(
        x + 0.012,
        y + height - 0.075,
        body,
        fontsize=8.6,
        color=INK_SECONDARY,
        va="top",
        ha="left",
        linespacing=1.5,
        zorder=4,
    )


def arrow(ax: plt.Axes, start: tuple[float, float], end: tuple[float, float]) -> None:
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=13,
            linewidth=1.2,
            color=MUTED,
            shrinkA=0,
            shrinkB=0,
            zorder=1,
        )
    )


RULE_TEXT = {
    "spot-daily": {
        "venue": "Binance Spot USDT pairs, cash only",
        "wait": "The order lives 3 days. It fills only if a\n"
        "daily low trades strictly through the limit;\n"
        "the fill price is the limit itself, never\n"
        "better. It is canceled early if momentum\n"
        "drops out of the signal set before the\n"
        "correction arrives.",
        "hold": "No stop, no profit target, no scale-in and\n"
        "no re-entry while the position is open.\n"
        "The holding period is the only exit rule.\n"
        "The fill moment inside the day is unknown,\n"
        "so the realized hold is uncertain by a day.",
        "sell": "Market sell at the target-day open less\n"
        "5 bps of modeled slippage. Costs are\n"
        "10 bps in and 10 bps out; the freed cash\n"
        "returns to the next day's budget.",
    },
    "futures-hourly": {
        "venue": "Binance USD-M perpetual futures, cash only, funding reported separately",
        "wait": "The order lives 3 days. It fills on the first\n"
        "hourly bar whose low trades strictly through\n"
        "the limit; the fill price is the limit itself,\n"
        "never better. It is canceled early if momentum\n"
        "drops out of the signal set before the\n"
        "correction arrives.",
        "hold": "No stop, no profit target, no scale-in and\n"
        "no re-entry while the position is open.\n"
        "The fill hour is observed, so the exit is\n"
        "scheduled exactly 5 x 24h after it rather\n"
        "than from the calendar.",
        "sell": "Market sell at the hourly open on the exit\n"
        "stamp less 5 bps of modeled slippage. Costs\n"
        "are 10 bps in and 10 bps out; the freed cash\n"
        "returns to the next day's budget.",
    },
}


def figure_strategy_rules(
    config: dict,
    summaries: pd.DataFrame,
    orders: pd.DataFrame,
    profile: str = "spot-daily",
) -> str:
    base = summaries.loc[summaries["mode"].eq("full")].iloc[0]
    counts = orders["status"].value_counts() if not orders.empty else pd.Series(dtype=int)
    # Orders still resting on the final date are counted at placement but never
    # reach a terminal status, so the two branches need not exhaust the total.
    total_orders = int(base["orders"])
    filled = int(counts.get("filled", base["fills"]))
    expired = int(counts.get("canceled_expired", 0))
    signal_lost = int(counts.get("canceled_signal", 0))
    unfilled = expired + signal_lost
    still_open = total_orders - filled - unfilled

    fig, ax = plt.subplots(figsize=(13.2, 6.0))
    ax.set_xlim(-0.014, 1.014)
    ax.axis("off")
    ax.grid(False)

    text = RULE_TEXT.get(profile, RULE_TEXT["spot-daily"])
    caption = "\n".join(
        textwrap.wrap(
            f"One pass per UTC daily close on {text['venue']}. "
            f"Frozen research parameters: {config['momentum_lookback']}-day "
            f"momentum, {config['correction_sigma']:.1f}σ limit, "
            f"{config['order_life_days']}-day order life, "
            f"{config['holding_days']}-day hold. The source post publishes "
            "none of them.",
            width=120,
        )
    )
    # The caption wraps to a different number of lines per venue, so the header
    # band grows upward instead of pushing into the first row of boxes.
    header = 0.065 * caption.count("\n")
    ax.set_ylim(-0.01, 1.02 + header)
    ax.text(
        0.0,
        1.02 + header,
        "How the momentum-correction strategy works",
        fontsize=15,
        fontweight="bold",
        color=INK,
        ha="left",
        va="top",
    )
    ax.text(
        0.0,
        0.955 + header,
        caption,
        fontsize=10,
        color=INK_SECONDARY,
        ha="left",
        va="top",
    )

    # The limit is a volatility distance, so its distance in percent is a
    # property of the sample rather than a parameter. Quote the realized range.
    discount = 1 - np.exp(-float(config["correction_sigma"]) * orders["sigma"])
    low_pct, median_pct, high_pct = (
        discount.quantile(0.10) * 100,
        discount.median() * 100,
        discount.quantile(0.90) * 100,
    )

    width = 0.30
    top_y = 0.60
    height = 0.30
    stage_box(
        ax,
        0.0,
        top_y,
        width,
        height,
        "1  Rank the universe",
        "Take the 50 USDT pairs with the largest\n"
        "30-day quote volume, point in time.\n"
        "Score each by 20-day return divided by\n"
        "its 20-day volatility. Keep coins whose\n"
        "momentum is positive and in the top\n"
        "quintile of that day's universe.",
        BLUE,
    )
    arrow(ax, (0.315, top_y + height / 2), (0.34, top_y + height / 2))
    stage_box(
        ax,
        0.35,
        top_y,
        width,
        height,
        "2  Rest a buy limit below price",
        "Limit = close × exp(−1.0 σ), rounded down\n"
        f"to the venue tick. In this run that sits\n"
        f"{low_pct:.0f} to {high_pct:.0f}% under the signal close, median\n"
        f"{median_pct:.1f}%. Cash is reserved when the order\n"
        "is placed: at most 10% of equity per coin,\n"
        "100% reserved plus invested in total.",
        BLUE,
    )
    arrow(ax, (0.665, top_y + height / 2), (0.69, top_y + height / 2))
    stage_box(
        ax,
        0.70,
        top_y,
        width,
        height,
        "3  Wait for the correction",
        text["wait"],
        BLUE,
    )

    # Outcome split under stage 3.
    split_y = 0.47
    ax.plot(
        [0.85, 0.85],
        [top_y - 0.005, split_y + 0.04],
        color=MUTED,
        linewidth=1.2,
        zorder=1,
    )
    ax.plot([0.30, 0.85], [split_y + 0.04, split_y + 0.04], color=MUTED, linewidth=1.2)
    ax.plot([0.85, 0.94], [split_y + 0.04, split_y + 0.04], color=MUTED, linewidth=1.2)
    arrow(ax, (0.30, split_y + 0.04), (0.30, split_y - 0.005))
    arrow(ax, (0.94, split_y + 0.04), (0.94, split_y - 0.005))

    ax.text(
        0.315,
        split_y + 0.012,
        f"filled   {filled:,} orders   ({filled / total_orders:.1%})",
        fontsize=9,
        color=GREEN,
        fontweight="bold",
        va="bottom",
        ha="left",
    )
    ax.text(
        0.925,
        split_y + 0.012,
        f"never filled   {unfilled:,}   ({unfilled / total_orders:.1%})",
        fontsize=9,
        color=RED,
        fontweight="bold",
        va="bottom",
        ha="right",
    )

    bottom_height = 0.29
    bottom_y = split_y - 0.005 - bottom_height
    stage_box(
        ax,
        0.0,
        bottom_y,
        width,
        bottom_height,
        "4  Hold exactly 5 days",
        text["hold"],
        AQUA,
    )
    arrow(ax, (0.315, bottom_y + bottom_height / 2), (0.34, bottom_y + bottom_height / 2))
    stage_box(
        ax,
        0.35,
        bottom_y,
        width,
        bottom_height,
        "5  Sell into the open",
        text["sell"],
        AQUA,
    )
    stage_box(
        ax,
        0.70,
        bottom_y,
        width,
        bottom_height,
        "Cancel and release the cash",
        f"{expired:,} orders expired after 3 days and\n"
        f"{signal_lost:,} were canceled when the coin\n"
        "left the signal set. The reserved cash is\n"
        "freed, and the move is missed entirely.",
        RED,
    )

    ax.text(
        0.0,
        0.0,
        f"Baseline run {config['start']} to {config['end']}: {total_orders:,} orders "
        f"placed, {filled:,} filled, {unfilled:,} canceled unfilled"
        + (f", {still_open} still resting at the sample end" if still_open else "")
        + f". {base['hit_rate']:.1%} of filled trades were profitable; "
        f"profit factor {base['profit_factor']:.2f}.",
        fontsize=9,
        color=INK_SECONDARY,
        ha="left",
        va="bottom",
    )
    return save(fig, "fig01_strategy_rules.png")


# --------------------------------------------------------------------------
# 2. One trade, twice
# --------------------------------------------------------------------------


def load_bars(symbol: str) -> pd.DataFrame | None:
    path = PARQUET_DIR / f"{symbol}.parquet"
    if not path.exists():
        return None
    bars = pd.read_parquet(path)
    bars["date"] = pd.to_datetime(bars["date"], utc=True)
    return bars.sort_values("date").reset_index(drop=True)


def draw_bars(ax: plt.Axes, window: pd.DataFrame) -> None:
    for _, row in window.iterrows():
        ax.plot(
            [row["date"], row["date"]],
            [row["low"], row["high"]],
            color=AXIS,
            linewidth=1.6,
            solid_capstyle="round",
            zorder=2,
        )
    ax.plot(
        window["date"],
        window["close"],
        color=INK_SECONDARY,
        linewidth=1.2,
        zorder=3,
    )


def price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.2f}"
    return f"{value:.6g}"


def anatomy_panel(
    ax: plt.Axes,
    bars: pd.DataFrame,
    order: pd.Series,
    trade: pd.Series | None,
    title: str,
    caption: str,
    offsets: dict[str, tuple[float, float, str]],
) -> None:
    def annotate(key: str, text: str, xy: tuple, color: str, flip: bool = False) -> None:
        dx, dy, align = offsets[key]
        # The exit sits above the signal on a winner and below it on a loser, so
        # its label has to move with it or the two annotations collide.
        if flip:
            dy = -dy - 24
        ax.annotate(
            text,
            xy=xy,
            xytext=(dx, dy),
            textcoords="offset points",
            fontsize=8.5,
            color=color,
            ha=align,
            arrowprops={
                "arrowstyle": "-",
                "color": MUTED if color == INK_SECONDARY else color,
                "linewidth": 0.9,
            },
        )

    signal_date = pd.Timestamp(order["signal_date"])
    active = pd.Timestamp(order["active_date"])
    expiry = pd.Timestamp(order["expiry_date"])
    last = pd.Timestamp(trade["target_exit_date"]) if trade is not None else expiry
    window = bars.loc[
        bars["date"].between(signal_date - pd.Timedelta(days=24), last + pd.Timedelta(days=3))
    ]
    lookback_start = signal_date - pd.Timedelta(days=19)
    signal_bar = bars.loc[bars["date"].eq(signal_date)].iloc[0]
    limit = float(order["limit"])

    ax.axvspan(
        lookback_start,
        signal_date,
        color=BLUE,
        alpha=0.07,
        zorder=0,
        linewidth=0,
    )
    ax.axvspan(
        active - pd.Timedelta(hours=12),
        expiry + pd.Timedelta(hours=12),
        color=YELLOW,
        alpha=0.16,
        zorder=0,
        linewidth=0,
    )
    if trade is not None:
        ax.axvspan(
            pd.Timestamp(trade["entry_date"]),
            pd.Timestamp(trade["target_exit_date"]),
            color=AQUA,
            alpha=0.13,
            zorder=0,
            linewidth=0,
        )

    draw_bars(ax, window)

    ax.hlines(
        limit,
        active - pd.Timedelta(hours=12),
        expiry + pd.Timedelta(hours=12),
        color=ORANGE,
        linewidth=2.0,
        linestyle=(0, (4, 2)),
        zorder=4,
    )
    ax.plot(
        [signal_date],
        [signal_bar["close"]],
        marker="o",
        markersize=8,
        color=BLUE,
        markeredgecolor=SURFACE,
        markeredgewidth=1.6,
        zorder=6,
    )

    top = window["high"].max()
    bottom = window["low"].min()
    span = top - bottom
    ax.set_ylim(bottom - span * 0.10, top + span * 0.30)

    annotate(
        "signal",
        f"signal close {price(signal_bar['close'])}\n"
        f"momentum {order['momentum']:.2f}σ, daily σ {order['sigma']:.1%}",
        (signal_date, signal_bar["close"]),
        INK_SECONDARY,
    )
    annotate(
        "limit",
        f"buy limit {price(limit)}\n"
        f"= close × exp(−1.0σ), {limit / signal_bar['close'] - 1:.1%}",
        (expiry, limit),
        ORANGE,
    )

    spans = [
        (BLUE, 0.07, "20-day momentum window"),
        (YELLOW, 0.16, "limit rests 3 days"),
    ]
    if trade is not None:
        spans.append((AQUA, 0.13, "5-day hold"))
    ax.legend(
        handles=[
            plt.Rectangle((0, 0), 1, 1, facecolor=color, alpha=alpha, linewidth=0, label=label)
            for color, alpha, label in spans
        ],
        loc="upper left",
        fontsize=8.5,
        handlelength=1.6,
        handleheight=1.1,
        borderpad=0.2,
    )

    if trade is not None:
        entry_date = pd.Timestamp(trade["entry_date"])
        exit_date = pd.Timestamp(trade["target_exit_date"])
        fill_bar = bars.loc[bars["date"].eq(entry_date)].iloc[0]
        ax.plot(
            [entry_date],
            [float(trade["entry_price"])],
            marker="^",
            markersize=11,
            color=GREEN,
            markeredgecolor=SURFACE,
            markeredgewidth=1.4,
            zorder=6,
        )
        ax.plot(
            [exit_date],
            [float(trade["exit_price"])],
            marker="v",
            markersize=11,
            color=RED,
            markeredgecolor=SURFACE,
            markeredgewidth=1.4,
            zorder=6,
        )
        annotate(
            "fill",
            f"filled: the low {price(fill_bar['low'])} trades through",
            (entry_date, float(trade["entry_price"])),
            GREEN,
        )
        annotate(
            "exit",
            f"sell at the open less 5 bps: {price(float(trade['exit_price']))}\n"
            f"net {float(trade['pnl']) / float(trade['reserved']):+.1%} on reserved cash",
            (exit_date, float(trade["exit_price"])),
            INK_SECONDARY,
            flip=float(trade["exit_price"]) < float(signal_bar["close"]),
        )
    else:
        window_bars = bars.loc[bars["date"].between(active, expiry)]
        lowest = window_bars["low"].min()
        lowest_date = window_bars.loc[window_bars["low"].idxmin(), "date"]
        after = bars.loc[bars["date"].between(expiry, expiry + pd.Timedelta(days=3))]
        ax.plot(
            [lowest_date],
            [lowest],
            marker="X",
            markersize=11,
            color=RED,
            markeredgecolor=SURFACE,
            markeredgewidth=1.4,
            zorder=6,
        )
        annotate(
            "fill",
            f"lowest low {price(lowest)}\nstays {lowest / limit - 1:+.2%} above the limit",
            (lowest_date, lowest),
            RED,
        )
        if not after.empty:
            run = float(after["close"].iloc[-1]) / float(signal_bar["close"]) - 1
            annotate(
                "exit",
                f"order canceled, cash released\nprice ran {run:+.0%} instead",
                (
                    pd.Timestamp(after["date"].iloc[-1]),
                    float(after["close"].iloc[-1]),
                ),
                INK_SECONDARY,
            )

    head(ax, title, caption, size=11.5)
    ax.set_ylabel("Price (USDT)")
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=7))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: price(v)))
    ax.grid(axis="x", visible=False)


def figure_trade_anatomy(trades: pd.DataFrame, orders: pd.DataFrame) -> str | None:
    """Two real orders: one that filled, one that expired.

    Neither is chosen for its outcome. The filled example is the trade sitting
    closest to the median return of every scheduled trade in the run, so the
    picture is typical rather than flattering; the expired example is the
    middle order of the expiry list by date.
    """
    if trades.empty or orders.empty:
        return None
    order_frame = orders.copy()
    order_frame["signal_date"] = pd.to_datetime(order_frame["signal_date"], utc=True)
    keyed = order_frame.set_index(["symbol", "signal_date"])

    scheduled = trades.loc[trades["exit_reason"].eq("scheduled")].copy()
    scheduled["gain"] = scheduled["exit_price"] / scheduled["entry_price"] - 1
    scheduled = scheduled.reset_index(drop=True)

    filled_trade = filled_order = filled_bars = None
    for position in median_by_return(scheduled):
        candidate = scheduled.iloc[position]
        key = (candidate["symbol"], pd.Timestamp(candidate["signal_date"]))
        if key not in keyed.index:
            continue
        bars = load_bars(str(candidate["symbol"]))
        if bars is None or bars.empty:
            continue
        filled_trade, filled_bars = candidate, bars
        filled_order = keyed.loc[key]
        if isinstance(filled_order, pd.DataFrame):
            filled_order = filled_order.iloc[0]
        filled_order = filled_order.copy()
        filled_order["symbol"] = candidate["symbol"]
        filled_order["signal_date"] = key[1]
        break
    if filled_trade is None:
        print("  skipping fig02: no filled example with bars available")
        return None

    expired = order_frame.loc[order_frame["status"].eq("canceled_expired")]
    expired = expired.sort_values(["signal_date", "symbol"]).reset_index(drop=True)
    expired_order = expired_bars = None
    for offset in range(len(expired)):
        # Walk outward from the middle of the list so the pick is positional
        # rather than a search for a dramatic near miss.
        position = len(expired) // 2 + (offset // 2) * (1 if offset % 2 else -1)
        if not 0 <= position < len(expired):
            continue
        candidate = expired.iloc[position]
        bars = load_bars(str(candidate["symbol"]))
        if bars is None or bars.empty:
            continue
        expired_order, expired_bars = candidate, bars
        break
    if expired_order is None:
        print("  skipping fig02: no expired example with bars available")
        return None

    hit_rate = float((scheduled["gain"] > 0).mean())
    entry = pd.Timestamp(filled_trade["signal_date"])
    expiry_entry = pd.Timestamp(expired_order["signal_date"])

    fig, axes = plt.subplots(1, 2, figsize=(14.6, 6.4))
    anatomy_panel(
        axes[0],
        filled_bars,
        filled_order,
        filled_trade,
        f"The correction arrives: {filled_trade['symbol']}, {entry:%b %Y}",
        f"The median trade of the run, not a good one. {hit_rate:.0%} of filled "
        "trades end in profit.",
        {
            "signal": (-34, 56, "right"),
            "limit": (30, -36, "left"),
            "fill": (36, -74, "left"),
            "exit": (-96, 84, "right"),
        },
    )
    anatomy_panel(
        axes[1],
        expired_bars,
        expired_order,
        None,
        f"The correction never comes: {expired_order['symbol']}, {expiry_entry:%b %Y}",
        "About half of all orders end this way: no trade, no loss, no gain.",
        {
            "signal": (-34, 56, "right"),
            "limit": (22, -44, "left"),
            "fill": (10, -86, "left"),
            "exit": (-118, 42, "right"),
        },
    )
    fig.tight_layout()
    return save(fig, "fig02_trade_anatomy.png")


# --------------------------------------------------------------------------
# 3. What the account did
# --------------------------------------------------------------------------


def figure_equity_drawdown(
    equity: pd.DataFrame, test_start: str, benchmark: pd.Series | None = None
) -> str:
    fig, axes = plt.subplots(
        2,
        1,
        figsize=(12.4, 7.4),
        sharex=True,
        gridspec_kw={"height_ratios": [2.1, 1]},
    )
    split = pd.Timestamp(test_start, tz="UTC")
    trough = equity.loc[equity["drawdown"].idxmin()]

    for ax in axes:
        ax.axvspan(
            split,
            equity["date"].iloc[-1],
            color=NEUTRAL_FILL,
            zorder=0,
            linewidth=0,
        )

    if benchmark is not None and not benchmark.empty:
        axes[0].plot(
            benchmark.index, benchmark.to_numpy(), color=MUTED, linewidth=1.5,
            linestyle=(0, (5, 2)), zorder=3, label="BTC buy and hold",
        )
    axes[0].plot(
        equity["date"], equity["equity"], color=BLUE, linewidth=1.8,
        zorder=4, label="Momentum + correction",
    )
    axes[0].axhline(
        equity["equity"].iloc[0], color=AXIS, linewidth=1.0, linestyle=(0, (4, 3))
    )
    axes[0].set_yscale("log")
    axes[0].set_yticks([50_000, 100_000, 250_000, 500_000, 1_000_000, 2_000_000])
    axes[0].get_yaxis().set_major_formatter(
        plt.FuncFormatter(
            lambda value, _: f"${value * 1e-6:.1f}M"
            if value >= 1e6
            else f"${value * 1e-3:,.0f}k"
        )
    )
    axes[0].set_ylabel("Account equity, log scale")
    head(
        axes[0],
        "Baseline equity and drawdown",
        "Shaded band: the clean 2024-2026 evaluation window. The 2021 peak is "
        "never regained, and the account finishes below simply holding the "
        "market it trades.",
    )
    if benchmark is not None and not benchmark.empty:
        axes[0].legend(loc="lower right", ncols=2)

    peak = equity.loc[equity["equity"].idxmax()]
    axes[0].annotate(
        f"peak ${peak['equity'] * 1e-6:.2f}M",
        xy=(peak["date"], peak["equity"]),
        xytext=(14, -4),
        textcoords="offset points",
        fontsize=9,
        color=INK_SECONDARY,
        arrowprops={"arrowstyle": "-", "color": MUTED, "linewidth": 0.9},
    )
    axes[0].annotate(
        f"final ${equity['equity'].iloc[-1] * 1e-3:,.0f}k",
        xy=(equity["date"].iloc[-1], equity["equity"].iloc[-1]),
        xytext=(-8, -26),
        textcoords="offset points",
        fontsize=9,
        color=INK_SECONDARY,
        ha="right",
        arrowprops={"arrowstyle": "-", "color": MUTED, "linewidth": 0.9},
    )
    axes[0].text(
        split,
        equity["equity"].max() * 0.95,
        "  evaluation window",
        fontsize=9,
        color=MUTED,
        ha="left",
        va="top",
    )

    axes[1].fill_between(
        equity["date"],
        equity["drawdown"] * 100,
        0,
        color=RED,
        alpha=0.85,
        linewidth=0,
    )
    axes[1].set_ylabel("Drawdown (%)")
    axes[1].set_ylim(equity["drawdown"].min() * 118, 2)
    axes[1].annotate(
        f"worst {trough['drawdown']:.1%}",
        xy=(trough["date"], trough["drawdown"] * 100),
        xytext=(-12, -18),
        textcoords="offset points",
        fontsize=9,
        color=RED,
        ha="right",
        arrowprops={"arrowstyle": "-", "color": RED, "linewidth": 0.9},
    )
    axes[1].xaxis.set_major_locator(mdates.YearLocator())
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    for ax in axes:
        ax.grid(axis="x", visible=False)
    fig.tight_layout()
    return save(fig, "fig03_equity_drawdown.png")


def figure_controls(equities: dict[str, pd.DataFrame]) -> str | None:
    available = [mode for mode in MODE_ORDER if mode in equities]
    if not available:
        return None
    fig, ax = plt.subplots(figsize=(12.4, 6.4))
    ends: list[tuple[str, float]] = []
    for mode in available:
        frame = equities[mode]
        series = frame["equity"] / frame["equity"].iloc[0]
        ax.plot(
            frame["date"],
            series,
            color=MODE_COLORS[mode],
            linewidth=2.4 if mode == "full" else 1.5,
            zorder=4 if mode == "full" else 3,
            label=SHORT_LABELS[mode],
        )
        ends.append((mode, float(series.iloc[-1])))

    ax.axhline(1.0, color=AXIS, linewidth=1.0, linestyle=(0, (4, 3)))
    ax.set_yscale("log")
    ax.set_yticks([0.5, 1, 2, 5, 10, 20])
    ax.get_yaxis().set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:g}×"))
    ax.set_ylabel("Growth of the starting account, log scale")
    head(
        ax,
        "The correction rule against its controls",
        "Same universe, sizing, costs and exits; only the entry rule changes. "
        "Generic resting bids do at least as well as the momentum filter.",
    )

    limits = ax.get_ylim()

    def to_axes(value: float) -> float:
        return (np.log10(value) - np.log10(limits[0])) / (
            np.log10(limits[1]) - np.log10(limits[0])
        )

    positions = spread_labels([to_axes(value) for _, value in ends], 0.062)
    for (mode, value), position in zip(ends, positions):
        ax.annotate(
            f"{SHORT_LABELS[mode]}  {value:.1f}×",
            xy=(1.005, position),
            xycoords="axes fraction",
            fontsize=9,
            fontweight="bold" if mode == "full" else "normal",
            color=MODE_COLORS[mode],
            va="center",
            ha="left",
        )
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.19), ncols=4)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    return save(fig, "fig04_controls.png")


def figure_oos_controls(oos: dict[str, dict]) -> str:
    modes = [mode for mode in MODE_ORDER if mode in oos]
    labels = [SHORT_LABELS[mode] for mode in modes]
    y = np.arange(len(modes))[::-1]

    fig, axes = plt.subplots(1, 3, figsize=(13.8, 4.4))
    panels = [
        ("total_return", "Total return", "percent", 0.0),
        ("max_drawdown", "Maximum drawdown", "percent", 0.0),
        ("profit_factor", "Profit factor", "ratio", 1.0),
    ]
    for ax, (key, title, kind, baseline) in zip(axes, panels):
        values = [float(oos[mode][key]) for mode in modes]
        ax.barh(
            y,
            [value - baseline for value in values],
            left=baseline,
            height=0.6,
            color=[RED if value < baseline else BLUE for value in values],
            linewidth=0,
        )
        ax.axvline(baseline, color=AXIS, linewidth=1.2, zorder=3)
        for position, value in zip(y, values):
            ax.text(
                value,
                position,
                (f"{value:.0%}" if kind == "percent" else f"{value:.2f}") + "  ",
                va="center",
                ha="right",
                fontsize=9,
                color=INK_SECONDARY,
            )
        ax.set_yticks(y)
        ax.set_yticklabels(
            labels if ax is axes[0] else ["" for _ in labels],
            color=INK_SECONDARY,
            fontsize=9.5,
        )
        ax.tick_params(axis="y", length=0)
        ax.set_title(title, fontsize=11)
        ax.grid(axis="y", visible=False)
        ax.spines["left"].set_visible(False)
        low = min(values + [baseline])
        high = max(values + [baseline])
        pad = 0.30 * max(high - low, 1e-9)
        ax.set_xlim(low - pad, high + pad * 0.35)
        ax.xaxis.set_major_formatter(
            plt.FuncFormatter(
                lambda v, _, kind=kind: f"{v:.0%}" if kind == "percent" else f"{v:.2f}"
            )
        )

    figure_head(
        fig,
        "Clean 2024-2026 restart: every variant loses money",
        "Each control restarts with $100,000 and no positions on 2024-01-01. "
        "A profit factor below 1.00 means gross losses exceed gross profits.",
    )
    fig.tight_layout()
    return save(fig, "fig05_oos_controls.png")


def figure_yearly(yearly: pd.DataFrame) -> str:
    fig, axes = plt.subplots(
        2, 1, figsize=(11.6, 6.6), sharex=True, gridspec_kw={"height_ratios": [1.35, 1]}
    )
    years = yearly["year"].astype(int).to_numpy()
    returns = yearly["return"].to_numpy()
    drawdowns = yearly["worst_drawdown"].to_numpy()

    axes[0].bar(
        years,
        returns * 100,
        color=[BLUE if value >= 0 else RED for value in returns],
        width=0.62,
        linewidth=0,
    )
    axes[0].axhline(0, color=AXIS, linewidth=1.0)
    for year, value in zip(years, returns):
        axes[0].text(
            year,
            value * 100 + (18 if value >= 0 else -18),
            f"{value:+.0%}",
            ha="center",
            va="bottom" if value >= 0 else "top",
            fontsize=9,
            color=INK_SECONDARY,
        )
    axes[0].set_ylabel("Calendar-year return (%)")
    head(
        axes[0],
        "One year carries the whole record",
        f"{int(years[np.argmax(returns)])} returns {returns.max():+.0%}. "
        "Excluding it, the remaining years compound to "
        f"{np.prod(1 + np.delete(returns, np.argmax(returns))) - 1:+.0%}.",
    )
    axes[0].set_ylim(returns.min() * 100 - 60, returns.max() * 100 + 90)

    axes[1].bar(
        years,
        drawdowns * 100,
        color=RED,
        width=0.62,
        linewidth=0,
        alpha=0.85,
    )
    for year, value in zip(years, drawdowns):
        axes[1].text(
            year,
            value * 100 - 3,
            f"{value:.0%}",
            ha="center",
            va="top",
            fontsize=9,
            color=INK_SECONDARY,
        )
    axes[1].set_ylabel("Worst drawdown\nwithin the year (%)")
    axes[1].set_ylim(drawdowns.min() * 100 - 22, 4)
    axes[1].set_xticks(years)
    for ax in axes:
        ax.grid(axis="x", visible=False)
    fig.tight_layout()
    return save(fig, "fig06_yearly.png")


# --------------------------------------------------------------------------
# 4. How fragile
# --------------------------------------------------------------------------


def heatmap_panel(
    ax: plt.Axes,
    pivot: pd.DataFrame,
    title: str,
    norm: TwoSlopeNorm,
    baseline: tuple[float, int] | None,
) -> None:
    values = pivot.to_numpy() * 100
    ax.imshow(values, cmap=DIVERGING, norm=norm, aspect="auto")
    ax.set_xticks(range(len(pivot.columns)), [str(int(c)) for c in pivot.columns])
    ax.set_yticks(range(len(pivot.index)), [f"{value:.1f}σ" for value in pivot.index])
    ax.set_xlabel("Holding period (days)")
    ax.set_title(title, fontsize=11)
    ax.grid(False)
    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            value = values[row, column]
            rgba = DIVERGING(norm(value))
            luminance = 0.299 * rgba[0] + 0.587 * rgba[1] + 0.114 * rgba[2]
            ax.text(
                column,
                row,
                f"{value:+.0f}%",
                ha="center",
                va="center",
                fontsize=9.5,
                color="#ffffff" if luminance < 0.55 else INK,
            )
    if baseline is not None:
        sigma, hold = baseline
        if sigma in list(pivot.index) and hold in list(pivot.columns):
            row = list(pivot.index).index(sigma)
            column = list(pivot.columns).index(hold)
            ax.add_patch(
                plt.Rectangle(
                    (column - 0.5, row - 0.5),
                    1,
                    1,
                    fill=False,
                    edgecolor=INK,
                    linewidth=2.2,
                    zorder=5,
                )
            )


def figure_sensitivity(sensitivity: pd.DataFrame, config: dict) -> str:
    full = sensitivity.pivot(
        index="correction_sigma", columns="holding_days", values="cagr"
    )
    clean = sensitivity.pivot(
        index="correction_sigma", columns="holding_days", values="oos_cagr"
    )
    bound = max(abs(full.to_numpy()).max(), abs(clean.to_numpy()).max()) * 100
    norm = TwoSlopeNorm(vmin=-bound, vcenter=0.0, vmax=bound)

    fig, axes = plt.subplots(1, 2, figsize=(12.6, 5.0))
    baseline = (float(config["correction_sigma"]), int(config["holding_days"]))
    heatmap_panel(axes[0], full, "Full sample 2020-2026", norm, baseline)
    heatmap_panel(axes[1], clean, "Clean restart 2024-2026", norm, baseline)
    axes[0].set_ylabel("Correction depth")

    positive = float((sensitivity["oos_cagr"] > 0).mean())
    figure_head(
        fig,
        "The sign of the result depends on two parameters nobody published",
        "Annualized return for every predeclared cell. Black outline: the frozen "
        f"baseline. Only {positive:.0%} of cells earn a positive return on the clean "
        "restart, and the winners are not the frozen one.",
    )
    fig.tight_layout()
    return save(fig, "fig07_sensitivity.png")


def figure_friction(friction: pd.DataFrame) -> str:
    data = friction.sort_values("round_trip_bps")
    bps = data["round_trip_bps"].to_numpy()
    cagr = data["cagr"].to_numpy() * 100
    drawdown = data["max_drawdown"].to_numpy() * 100

    breakeven = None
    for index in range(len(bps) - 1):
        if cagr[index] > 0 >= cagr[index + 1]:
            span = cagr[index] - cagr[index + 1]
            breakeven = bps[index] + (bps[index + 1] - bps[index]) * (cagr[index] / span)
            break

    fig, axes = plt.subplots(1, 2, figsize=(12.6, 4.8))
    axes[0].plot(bps, cagr, color=BLUE, marker="o", markersize=8, zorder=3)
    axes[0].axhline(0, color=AXIS, linewidth=1.2)
    for x, y in zip(bps, cagr):
        axes[0].annotate(
            f"{y:+.1f}%",
            xy=(x, y),
            xytext=(10, 8),
            textcoords="offset points",
            ha="left",
            fontsize=9,
            color=INK_SECONDARY,
        )
    if breakeven is not None:
        axes[0].axvline(breakeven, color=RED, linewidth=1.2, linestyle=(0, (4, 3)))
        axes[0].annotate(
            f"break-even ≈ {breakeven:.0f} bps",
            xy=(breakeven, cagr.min()),
            xytext=(-10, 16),
            textcoords="offset points",
            ha="right",
            fontsize=9,
            color=RED,
        )
    axes[0].set_ylabel("CAGR (%)")
    axes[0].set_xlabel("Round-trip cost (bps)")
    axes[0].set_title("Return against trading cost", fontsize=11)
    axes[0].set_ylim(cagr.min() - 10, cagr.max() + 10)

    axes[1].plot(bps, drawdown, color=RED, marker="o", markersize=8, zorder=3)
    for x, y in zip(bps, drawdown):
        axes[1].annotate(
            f"{y:.0f}%",
            xy=(x, y),
            xytext=(10, 8),
            textcoords="offset points",
            ha="left",
            fontsize=9,
            color=INK_SECONDARY,
        )
    axes[1].set_ylabel("Maximum drawdown (%)")
    axes[1].set_xlabel("Round-trip cost (bps)")
    axes[1].set_title("Drawdown against trading cost", fontsize=11)
    axes[1].set_ylim(drawdown.min() - 8, drawdown.max() + 8)
    for ax in axes:
        ax.set_xlim(-8, bps.max() + 12)

    figure_head(
        fig,
        f"The whole edge is inside {breakeven:.0f} bps of friction"
        if breakeven is not None
        else "Return against friction",
        "Modeled fees plus slippage on a round trip. The baseline assumes 25 bps; "
        "a retail taker account without fee discounts pays more.",
    )
    fig.tight_layout()
    return save(fig, "fig08_friction.png")


def figure_bootstrap(draws: np.ndarray, bootstrap: dict) -> str:
    values = draws * 100
    # A thin right tail would otherwise squash the mass that matters; the axis
    # stops at the 99th percentile and the figure says how much it drops.
    ceiling = float(np.quantile(values, 0.99))
    beyond = int((values > ceiling).sum())
    fig, ax = plt.subplots(figsize=(11.6, 5.2))
    counts, edges = np.histogram(values[values <= ceiling], bins=48)
    centers = (edges[:-1] + edges[1:]) / 2
    ax.bar(
        centers,
        counts,
        width=(edges[1] - edges[0]) * 0.92,
        color=[RED if center <= 0 else BLUE for center in centers],
        linewidth=0,
    )
    low = bootstrap["cagr_percentiles"]["2.5"] * 100
    median = bootstrap["cagr_percentiles"]["50"] * 100
    high = bootstrap["cagr_percentiles"]["97.5"] * 100
    ax.axvline(median, color=INK, linewidth=1.4)
    ax.annotate(
        f"median {median:.0f}%",
        xy=(median, counts.max()),
        xytext=(6, -4),
        textcoords="offset points",
        fontsize=9,
        color=INK,
    )
    ax.annotate(
        "",
        xy=(low, counts.max() * 1.06),
        xytext=(high, counts.max() * 1.06),
        arrowprops={"arrowstyle": "|-|,widthA=0.4,widthB=0.4", "color": MUTED, "linewidth": 1.1},
    )
    ax.text(
        (low + high) / 2,
        counts.max() * 1.10,
        f"95% interval  {low:.0f}% to {high:.0f}%",
        ha="center",
        va="bottom",
        fontsize=9,
        color=INK_SECONDARY,
    )
    probability = float(bootstrap["probability_cagr_le_zero"])
    ax.text(
        0.985,
        0.94,
        f"{probability:.0%} of resampled paths\nend flat or down",
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=11,
        color=RED,
        fontweight="bold",
    )
    ax.set_ylim(0, counts.max() * 1.26)
    ax.set_xlim(values.min() - 3, ceiling + 3)
    ax.set_xlabel("Annualized return of a resampled 2024-2026 path (%)")
    ax.set_ylabel("Resampled paths")
    head(
        ax,
        "Sampling uncertainty swamps the point estimate",
        f"{bootstrap['repetitions']:,} moving-block resamples of "
        f"{bootstrap['block_days']}-day blocks from the evaluation window; "
        f"{beyond} paths run past the right edge.",
    )
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    return save(fig, "fig09_bootstrap.png")


def figure_concentration(trades: pd.DataFrame) -> str:
    pnl = trades["pnl"].sort_values(ascending=False).to_numpy()
    total = pnl.sum()
    share = np.cumsum(pnl) / total * 100
    rank = np.arange(1, len(pnl) + 1) / len(pnl) * 100

    fig, ax = plt.subplots(figsize=(11.6, 5.6))
    ax.plot(rank, share, color=BLUE, linewidth=2.2)
    ax.axhline(100, color=AXIS, linewidth=1.2, linestyle=(0, (4, 3)))
    ax.text(
        99,
        100,
        "100% of net profit ",
        ha="right",
        va="bottom",
        fontsize=9,
        color=MUTED,
    )
    for fraction, color in ((1, ORANGE), (5, ORANGE), (10, ORANGE)):
        index = max(0, int(np.ceil(len(pnl) * fraction / 100)) - 1)
        ax.plot([fraction], [share[index]], marker="o", markersize=8, color=color, zorder=4)
        ax.annotate(
            f"best {fraction}% of trades → {share[index]:,.0f}%",
            xy=(fraction, share[index]),
            xytext=(14, -2),
            textcoords="offset points",
            fontsize=9,
            color=INK_SECONDARY,
            va="center",
        )
    ax.set_xlabel("Trades ranked by profit (best → worst, % of all trades)")
    ax.set_ylabel("Cumulative share of net profit (%)")
    head(
        ax,
        "Net profit is the residue of a few extreme winners",
        f"All {len(pnl):,} baseline trades. The curve peaks far above 100% because "
        "everything after the peak loses money on aggregate.",
    )
    ax.set_xlim(0, 100)
    fig.tight_layout()
    return save(fig, "fig10_concentration.png")


# --------------------------------------------------------------------------


HOURLY_SOURCE = Path(
    os.environ.get(
        "MOMENTUM_CORRECTION_HOURLY_DIR", PROJECT / "data" / "external" / "hourly"
    )
)


def load_hourly_bars(symbol: str) -> pd.DataFrame | None:
    path = HOURLY_SOURCE / f"{symbol}.parquet"
    if not path.exists():
        return None
    bars = pd.read_parquet(path).rename(columns={"open_time": "date"})
    bars["date"] = pd.to_datetime(bars["date"], unit="ms", utc=True)
    return bars.sort_values("date").reset_index(drop=True)


def figure_hourly_anatomy(trades: pd.DataFrame, orders: pd.DataFrame) -> str | None:
    """One real trade at hourly resolution, against what a daily model saw.

    The daily proxy cannot see the fill hour, so it schedules the exit from the
    calendar. Selecting a trade filled late in the day makes the size of that
    error visible, and selecting within that group on distance from the median
    return keeps the outcome typical, so the figure illustrates the timing
    error rather than advertising a good trade.
    """
    if trades.empty or "entry_time" not in trades:
        return None
    frame = trades.copy()
    for column in ("entry_time", "exit_date", "signal_date", "target_exit_date"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    frame["fill_hour"] = frame["entry_time"].dt.hour
    frame["gain"] = frame["exit_price"] / frame["entry_price"] - 1
    scheduled = frame.loc[frame["exit_reason"].eq("scheduled")]
    # Selection is on the fill hour, which is the mechanism this figure is
    # about, and never on the outcome. Among the late fills, the example is the
    # one closest to the median return of every scheduled trade, so a reader
    # cannot mistake it for a good trade.
    candidates = scheduled.loc[
        scheduled["fill_hour"].ge(17)
        & scheduled["signal_date"].ge(pd.Timestamp("2024-01-01", tz="UTC"))
    ].reset_index(drop=True)
    if candidates.empty:
        return None
    population_median = float(scheduled["gain"].median())

    trade = None
    bars = None
    for position in median_by_return(candidates, target=population_median):
        row = candidates.iloc[position]
        loaded = load_hourly_bars(str(row["symbol"]))
        if loaded is not None and not loaded.empty:
            trade = frame.loc[
                frame["symbol"].eq(row["symbol"])
                & frame["entry_time"].eq(row["entry_time"])
            ].iloc[0]
            bars = loaded
            break
    if trade is None or bars is None:
        print("  skipping hourly anatomy: no hourly bars for any candidate")
        return None

    entry = pd.Timestamp(trade["entry_time"])
    exit_moment = pd.Timestamp(trade["exit_date"])
    window = bars.loc[
        bars["date"].between(
            entry - pd.Timedelta(hours=30), exit_moment + pd.Timedelta(hours=18)
        )
    ]
    if window.empty:
        return None

    # What the daily proxy would have booked: the open of the target calendar
    # day, which is where the exit lands when the fill hour is unknown.
    daily_exit_moment = pd.Timestamp(trade["target_exit_date"])
    daily_row = bars.loc[bars["date"].eq(daily_exit_moment)]
    daily_price = float(daily_row["open"].iloc[0]) if not daily_row.empty else np.nan

    fig, ax = plt.subplots(figsize=(13.6, 6.2))
    for _, row in window.iterrows():
        ax.plot(
            [row["date"], row["date"]],
            [row["low"], row["high"]],
            color=AXIS,
            linewidth=1.1,
            solid_capstyle="round",
            zorder=2,
        )
    ax.plot(window["date"], window["close"], color=INK_SECONDARY, linewidth=1.1, zorder=3)

    limit = float(trade["entry_price"])
    ax.axvspan(entry, exit_moment, color=AQUA, alpha=0.10, linewidth=0, zorder=0)
    ax.hlines(
        limit,
        window["date"].iloc[0],
        entry,
        color=ORANGE,
        linewidth=2.0,
        linestyle=(0, (4, 2)),
        zorder=4,
    )
    ax.plot(
        [entry],
        [limit],
        marker="^",
        markersize=12,
        color=GREEN,
        markeredgecolor=SURFACE,
        markeredgewidth=1.4,
        zorder=6,
    )
    ax.plot(
        [exit_moment],
        [float(trade["exit_price"])],
        marker="v",
        markersize=12,
        color=RED,
        markeredgecolor=SURFACE,
        markeredgewidth=1.4,
        zorder=6,
    )

    top, bottom = window["high"].max(), window["low"].min()
    span = top - bottom
    ax.set_ylim(bottom - span * 0.16, top + span * 0.24)

    ax.annotate(
        f"filled {entry:%b %d %H:%M} UTC at the limit {price(limit)}",
        xy=(entry, limit),
        xytext=(-16, -46),
        textcoords="offset points",
        fontsize=9,
        color=GREEN,
        ha="right",
        arrowprops={"arrowstyle": "-", "color": GREEN, "linewidth": 0.9},
    )
    ax.annotate(
        f"exit exactly {float(trade['holding_hours_actual']):.0f}h later\n"
        f"{exit_moment:%b %d %H:%M} at {price(float(trade['exit_price']))}"
        f"  ({trade['gain']:+.1%})",
        xy=(exit_moment, float(trade["exit_price"])),
        xytext=(18, 24),
        textcoords="offset points",
        fontsize=9,
        color=INK_SECONDARY,
        ha="left",
        arrowprops={"arrowstyle": "-", "color": MUTED, "linewidth": 0.9},
    )
    if np.isfinite(daily_price):
        drift = (exit_moment - daily_exit_moment).total_seconds() / 3600
        ax.plot(
            [daily_exit_moment],
            [daily_price],
            marker="x",
            markersize=12,
            markeredgewidth=2.4,
            color=RED,
            zorder=6,
        )
        ax.annotate(
            f"where the daily model exits: {daily_exit_moment:%b %d} open\n"
            f"{price(daily_price)}, {drift:.0f}h early "
            f"({daily_price / limit - 1:+.1%} instead of {trade['gain']:+.1%})",
            xy=(daily_exit_moment, daily_price),
            xytext=(-18, -54),
            textcoords="offset points",
            fontsize=9,
            color=RED,
            ha="right",
            arrowprops={"arrowstyle": "-", "color": RED, "linewidth": 0.9},
        )

    late = float((scheduled["fill_hour"] >= 17).mean())
    head(
        ax,
        f"The same trade, hour by hour: {trade['symbol']}, {entry:%b %Y}",
        "Hourly bars. The fill hour is observed, so the exit is scheduled from the "
        "fill rather than from the calendar, and the cross marks where the daily "
        f"proxy would have sold instead. Chosen for its late fill, which {late:.0%} "
        "of trades share, and for sitting at the median return of the run; the "
        "aggregate effect is in the execution comparison, not in this one trade.",
    )
    ax.set_ylabel("Price (USDT)")
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=12))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: price(v)))
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    return save(fig, "fig02_hourly_anatomy.png")


def figure_execution_comparison(comparison: pd.DataFrame) -> str:
    rungs = [
        ("daily", False, "Daily bars", BLUE),
        ("hourly", False, "Hourly fills, exact holds", ORANGE),
        ("hourly", True, "Hourly + funding charged", AQUA),
    ]
    windows = [("full", "Full sample 2020-2026"), ("clean-oos", "Clean restart 2024-2026")]
    holdings = sorted(comparison["holding_days"].unique())
    width = 0.26

    fig, axes = plt.subplots(1, 2, figsize=(13.4, 5.2), sharey=True)
    for ax, (window, title) in zip(axes, windows):
        for offset, (execution, charged, label, color) in enumerate(rungs):
            values = []
            for holding in holdings:
                row = comparison.loc[
                    comparison["holding_days"].eq(holding)
                    & comparison["execution"].eq(execution)
                    & comparison["funding_charged"].eq(charged)
                    & comparison["window"].eq(window)
                ]
                values.append(float(row["cagr"].iloc[0]) * 100 if not row.empty else np.nan)
            positions = np.arange(len(holdings)) + (offset - 1) * width
            ax.bar(positions, values, width=width * 0.92, color=color, linewidth=0, label=label)
            for x, value in zip(positions, values):
                if not np.isfinite(value):
                    continue
                ax.text(
                    x,
                    value + (3 if value >= 0 else -3),
                    f"{value:.0f}",
                    ha="center",
                    va="bottom" if value >= 0 else "top",
                    fontsize=8,
                    color=INK_SECONDARY,
                )
        ax.axhline(0, color=AXIS, linewidth=1.2)
        ax.set_xticks(np.arange(len(holdings)), [f"{int(h)}d" for h in holdings])
        ax.set_xlabel("Holding period")
        ax.set_title(title, fontsize=11)
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("CAGR (%)")
    axes[0].legend(loc="upper left", ncols=1)

    figure_head(
        fig,
        "Resolving the fill to the hour changes the one-day result most",
        "Same venue, universe, sizing and costs in every bar; only the execution "
        "model changes. The funding rung is shown for completeness and is not the "
        "headline; see the funding attribution below.",
    )
    fig.tight_layout()
    return save(fig, "fig11_execution_comparison.png")


def figure_funding_attribution(funded: pd.DataFrame) -> str:
    notional = funded["quantity"] * funded["entry_price"]
    received = -funded["funding_cost"]
    total = received.sum()
    ranked = received.sort_values(ascending=False).to_numpy()
    share = np.cumsum(ranked) / total * 100
    rank = np.arange(1, len(ranked) + 1) / len(ranked) * 100
    per_trade = (funded["funding_cost"] / notional * 100).to_numpy()

    fig, axes = plt.subplots(1, 2, figsize=(13.4, 5.0))

    axes[0].plot(rank, share, color=ORANGE, linewidth=2.2)
    axes[0].axhline(100, color=AXIS, linewidth=1.2, linestyle=(0, (4, 3)))
    for fraction in (1, 5, 10):
        index = max(0, int(np.ceil(len(ranked) * fraction / 100)) - 1)
        axes[0].plot([fraction], [share[index]], marker="o", markersize=8, color=ORANGE)
        axes[0].annotate(
            f"top {fraction}% of trades → {share[index]:.0f}%",
            xy=(fraction, share[index]),
            xytext=(14, -2),
            textcoords="offset points",
            fontsize=9,
            color=INK_SECONDARY,
            va="center",
        )
    axes[0].set_xlabel("Trades ranked by funding received (% of all trades)")
    axes[0].set_ylabel("Cumulative share of funding received (%)")
    axes[0].set_title("Where the funding comes from", fontsize=11)
    axes[0].set_xlim(0, 100)

    floor = -8.0
    clipped = per_trade[per_trade >= floor]
    beyond = int((per_trade < floor).sum())
    counts, edges = np.histogram(clipped, bins=60)
    centers = (edges[:-1] + edges[1:]) / 2
    axes[1].bar(
        centers,
        counts,
        width=(edges[1] - edges[0]) * 0.92,
        # Orange marks funding received, matching the ranked curve beside it;
        # the report's red/blue polarity is reserved for losses and gains.
        color=[ORANGE if center < 0 else BLUE for center in centers],
        linewidth=0,
    )
    axes[1].set_yscale("log")
    axes[1].axvline(0, color=AXIS, linewidth=1.2)
    axes[1].set_xlabel("Funding paid per trade (% of entry notional; negative = received)")
    axes[1].set_ylabel("Trades, log scale")
    axes[1].set_title("Per-trade funding", fontsize=11)
    axes[1].annotate(
        f"{beyond} trades receive more than {-floor:.0f}%\nof notional and run off this axis\n"
        f"(worst {-per_trade.min():.0f}% received)",
        xy=(0.02, 0.95),
        xycoords="axes fraction",
        fontsize=9,
        color=ORANGE,
        va="top",
        ha="left",
        fontweight="bold",
    )
    axes[1].grid(axis="x", visible=False)

    price_only = funded["pnl"].sum() + funded["funding_cost"].sum()
    figure_head(
        fig,
        "Funding on this venue is a tail artifact, not a strategy result",
        f"Inside the funding-charged run, price movement contributes "
        f"\\${price_only:,.0f} of trade P&L and funding contributes "
        f"\\${-funded['funding_cost'].sum():,.0f} more. That extra comes almost entirely "
        "from a few newly listed perpetuals on hourly funding at the negative cap, at "
        "sizes that would never have filled, so funding is excluded from the headline.",
    )
    fig.tight_layout()
    return save(fig, "fig12_funding_attribution.png")


def figure_regime_reconstruction(
    curves: pd.DataFrame, summary: pd.DataFrame, source: pd.Series | None
) -> str:
    """The gated variants against the published curve.

    Every series is rescaled to its own end-2021 level, because the source
    plots additive P&L on unknown capital while this project compounds. Only
    the trajectory after that point is comparable, which is the whole question:
    does standing down in a bear market reproduce what the author shows.
    """
    fig, axes = plt.subplots(
        1, 2, figsize=(13.8, 5.4), gridspec_kw={"width_ratios": [1.9, 1]}
    )

    anchor = pd.Timestamp("2021-12-31", tz="UTC")
    palette = [BLUE, ORANGE, AQUA, YELLOW]
    if source is not None and not source.empty:
        scaled = source / float(source.loc[:anchor.tz_localize(None)].iloc[-1]) * 100
        axes[0].plot(
            scaled.index, scaled.to_numpy(), color=INK, linewidth=2.6,
            zorder=6, label="Source, digitised",
        )
    for index, column in enumerate(curves.columns):
        series = curves[column]
        base = float(series.loc[:anchor].iloc[-1])
        if base <= 0:
            continue
        colour = MUTED if column == "none" else palette[index % len(palette)]
        axes[0].plot(
            series.index, (series / base * 100).to_numpy(),
            color=colour, linewidth=1.7,
            linestyle=(0, (5, 2)) if column == "none" else "solid",
            label=column if column != "none" else "No filter",
        )
    axes[0].axvline(anchor, color=AXIS, linewidth=1.0)
    axes[0].set_ylabel("Cumulative P&L, end-2021 = 100")
    axes[0].set_title("Trajectory after the 2021 peak", fontsize=11)
    axes[0].legend(loc="upper left", ncols=2, fontsize=8.5)
    axes[0].xaxis.set_major_locator(mdates.YearLocator())
    axes[0].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axes[0].grid(axis="x", visible=False)

    labels = [str(row) for row in summary["label"]]
    y = np.arange(len(labels))[::-1]
    axes[1].barh(
        y, summary["oos_cagr"].to_numpy() * 100, height=0.6,
        color=[RED if v < 0 else BLUE for v in summary["oos_cagr"]], linewidth=0,
    )
    axes[1].axvline(0, color=AXIS, linewidth=1.2, zorder=3)
    for position, value in zip(y, summary["oos_cagr"]):
        axes[1].text(
            value * 100, position, f"{value:.1%}  ", va="center", ha="right",
            fontsize=9, color=INK_SECONDARY,
        )
    axes[1].set_yticks(y)
    axes[1].set_yticklabels(labels, fontsize=9, color=INK_SECONDARY)
    axes[1].tick_params(axis="y", length=0)
    axes[1].set_title("Clean 2024-2026 CAGR", fontsize=11)
    axes[1].grid(axis="y", visible=False)
    axes[1].spines["left"].set_visible(False)
    axes[1].xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    low = float(summary["oos_cagr"].min()) * 100
    axes[1].set_xlim(low * 1.45, max(2.0, -low * 0.25))

    figure_head(
        fig,
        "A regime gate fixes 2022 and leaves the real gap untouched",
        "Standing down when Bitcoin trades under its moving average brackets the "
        "source's shallow 2022, but every variant still falls through 2025-2026 "
        "while the published curve more than doubles. Chosen after seeing this "
        "window, so the levels are a fit, not a test.",
    )
    fig.tight_layout()
    return save(fig, "fig13_regime_reconstruction.png")


def figure_regime_equity(
    equity: pd.DataFrame,
    drawdown: pd.DataFrame,
    summary: pd.DataFrame,
    benchmark: pd.Series | None = None,
) -> str:
    """What the gate does to the account, rather than to the comparison.

    The table beside this says the gated variants multiply the return and cut
    the drawdown. That is worth seeing as a path: the gap opens in one year and
    the two curves move together the rest of the time.
    """
    palette = {"C > SMA(50)": BLUE, "C > SMA(50), 3d": AQUA,
               "C > EMA(120)": ORANGE, "C > EMA(120), 3d": YELLOW}
    fig, axes = plt.subplots(
        2, 1, figsize=(12.4, 7.6), sharex=True, gridspec_kw={"height_ratios": [2.1, 1]}
    )

    ends: list[tuple[str, float, str]] = []
    if benchmark is not None and not benchmark.empty:
        axes[0].plot(
            benchmark.index, benchmark.to_numpy(), color=RED, linewidth=1.8,
            linestyle=(0, (2, 2)), zorder=6,
        )
        ends.append(("BTC buy and hold", float(benchmark.iloc[-1]), RED))
    for column in equity.columns:
        colour = MUTED if column == "none" else palette.get(column, INK_SECONDARY)
        label = "No filter" if column == "none" else column
        axes[0].plot(
            equity.index, equity[column].to_numpy(), color=colour,
            linewidth=2.4 if column == "none" else 1.6,
            linestyle=(0, (5, 2)) if column == "none" else "solid",
            zorder=5 if column == "none" else 4, label=label,
        )
        ends.append((label, float(equity[column].iloc[-1]), colour))
    axes[0].axhline(100_000, color=AXIS, linewidth=1.0, linestyle=(0, (4, 3)))
    axes[0].set_yscale("log")
    axes[0].set_yticks([50_000, 100_000, 250_000, 500_000, 1_000_000, 2_000_000, 5_000_000])
    axes[0].get_yaxis().set_major_formatter(
        plt.FuncFormatter(
            lambda v, _: f"${v * 1e-6:.1f}M" if v >= 1e6 else f"${v * 1e-3:,.0f}k"
        )
    )
    axes[0].set_ylabel("Account equity, log scale")
    head(
        axes[0],
        "The gate earns its keep in one year",
        "Same rule, same universe, same costs; the only change is standing down "
        "when the reference asset closes under its average. The dashed red line "
        "is the benchmark that has to be beaten.",
    )
    limits = axes[0].get_ylim()
    to_axes = lambda v: (np.log10(v) - np.log10(limits[0])) / (  # noqa: E731
        np.log10(limits[1]) - np.log10(limits[0])
    )
    positions = spread_labels([to_axes(value) for _, value, _ in ends], 0.058)
    for (label, value, colour), position in zip(ends, positions):
        axes[0].annotate(
            f"{label}  ${value * 1e-3:,.0f}k", xy=(1.005, position),
            xycoords="axes fraction", fontsize=8.5, color=colour,
            va="center", ha="left",
        )

    best = summary.loc[summary["span"].gt(0)].sort_values("total_return").iloc[-1]
    for column, colour in (("none", MUTED), (str(best["label"]), BLUE)):
        if column not in drawdown:
            continue
        axes[1].fill_between(
            drawdown.index, drawdown[column].to_numpy() * 100, 0,
            color=colour, alpha=0.55 if column == "none" else 0.75, linewidth=0,
            label="No filter" if column == "none" else column,
        )
    axes[1].set_ylabel("Drawdown (%)")
    axes[1].legend(loc="lower left", ncols=2)
    axes[1].set_ylim(drawdown.min().min() * 112, 2)
    axes[1].xaxis.set_major_locator(mdates.YearLocator())
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    for ax in axes:
        ax.grid(axis="x", visible=False)
    fig.tight_layout()
    return save(fig, "fig14_regime_equity.png")


def build_all(
    *,
    config: dict,
    summaries: pd.DataFrame,
    oos: dict[str, dict],
    sensitivity: pd.DataFrame,
    friction: pd.DataFrame,
    equity: pd.DataFrame,
    equities: dict[str, pd.DataFrame],
    trades: pd.DataFrame,
    orders: pd.DataFrame,
    yearly: pd.DataFrame,
    bootstrap: dict,
    bootstrap_draws: np.ndarray | None,
    test_start: str,
    profile: str = "spot-daily",
    comparison: pd.DataFrame | None = None,
    funding_trades: pd.DataFrame | None = None,
    regime_curves: pd.DataFrame | None = None,
    regime_summary: pd.DataFrame | None = None,
    regime_equity: pd.DataFrame | None = None,
    regime_drawdown: pd.DataFrame | None = None,
    source_curve: pd.Series | None = None,
    benchmark: pd.Series | None = None,
) -> dict[str, str | None]:
    """Write every figure and return the report key to filename mapping."""
    use_style()
    print("building figures", flush=True)
    return {
        "rules": figure_strategy_rules(config, summaries, orders, profile),
        "execution": (
            figure_execution_comparison(comparison)
            if comparison is not None and not comparison.empty
            else None
        ),
        "funding": (
            figure_funding_attribution(funding_trades)
            if funding_trades is not None and not funding_trades.empty
            else None
        ),
        "anatomy": (
            figure_hourly_anatomy(trades, orders)
            if profile == "futures-hourly"
            else figure_trade_anatomy(trades, orders)
        ),
        "equity": figure_equity_drawdown(equity, test_start, benchmark),
        "controls": figure_controls(equities),
        "oos_controls": figure_oos_controls(oos),
        "yearly": figure_yearly(yearly),
        "sensitivity": figure_sensitivity(sensitivity, config),
        "friction": figure_friction(friction),
        "bootstrap": (
            figure_bootstrap(bootstrap_draws, bootstrap)
            if bootstrap_draws is not None
            else None
        ),
        "concentration": figure_concentration(trades),
        "regime": (
            figure_regime_reconstruction(regime_curves, regime_summary, source_curve)
            if regime_curves is not None and regime_summary is not None
            else None
        ),
        "regime_equity": (
            figure_regime_equity(regime_equity, regime_drawdown, regime_summary, benchmark)
            if regime_equity is not None and regime_drawdown is not None
            and regime_summary is not None
            else None
        ),
    }
