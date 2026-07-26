#!/usr/bin/env python3
"""Measure the strategy against simply holding the market.

A crypto strategy that compounded at 25% a year over 2020 to 2026 sounds
respectable until you ask what Bitcoin did over the same window. Every return
figure elsewhere in this project is absolute; this stage makes them relative,
which is the only form that answers whether the rule is worth running.

Two comparisons are reported:

- the plain one, total return and drawdown and Sharpe side by side;
- the regression of daily strategy returns on daily benchmark returns, whose
  intercept is the part of the result that market exposure does not explain.

The strategy holds cash roughly half the time, so its beta is well under one
and a raw return comparison flatters the benchmark. The intercept is the
honest number, and its t-statistic says whether it is distinguishable from
zero at all.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import BacktestConfig, prepare_market, simulate  # noqa: E402
from paths import RESULTS_DIR, ensure_directories  # noqa: E402
from backtest import load_market  # noqa: E402

BENCHMARK = "BTCUSDT"
ENTRY_COST = 0.001  # one 10 bps fill to establish the position


def buy_and_hold(closes: pd.Series, initial: float) -> pd.Series:
    """Equity of a single position opened on the first bar and never touched."""
    return initial * (1 - ENTRY_COST) * closes / float(closes.iloc[0])


def describe(equity: pd.Series) -> dict[str, float]:
    returns = equity.pct_change().fillna(0)
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    deviation = float(returns.std(ddof=1))
    return {
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1),
        "cagr": float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1),
        "max_drawdown": float((equity / equity.cummax() - 1).min()),
        "sharpe_zero_cash": (
            float(returns.mean() / deviation * np.sqrt(365)) if deviation else np.nan
        ),
    }


def regress(strategy: pd.Series, benchmark: pd.Series) -> dict[str, float]:
    """Ordinary least squares of strategy returns on benchmark returns.

    Reported annualised. The t-statistic uses the residual standard error,
    which assumes independent daily residuals; serial correlation would widen
    it, so treat a marginal t as weaker than it looks rather than stronger.
    """
    shared = strategy.index.intersection(benchmark.index)
    x = benchmark.loc[shared].pct_change().fillna(0).to_numpy()
    y = strategy.loc[shared].pct_change().fillna(0).to_numpy()
    beta, alpha = np.polyfit(x, y, 1)
    residual = y - (alpha + beta * x)
    error = residual.std(ddof=2) / np.sqrt(len(x))
    return {
        "beta": float(beta),
        "alpha_annual": float(alpha * 365),
        "alpha_t": float(alpha / error) if error else np.nan,
        "r_squared": float(np.corrcoef(x, y)[0, 1] ** 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-06-30")
    parser.add_argument("--test-start", default="2024-01-01")
    args = parser.parse_args()
    ensure_directories()

    market = load_market()
    closes = (
        market.loc[market["symbol"].eq(BENCHMARK), ["date", "close"]]
        .assign(date=lambda f: pd.to_datetime(f["date"], utc=True))
        .drop_duplicates("date")
        .sort_values("date")
        .set_index("date")["close"]
    )
    if closes.empty:
        raise SystemExit(f"{BENCHMARK} absent from the market data")

    variants = {
        "Momentum + correction": {},
        "Momentum + correction, SMA(50) gate": {
            "regime_symbol": BENCHMARK,
            "regime_kind": "sma",
            "regime_span": 50,
            "regime_confirm_days": 1,
        },
    }

    rows: list[dict[str, object]] = []
    curves: dict[str, pd.Series] = {}

    for window, start in (("full", args.start), ("clean", args.test_start)):
        base = BacktestConfig(start=start, end=args.end)
        held = buy_and_hold(closes.loc[start : args.end], base.initial_equity)
        curves[f"{window}: BTC buy and hold"] = held
        rows.append(
            {"window": window, "strategy": "BTC buy and hold", **describe(held),
             "beta": 1.0, "alpha_annual": 0.0, "alpha_t": np.nan, "r_squared": 1.0}
        )

        for label, overrides in variants.items():
            config = replace(base, **overrides)
            equity = (
                simulate(prepare_market(market, config), config, "full")["equity"]
                .set_index("date")["equity"]
            )
            curves[f"{window}: {label}"] = equity
            rows.append(
                {
                    "window": window,
                    "strategy": label,
                    **describe(equity),
                    **regress(equity, held),
                }
            )
            print(f"{window:6} {label:38} done", flush=True)

    frame = pd.DataFrame(rows)
    frame.to_csv(RESULTS_DIR / "benchmark.csv", index=False)
    pd.DataFrame(curves).to_csv(RESULTS_DIR / "benchmark_curves.csv")
    print(f"\nwrote {RESULTS_DIR / 'benchmark.csv'}")
    print(
        frame[
            ["window", "strategy", "total_return", "cagr", "max_drawdown",
             "sharpe_zero_cash", "beta", "alpha_annual", "alpha_t"]
        ].round(3).to_string(index=False)
    )


if __name__ == "__main__":
    main()
