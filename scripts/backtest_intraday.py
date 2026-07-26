#!/usr/bin/env python3
"""Measure what hour-resolution execution changes.

This is not a second study. The daily spot run in `results/report.md` is the
research result; this pipeline exists to answer one question that daily bars
cannot, namely how much of that result is an artifact of not knowing when
inside a day a limit filled.

It contributes three files to the same result set and nothing else:

- `futures_execution_comparison.csv`, the daily-versus-hourly grid;
- `futures_funding_summary.csv` and `futures_funding_trades.csv`, the funding
  attribution.

The venue is Binance USD-M perpetual futures, because that is what the cached
hourly archive covers. Levels from it are not comparable to the spot study;
only the steps within each block of the comparison grid are.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import (  # noqa: E402
    BacktestConfig,
    config_dict,
    performance_summary,
    prepare_market,
    simulate,
)
from paths import RESULTS_DIR, ensure_directories  # noqa: E402
from backtest import MODES, period_result, write_json  # noqa: E402
import funding as funding_module  # noqa: E402
import intraday  # noqa: E402

CORRECTIONS = (0.5, 1.0, 1.5, 2.0)
HOLDINGS = (1, 3, 5, 10)
FRICTIONS = [
    ("zero-cost diagnostic", 0.0, 0.0, 0.0),
    ("baseline", 10.0, 10.0, 5.0),
    ("50 bps round trip", 15.0, 15.0, 20.0),
    ("100 bps round trip", 25.0, 25.0, 50.0),
]

# Populated once in the parent process; forked workers inherit it copy-on-write
# rather than pickling a 14-million-row index per task.
MARKET: pd.DataFrame | None = None
HOURLY: intraday.HourlyIndex | None = None
FUNDING: funding_module.FundingIndex | None = None


def run(
    config: BacktestConfig,
    mode: str,
    execution: str = "hourly",
    charge_funding: bool = False,
) -> dict:
    assert MARKET is not None
    hourly = HOURLY if execution == "hourly" else None
    charge = FUNDING if (hourly is not None and charge_funding) else None
    result = simulate(MARKET, config, mode, hourly=hourly, funding=charge)  # type: ignore[arg-type]
    summary = performance_summary(result, config.initial_equity)
    summary["mode"] = mode
    summary["execution"] = execution
    summary["funding_charged"] = charge is not None
    return summary


def _control(args: tuple) -> tuple[str, dict, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    config, mode, test_start, end = args
    assert MARKET is not None
    result = simulate(MARKET, config, mode, hourly=HOURLY)  # type: ignore[arg-type]
    summary = performance_summary(result, config.initial_equity)
    summary["mode"] = mode
    oos, _ = period_result(result, test_start, end)
    return (
        mode,
        summary,
        result["equity"],
        result["trades"],
        result["orders"],
        oos,
    )


def _sensitivity(args: tuple) -> dict:
    base, correction, holding, start = args
    config = replace(
        base, correction_sigma=correction, holding_days=holding, start=start
    )
    summary = run(config, "full")
    summary.update({"correction_sigma": correction, "holding_days": holding})
    return summary


def _friction(args: tuple) -> dict:
    base, index = args
    label, entry_fee, exit_fee, exit_slip = FRICTIONS[index]
    config = replace(
        base,
        entry_maker_bps=entry_fee,
        exit_taker_bps=exit_fee,
        exit_slippage_bps=exit_slip,
    )
    summary = run(config, "full")
    summary.update(
        {"label": label, "round_trip_bps": entry_fee + exit_fee + exit_slip}
    )
    return summary


def _oos_control(args: tuple) -> dict:
    base, mode, test_start = args
    return run(replace(base, start=test_start), mode)


def _comparison(args: tuple) -> dict:
    base, holding, execution, charge_funding, start = args
    config = replace(base, holding_days=holding, start=start)
    summary = run(config, "full", execution=execution, charge_funding=charge_funding)
    summary["holding_days"] = holding
    summary["window"] = "full" if start == base.start else "clean-oos"
    return summary


def main() -> None:
    global MARKET, HOURLY, FUNDING
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-06-30")
    parser.add_argument("--test-start", default="2024-01-01")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    ensure_directories()

    base = BacktestConfig(start=args.start, end=args.end)
    print("loading cached hourly futures archive", flush=True)
    daily = intraday.load_daily()
    HOURLY = intraday.load_hourly_index()
    FUNDING = funding_module.load_funding_index()
    MARKET = prepare_market(daily, base)
    print(
        f"{len(MARKET):,} point-in-time rows, "
        f"{MARKET['symbol'].nunique()} symbols, "
        f"{int(MARKET['signal'].sum()):,} signal rows",
        flush=True,
    )

    pool = ProcessPoolExecutor(args.workers)

    print("controls", flush=True)
    control_jobs = [(base, mode, args.test_start, args.end) for mode in MODES]
    summaries: list[dict] = []
    oos: dict[str, dict] = {}
    for mode, summary, equity, trades, orders, oos_row in pool.map(
        _control, control_jobs
    ):
        summaries.append(summary)
        oos[mode] = oos_row
        if mode == "full":
            trades.to_csv(RESULTS_DIR / "futures_trades.csv", index=False)
        print(f"  {mode}: {summary['total_return']:.2%}", flush=True)
    pd.DataFrame(summaries).to_csv(RESULTS_DIR / "futures_summary.csv", index=False)

    print("clean out-of-sample controls", flush=True)
    rows = list(
        pool.map(_oos_control, [(base, mode, args.test_start) for mode in MODES])
    )
    pd.DataFrame(rows).to_csv(RESULTS_DIR / "futures_oos_summary.csv", index=False)

    print("parameter sensitivity", flush=True)
    grid = [
        (base, correction, holding, args.start)
        for correction in CORRECTIONS
        for holding in HOLDINGS
    ]
    pd.DataFrame(list(pool.map(_sensitivity, grid))).to_csv(
        RESULTS_DIR / "futures_sensitivity.csv", index=False
    )

    print("funding attribution", flush=True)
    funded = simulate(
        MARKET, replace(base), "full", hourly=HOURLY, funding=FUNDING
    )  # type: ignore[arg-type]
    funded_trades = funded["trades"]
    funded_trades.to_csv(RESULTS_DIR / "futures_funding_trades.csv", index=False)
    funded_summary = performance_summary(funded, base.initial_equity)
    funded_summary["mode"] = "full"
    funded_summary["execution"] = "hourly"
    funded_summary["funding_charged"] = True
    pd.DataFrame([funded_summary]).to_csv(
        RESULTS_DIR / "futures_funding_summary.csv", index=False
    )

    print("execution-model comparison", flush=True)
    # Three rungs per holding period and window, so the daily-spot headline can
    # be walked to the hourly-futures headline one change at a time:
    # execution model first, then the funding charge.
    comparison_jobs = [
        (base, holding, execution, charge, start)
        for holding in (1, 3, 5, 10)
        for execution, charge in (("daily", False), ("hourly", False), ("hourly", True))
        for start in (args.start, args.test_start)
    ]
    pd.DataFrame(list(pool.map(_comparison, comparison_jobs))).to_csv(
        RESULTS_DIR / "futures_execution_comparison.csv", index=False
    )

    pool.shutdown()

    write_json(
        RESULTS_DIR / "futures_config.json",
        {
            "config": config_dict(base),
            "test_start": args.test_start,
            "venue": "Binance USD-M perpetual futures",
            "execution": "hourly klines, fill at first trade-through bar, "
            "exit at fill + holding_days*24h",
            "signal_bars": "UTC daily, resampled from the same hourly archive",
            "funding": "excluded from the headline P&L and reported separately; "
            "the charged variant is in funding_charged_summary.csv",
        },
    )
    print("intraday inputs written; rerun scripts/report.py to fold them in")


if __name__ == "__main__":
    main()
