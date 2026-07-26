#!/usr/bin/env python3
"""Digitise the source equity chart so claims about it can be checked.

Reading a competitor's chart by eye invites exactly the kind of motivated
interpretation this project is trying to avoid. This extracts the plotted
series from the published image, calibrates it against the axis gridlines, and
writes the numbers out, so every statement in SOURCE.md about the source curve
is a measurement someone else can reproduce or contradict.

The extraction is deliberately simple: the series is the only saturated blue in
the frame, the plot box is the only long dark rectangle, and the horizontal
gridlines are evenly spaced grey rows. If the author reposts a differently
styled chart this will fail loudly rather than quietly return nonsense.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from paths import RESULTS_DIR, ensure_directories  # noqa: E402

DEFAULT_IMAGE = PROJECT / "docs" / "source_chart.jpg"
# Axis facts read off the published image, which the calibration below checks.
AXIS_TOP_PERCENT = 1000.0
AXIS_STEP_PERCENT = 200.0
CHART_START = "2020-01-01"
CHART_END = "2026-07-22"


def load_series(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return per-column top, bottom and median ink rows for the plotted line."""
    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover
        raise SystemExit("pillow is required to digitise the chart") from error

    image = np.asarray(Image.open(path).convert("RGB")).astype(int)
    red, green, blue = image[:, :, 0], image[:, :, 1], image[:, :, 2]
    ink = (blue > 110) & (blue - red > 40) & (blue - green > 25)

    dark = (red < 120) & (green < 120) & (blue < 120)
    rows = np.nonzero(dark.sum(axis=1) > 400)[0]
    cols = np.nonzero(dark.sum(axis=0) > 300)[0]
    if rows.size < 2 or cols.size < 2:
        raise SystemExit("could not locate the plot box; is this the right image?")
    top, bottom, left, right = rows.min(), rows.max(), cols.min(), cols.max()

    ys, xs = np.nonzero(ink)
    inside = (xs > left + 1) & (xs < right - 1) & (ys > top + 1) & (ys < bottom - 1)
    ys, xs = ys[inside], xs[inside]
    if ys.size == 0:
        raise SystemExit("no plotted series found inside the axes")

    columns = np.array(sorted(set(xs)))
    lower = np.array([ys[xs == x].min() for x in columns])
    upper = np.array([ys[xs == x].max() for x in columns])
    middle = np.array([np.median(ys[xs == x]) for x in columns])
    return lower, upper, middle


def calibrate(path: Path) -> tuple[float, float]:
    """Pixels per percent, and the row of the top gridline."""
    from PIL import Image

    image = np.asarray(Image.open(path).convert("RGB")).astype(int)
    red, green, blue = image[:, :, 0], image[:, :, 1], image[:, :, 2]
    grey = (
        (abs(red - green) < 12)
        & (abs(green - blue) < 12)
        & (red > 180)
        & (red < 240)
    )
    gridlines = np.nonzero(grey[:, 140:1180].sum(axis=1) > 800)[0]
    if gridlines.size < 2:
        raise SystemExit("could not locate horizontal gridlines")
    spacing = float(np.median(np.diff(gridlines)))
    return spacing / AXIS_STEP_PERCENT, float(gridlines.min())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--trades", type=Path, default=RESULTS_DIR / "trades.csv")
    args = parser.parse_args()
    ensure_directories()
    if not args.image.exists():
        raise SystemExit(f"missing {args.image}")

    lower, upper, middle = load_series(args.image)
    per_percent, top_row = calibrate(args.image)
    to_percent = lambda row: AXIS_TOP_PERCENT - (row - top_row) / per_percent  # noqa: E731

    dates = pd.date_range(CHART_START, CHART_END, periods=len(middle))
    series = pd.Series(to_percent(middle), index=dates, name="cumulative_pnl_pct")
    yearly = series.resample("YE").last()

    # A column counts as moving when its ink spans more than a rendering width,
    # which is what a step in a step function looks like.
    moving = float(((upper - lower) > 2).mean())

    report: dict[str, object] = {
        "image": args.image.name,
        "columns": int(len(middle)),
        "days_per_column": round(
            (pd.Timestamp(CHART_END) - pd.Timestamp(CHART_START)).days / len(middle), 2
        ),
        "start_percent": round(float(to_percent(upper[0])), 1),
        "end_percent": round(float(to_percent(lower[-1])), 1),
        "peak_percent": round(float(to_percent(lower.min())), 1),
        "columns_moving": round(moving, 3),
        "year_end_percent": {
            str(index.year): round(float(value), 1) for index, value in yearly.items()
        },
    }

    if args.trades.exists():
        trades = pd.read_csv(args.trades, parse_dates=["exit_date"])
        booked = trades.groupby(trades["exit_date"].dt.normalize())["pnl"].sum()
        span = pd.date_range(booked.index.min(), booked.index.max(), freq="D")
        booked = booked.reindex(span, fill_value=0.0)
        edges = np.linspace(0, len(booked), len(middle) + 1).astype(int)
        ours = float(
            np.mean(
                [(booked.iloc[a:b] != 0).any() for a, b in zip(edges[:-1], edges[1:])]
            )
        )
        report["baseline_columns_moving"] = round(ours, 3)

    (RESULTS_DIR / "source_chart.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    series.to_csv(RESULTS_DIR / "source_chart_series.csv")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
