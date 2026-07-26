from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from engine import (  # noqa: E402
    BacktestConfig,
    capped_proportional_allocations,
    performance_summary,
    prepare_market,
    simulate,
)


def market_frame(days: int = 70) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    dates = pd.date_range("2020-01-01", periods=days, tz="UTC")
    for index, symbol in enumerate(("AAAUSDT", "BBBUSDT", "CCCUSDT")):
        for day, date in enumerate(dates):
            close = 100 + 0.5 * day + index
            low = close * (0.97 if day == 41 else 0.995)
            rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "open": close * 0.999,
                    "high": close * 1.01,
                    "low": low,
                    "close": close,
                    "quote_volume": 1_000_000 - index,
                }
            )
    return pd.DataFrame(rows)


class EngineTests(unittest.TestCase):
    def test_capped_allocation_redistributes(self) -> None:
        result = capped_proportional_allocations(
            {"a": 9.0, "b": 1.0},
            total=100.0,
            cap=60.0,
        )
        self.assertAlmostEqual(result["a"], 60.0)
        self.assertAlmostEqual(result["b"], 40.0)

    def test_features_use_point_in_time_ranks(self) -> None:
        config = BacktestConfig(
            volume_count=2,
            volume_lookback=5,
            momentum_lookback=5,
            volatility_lookback=5,
            start="2020-01-10",
            end="2020-03-01",
        )
        features = prepare_market(market_frame(), config)
        day = features.loc[features["date"].eq(pd.Timestamp("2020-02-10", tz="UTC"))]
        self.assertEqual(int(day["in_universe"].sum()), 2)
        self.assertFalse(bool(day.loc[day["symbol"].eq("CCCUSDT"), "in_universe"].iloc[0]))

    def test_limit_requires_strict_trade_through(self) -> None:
        frame = market_frame()
        config = BacktestConfig(
            volume_count=3,
            volume_lookback=5,
            momentum_lookback=5,
            volatility_lookback=5,
            momentum_percentile=0.0 + 1 / 3,
            correction_sigma=0.1,
            order_life_days=3,
            holding_days=2,
            start="2020-01-10",
            end="2020-03-01",
            entry_maker_bps=0,
            exit_taker_bps=0,
            exit_slippage_bps=0,
        )
        features = prepare_market(frame, config)
        result = simulate(features, config, "full")
        self.assertGreaterEqual(int(result["counters"]["fills"]), 1)
        summary = performance_summary(result, config.initial_equity)
        self.assertGreaterEqual(int(summary["trades"]), 1)

    def test_market_control_enters_next_open(self) -> None:
        config = BacktestConfig(
            volume_count=3,
            volume_lookback=5,
            momentum_lookback=5,
            volatility_lookback=5,
            momentum_percentile=1 / 3,
            holding_days=2,
            start="2020-01-10",
            end="2020-02-15",
        )
        result = simulate(prepare_market(market_frame(), config), config, "momentum_only")
        trades = result["trades"]
        self.assertFalse(trades.empty)
        self.assertTrue((trades["entry_date"] > trades["signal_date"]).all())


if __name__ == "__main__":
    unittest.main()
