#!/usr/bin/env python3
"""Test the regime filter the source author uses, as a reconstruction hypothesis.

The momentum-correction post does not mention a regime filter. Two other posts
by the same author do:

- an earlier LinkedIn post describes the same setup with "a simple regime
  filter" and does not define it;
- a later X post publishes one directly, "Buy BTC if C > MA(50)", with a chart
  captioned `Close > SMA(50) Timing vs Buy & Hold`.

That is enough to justify testing a market-wide gate, and not enough to claim
the momentum-correction equity curve used one. The grid here is deliberately
tiny and fixed to the two averages the author has actually published or that
follow directly from them, because a wide search over regime parameters on a
period whose answer is already known would be a curve fit rather than a test.

Every number this produces is post-hoc: the filter was chosen after seeing the
2024-2026 result. It is reported as a reconstruction hypothesis and is not
promoted to the frozen baseline.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import (  # noqa: E402
    BacktestConfig,
    performance_summary,
    prepare_market,
    simulate,
)
from paths import RESULTS_DIR, ensure_directories  # noqa: E402
from backtest import load_market  # noqa: E402

REFERENCE = "BTCUSDT"

# label, kind, span, confirmation days, provenance
GATES: list[tuple[str, str, int, int, str]] = [
    ("none", "sma", 0, 1, "the frozen baseline, always in the market"),
    ("C > SMA(50)", "sma", 50, 1, "the author's published rule, verbatim"),
    ("C > SMA(50), 3d", "sma", 50, 3, "the same rule with a confirmation window"),
    ("C > EMA(120)", "ema", 120, 1, "longer average, no confirmation"),
    ("C > EMA(120), 3d", "ema", 120, 3, "longer average with a confirmation window"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-06-30")
    parser.add_argument("--test-start", default="2024-01-01")
    args = parser.parse_args()
    ensure_directories()

    market = load_market()
    rows: list[dict[str, object]] = []
    curves: dict[str, pd.Series] = {}
    equity_curves: dict[str, pd.Series] = {}
    drawdowns: dict[str, pd.Series] = {}

    for label, kind, span, confirm, provenance in GATES:
        config = replace(
            BacktestConfig(start=args.start, end=args.end),
            regime_symbol=REFERENCE if span else "",
            regime_kind=kind,
            regime_span=span,
            regime_confirm_days=confirm,
        )
        features = prepare_market(market, config)
        exposure = float(features.groupby("date")["regime_on"].first().mean())
        print(f"{label}: gate open on {exposure:.1%} of days", flush=True)

        result = simulate(features, config, "full")
        summary = performance_summary(result, config.initial_equity)
        clean = performance_summary(
            simulate(features, replace(config, start=args.test_start), "full"),
            config.initial_equity,
        )
        rows.append(
            {
                "label": label,
                "kind": kind,
                "span": span,
                "confirm_days": confirm,
                "provenance": provenance,
                "days_in_market": exposure,
                "total_return": summary["total_return"],
                "cagr": summary["cagr"],
                "max_drawdown": summary["max_drawdown"],
                "sharpe_zero_cash": summary["sharpe_zero_cash"],
                "trades": summary["trades"],
                "profit_factor": summary["profit_factor"],
                "oos_total_return": clean["total_return"],
                "oos_cagr": clean["cagr"],
                "oos_max_drawdown": clean["max_drawdown"],
            }
        )

        equity = result["equity"].set_index("date")
        equity_curves[label] = equity["equity"]
        drawdowns[label] = equity["drawdown"]

        # Additive cumulative P&L, which is the axis the source chart plots.
        trades = result["trades"]
        trades["exit_date"] = pd.to_datetime(trades["exit_date"], utc=True)
        booked = trades.groupby(trades["exit_date"].dt.normalize())["pnl"].sum()
        span_days = pd.date_range(
            pd.Timestamp(args.start, tz="UTC"), pd.Timestamp(args.end, tz="UTC"), freq="D"
        )
        curves[label] = (
            booked.reindex(span_days, fill_value=0.0).cumsum()
            / config.initial_equity
            * 100
        )

    frame = pd.DataFrame(rows)
    frame.to_csv(RESULTS_DIR / "regime_reconstruction.csv", index=False)
    pd.DataFrame(curves).to_csv(RESULTS_DIR / "regime_curves.csv")
    pd.DataFrame(equity_curves).to_csv(RESULTS_DIR / "regime_equity.csv")
    pd.DataFrame(drawdowns).to_csv(RESULTS_DIR / "regime_drawdown.csv")
    print(f"\nwrote {RESULTS_DIR / 'regime_reconstruction.csv'}")
    print(
        frame[
            ["label", "days_in_market", "total_return", "cagr", "max_drawdown", "oos_cagr"]
        ].to_string(index=False)
    )


if __name__ == "__main__":
    main()
