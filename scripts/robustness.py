#!/usr/bin/env python3
"""Run bounded robustness stages separately from the base backtest."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import BacktestConfig, performance_summary, prepare_market, simulate  # noqa: E402
from paths import RESULTS_DIR, ensure_directories  # noqa: E402
from backtest import MODES, load_market  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage",
        choices=("sensitivity", "oos-controls", "oos-sensitivity", "friction"),
        required=True,
    )
    parser.add_argument("--correction", type=float)
    parser.add_argument("--friction-index", type=int)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--test-start", default="2024-01-01")
    parser.add_argument("--end", default="2026-06-30")
    args = parser.parse_args()
    ensure_directories()

    base = BacktestConfig(start=args.start, end=args.end)
    market = prepare_market(load_market(), base)

    if args.stage == "sensitivity":
        if args.correction is None:
            parser.error("--correction is required for sensitivity")
        sensitivity = []
        correction = args.correction
        for holding in (1, 3, 5, 10):
            print(
                f"full sensitivity: k={correction}, H={holding}",
                flush=True,
            )
            config = replace(
                base,
                correction_sigma=correction,
                holding_days=holding,
            )
            result = simulate(market, config, "full")
            summary = performance_summary(result, config.initial_equity)
            summary.update(
                {
                    "correction_sigma": correction,
                    "holding_days": holding,
                }
            )
            sensitivity.append(summary)
        suffix = str(correction).replace(".", "p")
        pd.DataFrame(sensitivity).to_csv(
            RESULTS_DIR / f"sensitivity_{suffix}.csv",
            index=False,
        )
        return

    if args.stage == "oos-controls":
        rows = []
        for mode in MODES:
            print(f"clean OOS control: {mode}", flush=True)
            config = replace(base, start=args.test_start)
            result = simulate(market, config, mode)  # type: ignore[arg-type]
            summary = performance_summary(result, config.initial_equity)
            summary["mode"] = mode
            rows.append(summary)
        pd.DataFrame(rows).to_csv(
            RESULTS_DIR / "oos_clean_summary.csv",
            index=False,
        )
        return

    if args.stage == "oos-sensitivity":
        if args.correction is None:
            parser.error("--correction is required for oos-sensitivity")
        sensitivity = []
        correction = args.correction
        for holding in (1, 3, 5, 10):
            print(
                f"clean OOS sensitivity: k={correction}, H={holding}",
                flush=True,
            )
            config = replace(
                base,
                start=args.test_start,
                correction_sigma=correction,
                holding_days=holding,
            )
            result = simulate(market, config, "full")
            summary = performance_summary(result, config.initial_equity)
            summary.update(
                {
                    "correction_sigma": correction,
                    "holding_days": holding,
                }
            )
            sensitivity.append(summary)
        suffix = str(correction).replace(".", "p")
        pd.DataFrame(sensitivity).to_csv(
            RESULTS_DIR / f"oos_sensitivity_{suffix}.csv",
            index=False,
        )
        return

    friction_configs = [
        ("zero-cost diagnostic", 0.0, 0.0, 0.0),
        ("baseline", 10.0, 10.0, 5.0),
        ("50 bps round trip", 15.0, 15.0, 20.0),
        ("100 bps round trip", 25.0, 25.0, 50.0),
    ]
    if args.friction_index is None:
        parser.error("--friction-index is required for friction")
    if not 0 <= args.friction_index < len(friction_configs):
        parser.error("--friction-index must be between 0 and 3")
    friction = []
    label, entry_fee, exit_fee, exit_slip = friction_configs[args.friction_index]
    print(f"friction: {label}", flush=True)
    config = replace(
        base,
        entry_maker_bps=entry_fee,
        exit_taker_bps=exit_fee,
        exit_slippage_bps=exit_slip,
    )
    result = simulate(market, config, "full")
    summary = performance_summary(result, config.initial_equity)
    summary.update(
        {
            "label": label,
            "round_trip_bps": entry_fee + exit_fee + exit_slip,
        }
    )
    friction.append(summary)
    pd.DataFrame(friction).to_csv(
        RESULTS_DIR / f"friction_{args.friction_index}.csv",
        index=False,
    )


if __name__ == "__main__":
    main()
