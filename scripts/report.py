#!/usr/bin/env python3
"""Assemble persisted research outputs into the final report and figures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import BacktestConfig, config_dict  # noqa: E402
from paths import DATA_DIR, RESULTS_DIR, ensure_directories  # noqa: E402
import figures  # noqa: E402
from backtest import (  # noqa: E402
    MODES,
    bootstrap_draws,
    render_report,
    summarize_bootstrap,
    trade_concentration,
    write_json,
    yearly_table,
)


def require_csv(name: str) -> pd.DataFrame:
    path = RESULTS_DIR / name
    if not path.exists():
        raise SystemExit(f"missing {path}; complete the backtest stages first")
    return pd.read_csv(path)


def require_parts(pattern: str) -> pd.DataFrame:
    paths = sorted(RESULTS_DIR.glob(pattern))
    if not paths:
        raise SystemExit(
            f"missing {RESULTS_DIR / pattern}; complete robustness stages first"
        )
    return pd.concat([pd.read_csv(path) for path in paths], ignore_index=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-start", default="2024-01-01")
    parser.add_argument("--bootstrap-repetitions", type=int, default=2000)
    args = parser.parse_args()
    ensure_directories()

    config = BacktestConfig()
    summaries = require_csv("summary.csv")
    full_sensitivity = (
        require_csv("sensitivity.csv")
        if (RESULTS_DIR / "sensitivity.csv").exists()
        else require_parts("sensitivity_*p*.csv")
    )
    oos_sensitivity = require_parts("oos_sensitivity_*p*.csv")
    friction = require_parts("friction_[0-9].csv")
    oos_frame = require_csv("oos_clean_summary.csv")
    oos = {
        str(row["mode"]): row.to_dict() for _, row in oos_frame.iterrows()
    }
    sensitivity = full_sensitivity.merge(
        oos_sensitivity[
            [
                "correction_sigma",
                "holding_days",
                "cagr",
                "max_drawdown",
                "total_return",
            ]
        ].rename(
            columns={
                "cagr": "oos_cagr",
                "max_drawdown": "oos_max_drawdown",
                "total_return": "oos_total_return",
            }
        ),
        on=["correction_sigma", "holding_days"],
        how="inner",
        validate="one_to_one",
    )
    sensitivity.to_csv(RESULTS_DIR / "sensitivity_combined.csv", index=False)

    equity = require_csv("equity.csv")
    equity["date"] = pd.to_datetime(equity["date"], utc=True)
    trades = require_csv("trades.csv")
    date_columns = ["signal_date", "entry_date", "target_exit_date", "exit_date"]
    for column in date_columns:
        if column in trades:
            trades[column] = pd.to_datetime(trades[column], utc=True)

    draws = bootstrap_draws(
        equity.loc[
            equity["date"].ge(pd.Timestamp(args.test_start, tz="UTC")),
            "daily_return",
        ],
        repetitions=args.bootstrap_repetitions,
    )
    bootstrap = summarize_bootstrap(draws, repetitions=args.bootstrap_repetitions)
    write_json(RESULTS_DIR / "bootstrap.json", bootstrap)
    yearly = yearly_table(equity)
    yearly.to_csv(RESULTS_DIR / "yearly.csv", index=False)
    concentration = trade_concentration(trades)

    if trades.empty:
        by_symbol = pd.DataFrame()
    else:
        by_symbol = (
            trades.groupby("symbol")
            .agg(
                trades=("pnl", "size"),
                pnl=("pnl", "sum"),
                mean_return=("return_on_reserved", "mean"),
                hit_rate=("pnl", lambda x: (x > 0).mean()),
            )
            .sort_values("pnl", ascending=False)
            .reset_index()
        )
    by_symbol.to_csv(RESULTS_DIR / "by_symbol.csv", index=False)

    metadata_path = DATA_DIR / "manifest.json"
    coverage = (
        json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata_path.exists()
        else {}
    )
    write_json(
        RESULTS_DIR / "config.json",
        {
            "config": config_dict(config),
            "test_start": args.test_start,
            "coverage": coverage,
            "concentration": concentration,
        },
    )

    # Control equity paths are optional: only the modes whose curves were
    # persisted can be drawn.
    equities: dict[str, pd.DataFrame] = {}
    for mode in MODES:
        if mode == "full":
            equities[mode] = equity.copy()
            continue
        path = RESULTS_DIR / f"equity_{mode}.csv"
        if not path.exists():
            continue
        mode_equity = pd.read_csv(path)
        if mode_equity.empty:
            continue
        mode_equity["date"] = pd.to_datetime(mode_equity["date"], utc=True)
        equities[mode] = mode_equity

    orders = (
        require_csv("orders.csv")
        if (RESULTS_DIR / "orders.csv").exists()
        else pd.DataFrame()
    )
    comparison = (
        require_csv("futures_execution_comparison.csv")
        if (RESULTS_DIR / "futures_execution_comparison.csv").exists()
        else None
    )
    funding_trades = (
        require_csv("futures_funding_trades.csv")
        if (RESULTS_DIR / "futures_funding_trades.csv").exists()
        else None
    )
    regime_summary = (
        require_csv("regime_reconstruction.csv")
        if (RESULTS_DIR / "regime_reconstruction.csv").exists()
        else None
    )
    regime_curves = None
    if (RESULTS_DIR / "regime_curves.csv").exists():
        regime_curves = pd.read_csv(
            RESULTS_DIR / "regime_curves.csv", index_col=0, parse_dates=True
        )
        regime_curves.index = pd.to_datetime(regime_curves.index, utc=True)
    regime_equity = regime_drawdown = None
    for name, target in (("regime_equity.csv", "equity"), ("regime_drawdown.csv", "drawdown")):
        path = RESULTS_DIR / name
        if not path.exists():
            continue
        frame = pd.read_csv(path, index_col=0, parse_dates=True)
        frame.index = pd.to_datetime(frame.index, utc=True)
        if target == "equity":
            regime_equity = frame
        else:
            regime_drawdown = frame

    benchmark = None
    if (RESULTS_DIR / "benchmark_curves.csv").exists():
        curves = pd.read_csv(
            RESULTS_DIR / "benchmark_curves.csv", index_col=0, parse_dates=True
        )
        curves.index = pd.to_datetime(curves.index, utc=True)
        if "full: BTC buy and hold" in curves:
            benchmark = curves["full: BTC buy and hold"].dropna()
    benchmark_summary = (
        require_csv("benchmark.csv")
        if (RESULTS_DIR / "benchmark.csv").exists()
        else None
    )

    source_curve = None
    if (RESULTS_DIR / "source_chart_series.csv").exists():
        source_curve = pd.read_csv(
            RESULTS_DIR / "source_chart_series.csv", index_col=0, parse_dates=True
        ).iloc[:, 0]

    figure_names = figures.build_all(
        config=config_dict(config),
        summaries=summaries,
        oos=oos,
        sensitivity=sensitivity,
        friction=friction,
        equity=equity,
        equities=equities,
        trades=trades,
        orders=orders,
        yearly=yearly,
        bootstrap=bootstrap,
        bootstrap_draws=draws[0] if draws is not None else None,
        test_start=args.test_start,
        comparison=comparison,
        funding_trades=funding_trades,
        regime_curves=regime_curves,
        regime_summary=regime_summary,
        regime_equity=regime_equity,
        regime_drawdown=regime_drawdown,
        source_curve=source_curve,
        benchmark=benchmark,
    )

    report = render_report(
        config,
        summaries,
        oos,
        sensitivity,
        friction,
        {"equity": equity, "trades": trades},
        bootstrap,
        coverage,
        yearly,
        concentration,
        figure_names,
        comparison=comparison,
        regime=regime_summary,
        benchmark=benchmark_summary,
    )
    (RESULTS_DIR / "report.md").write_text(report, encoding="utf-8")
    print(f"report written to {RESULTS_DIR / 'report.md'}")


if __name__ == "__main__":
    main()
