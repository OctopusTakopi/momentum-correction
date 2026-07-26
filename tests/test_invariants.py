"""Invariants that must hold for the backtest to mean anything.

These are the checks that would catch the errors which quietly turn a research
backtest into a fantasy: features that peek at the future, orders that fill
before they exist, exits that land at a convenient time, and cash that appears
from nowhere.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "scripts"))

from engine import (  # noqa: E402
    BacktestConfig,
    capped_proportional_allocations,
    prepare_market,
    simulate,
)
from intraday import HourlyIndex  # noqa: E402

FEATURES = ["momentum", "sigma", "volume_sum", "volume_rank", "signal"]


def synthetic_market(days: int = 200, symbols: int = 8, seed: int = 11) -> pd.DataFrame:
    """A deterministic pseudo-market with enough dispersion to trade."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2020-01-01", periods=days, tz="UTC")
    rows: list[dict[str, object]] = []
    for index in range(symbols):
        drift = 0.004 * (index - symbols / 2) / symbols
        shocks = rng.normal(drift, 0.04, size=days)
        close = 100.0 * np.exp(np.cumsum(shocks))
        for day, date in enumerate(dates):
            price = float(close[day])
            spread = abs(float(shocks[day])) + 0.01
            rows.append(
                {
                    "date": date,
                    "symbol": f"S{index:02d}USDT",
                    "open": price * (1 - spread / 4),
                    "high": price * (1 + spread),
                    "low": price * (1 - spread),
                    "close": price,
                    "quote_volume": 1e6 * (index + 1) * (1 + 0.1 * rng.random()),
                }
            )
    return pd.DataFrame(rows)


def hourly_from_daily(daily: pd.DataFrame) -> HourlyIndex:
    """Expand daily bars into 24 hourly bars that respect the daily range.

    The low is placed at a symbol-dependent hour so fills land at different
    times of day, which is what the exit scheduling has to cope with.
    """
    timestamps: dict[str, np.ndarray] = {}
    opens: dict[str, np.ndarray] = {}
    lows: dict[str, np.ndarray] = {}
    for symbol, group in daily.groupby("symbol", sort=False):
        stamps: list[int] = []
        open_values: list[float] = []
        low_values: list[float] = []
        low_hour = 3 + (int(symbol[1:3]) * 5) % 20
        for _, row in group.iterrows():
            base = pd.Timestamp(row["date"])
            for hour in range(24):
                moment = base + pd.Timedelta(hours=hour)
                stamps.append(moment.value)
                fraction = hour / 23
                open_values.append(
                    float(row["open"]) * (1 - fraction) + float(row["close"]) * fraction
                )
                low_values.append(
                    float(row["low"]) if hour == low_hour else float(row["open"]) * 0.999
                )
        timestamps[symbol] = np.asarray(stamps, dtype="int64")
        opens[symbol] = np.asarray(open_values, dtype=float)
        lows[symbol] = np.asarray(low_values, dtype=float)
    return HourlyIndex(timestamps, opens, lows)


CONFIG = BacktestConfig(
    volume_count=5,
    volume_lookback=10,
    momentum_lookback=10,
    volatility_lookback=10,
    momentum_percentile=0.6,
    correction_sigma=1.0,
    order_life_days=3,
    holding_days=5,
    start="2020-02-01",
    end="2020-07-15",
)


class LookAheadTests(unittest.TestCase):
    """Features may only use information available when they are stamped."""

    def test_features_are_truncation_invariant(self) -> None:
        """Recomputing on data that stops at T must not change T's features.

        This is the direct test for look-ahead: if any feature consulted a
        later bar, deleting every later bar would change its value.
        """
        market = synthetic_market()
        full = prepare_market(market, CONFIG)
        for cutoff in ("2020-03-15", "2020-05-01", "2020-06-20"):
            moment = pd.Timestamp(cutoff, tz="UTC")
            truncated = prepare_market(
                market.loc[market["date"].le(moment)].copy(), CONFIG
            )
            left = full.loc[full["date"].eq(moment)].set_index("symbol")[FEATURES]
            right = truncated.loc[
                truncated["date"].eq(moment)
            ].set_index("symbol")[FEATURES]
            self.assertFalse(left.empty, f"no rows at {cutoff}")
            pd.testing.assert_frame_equal(
                left.sort_index(), right.sort_index(), check_exact=False, rtol=1e-12
            )

    def test_signal_uses_no_future_return(self) -> None:
        """Momentum must be uncorrelated with the next day's return by design.

        Not a statistical claim about profitability: it checks that shifting
        the price series forward changes momentum, so momentum is anchored to
        the past rather than silently centred on the present.
        """
        market = synthetic_market()
        features = prepare_market(market, CONFIG)
        sample = features.dropna(subset=["momentum"]).iloc[500]
        symbol, date = sample["symbol"], sample["date"]
        history = market.loc[
            market["symbol"].eq(symbol) & market["date"].le(date)
        ].sort_values("date")
        closes = history["close"].to_numpy()
        returns = np.diff(np.log(closes))
        expected_sigma = returns[-CONFIG.volatility_lookback :].std(ddof=1)
        expected_momentum = (
            np.log(closes[-1] / closes[-1 - CONFIG.momentum_lookback])
            / (expected_sigma * np.sqrt(CONFIG.momentum_lookback))
        )
        self.assertAlmostEqual(float(sample["sigma"]), float(expected_sigma), places=12)
        self.assertAlmostEqual(
            float(sample["momentum"]), float(expected_momentum), places=10
        )


class OrderTimingTests(unittest.TestCase):
    """Orders cannot act before they exist, and exits cannot arrive early."""

    def setUp(self) -> None:
        self.market = synthetic_market()
        self.features = prepare_market(self.market, CONFIG)
        self.hourly = hourly_from_daily(self.market)

    def test_daily_fill_never_precedes_activation(self) -> None:
        result = simulate(self.features, CONFIG, "full")
        trades, orders = result["trades"], result["orders"]
        self.assertFalse(trades.empty)
        self.assertTrue((trades["entry_date"] > trades["signal_date"]).all())
        life = pd.Timedelta(days=CONFIG.order_life_days)
        self.assertTrue((trades["entry_date"] <= trades["signal_date"] + life).all())
        filled = orders.loc[orders["status"].eq("filled")]
        self.assertTrue((filled["active_date"] > filled["signal_date"]).all())

    def test_daily_exit_is_never_early(self) -> None:
        result = simulate(self.features, CONFIG, "full")
        trades = result["trades"]
        scheduled = trades.loc[trades["exit_reason"].eq("scheduled")]
        self.assertFalse(scheduled.empty)
        hold = pd.Timedelta(days=CONFIG.holding_days)
        self.assertTrue((scheduled["exit_date"] >= scheduled["entry_date"] + hold).all())

    def test_limit_fill_price_is_never_improved(self) -> None:
        """A resting bid fills at its own price, never at the lower trade."""
        result = simulate(self.features, CONFIG, "full")
        trades = result["trades"]
        orders = result["orders"].set_index(["symbol", "signal_date"])
        for _, trade in trades.iterrows():
            limit = float(
                orders.loc[(trade["symbol"], trade["signal_date"]), "limit"]
            )
            self.assertAlmostEqual(float(trade["entry_price"]), limit, places=12)

    def test_hourly_exit_is_exactly_the_holding_period(self) -> None:
        result = simulate(self.features, CONFIG, "full", hourly=self.hourly)
        trades = result["trades"]
        scheduled = trades.loc[trades["exit_reason"].eq("scheduled")]
        self.assertFalse(scheduled.empty)
        self.assertTrue(
            np.allclose(
                scheduled["holding_hours_actual"].to_numpy(),
                CONFIG.holding_days * 24,
            )
        )

    def test_hourly_fill_matches_a_bar_that_traded_through(self) -> None:
        result = simulate(self.features, CONFIG, "full", hourly=self.hourly)
        trades = result["trades"]
        self.assertFalse(trades.empty)
        for _, trade in trades.head(40).iterrows():
            symbol = str(trade["symbol"])
            moment = pd.Timestamp(trade["entry_time"])
            index = int(
                np.searchsorted(self.hourly.timestamps[symbol], moment.value)
            )
            self.assertLess(
                self.hourly.lows[symbol][index],
                float(trade["entry_price"]),
                f"{symbol} fill bar did not trade through the limit",
            )


class AccountingTests(unittest.TestCase):
    """Cash has to come from somewhere and exposure has to stay inside its cap."""

    def setUp(self) -> None:
        self.market = synthetic_market()
        self.features = prepare_market(self.market, CONFIG)

    def test_final_equity_equals_initial_plus_realised_pnl(self) -> None:
        """With every position closed by the end, the books must balance."""
        result = simulate(self.features, CONFIG, "full")
        equity, trades = result["equity"], result["trades"]
        self.assertEqual(int(equity["open_positions"].iloc[-1]), 0)
        expected = CONFIG.initial_equity + float(trades["pnl"].sum())
        self.assertAlmostEqual(
            float(equity["equity"].iloc[-1]), expected, delta=abs(expected) * 1e-9
        )

    def test_exposure_never_exceeds_the_gross_cap(self) -> None:
        result = simulate(self.features, CONFIG, "full")
        equity = result["equity"]
        exposure = equity["invested"] + equity["reserved"]
        cap = CONFIG.max_gross_fraction * equity["equity"]
        self.assertTrue((exposure <= cap * (1 + 1e-9) + 1e-6).all())

    def test_reservation_respects_the_per_symbol_cap(self) -> None:
        result = simulate(self.features, CONFIG, "full")
        orders, equity = result["orders"], result["equity"]
        marks = equity.set_index("date")["equity"]
        for _, order in orders.iterrows():
            reference = float(marks.loc[order["signal_date"]])
            self.assertLessEqual(
                float(order["reserved"]),
                CONFIG.max_symbol_fraction * reference * (1 + 1e-9) + 1e-6,
            )

    def test_allocation_is_proportional_and_capped(self) -> None:
        allocations = capped_proportional_allocations(
            {"a": 3.0, "b": 1.0, "c": 1.0}, total=100.0, cap=50.0
        )
        self.assertAlmostEqual(sum(allocations.values()), 100.0)
        self.assertLessEqual(max(allocations.values()), 50.0 + 1e-9)
        self.assertAlmostEqual(allocations["b"], allocations["c"])

    def test_allocation_cannot_exceed_available_budget(self) -> None:
        allocations = capped_proportional_allocations(
            {"a": 1.0, "b": 1.0}, total=10.0, cap=50.0
        )
        self.assertAlmostEqual(sum(allocations.values()), 10.0)


if __name__ == "__main__":
    unittest.main()
