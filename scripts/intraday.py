#!/usr/bin/env python3
"""Load cached hourly klines for hour-resolution execution.

The daily research run can only say that a low traded through the limit
somewhere inside a UTC day. That is tolerable at a five-day hold and useless at
a one-day hold, where the unobserved fill time is the whole holding period.
This module supplies the hourly bars the execution model needs: signals are
still computed on UTC daily bars resampled from the same source, so the
strategy definition does not change, only the resolution of the fill and the
exit.

The cached archive is Binance **USD-M perpetual futures**, not spot. That is a
different venue from the daily spot run and is labelled as such everywhere it
is used.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from paths import DATA_DIR  # noqa: E402

# Point this at a local mirror of the venue's hourly klines, one parquet per
# symbol. Nothing in the repository depends on where that mirror lives.
DEFAULT_SOURCE = Path(
    os.environ.get("MOMENTUM_CORRECTION_HOURLY_DIR", DATA_DIR / "external" / "hourly")
)
DAILY_CACHE = DATA_DIR / "futures_1h_daily.parquet"
HOURLY_CACHE = DATA_DIR / "futures_1h_execution.parquet"

# Same exclusion rules the spot universe uses, so the two runs differ by venue
# and resolution rather than by eligibility policy.
STABLE_BASES = {
    "AEUR", "BFDUSD", "BFUSD", "BKRW", "BRL", "BUSD", "DAI", "EUR", "EURI",
    "FDUSD", "GBP", "GUSD", "IDRT", "JPY", "PAX", "RLUSD", "RUB", "SUSD",
    "TUSD", "UAH", "USD1", "USDC", "USDE", "USDP", "USDS", "UST", "USTC",
    "VAI", "XUSD",
}
WRAPPED_BASES = {"BETH", "BTCB", "WBETH", "WBNB", "WBTC", "WETH"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

HOUR_NS = 3_600_000_000_000
HOURS_PER_DAY = 24


def eligible_symbol(symbol: str) -> bool:
    if not symbol.endswith("USDT"):
        return False
    base = symbol[: -len("USDT")]
    return (
        base not in STABLE_BASES
        and base not in WRAPPED_BASES
        and not base.endswith(LEVERAGED_SUFFIXES)
    )


def _read_symbol(path: Path) -> pd.DataFrame | None:
    """Read one symbol's hourly bars and normalize the schema."""
    symbol = path.stem
    frame = pd.read_parquet(path)
    if frame.empty:
        return None
    frame = frame.rename(columns={"open_time": "timestamp"})
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    numeric = ["open", "high", "low", "close", "quote_volume"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    frame = (
        frame.dropna(subset=["timestamp", *numeric])
        .loc[lambda x: (x[numeric] >= 0).all(axis=1) & x["close"].gt(0)]
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
    )
    if frame.empty:
        return None
    frame["symbol"] = symbol
    return frame[["timestamp", "symbol", "open", "high", "low", "close", "quote_volume"]]


def _daily_from_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate hourly bars into the UTC daily bars the signal uses.

    Incomplete days are dropped rather than kept. A listing day covering ten
    hours has an understated volume and a truncated range, and feeding it to a
    volatility estimate biases that estimate for the next twenty days. The
    signal is defined on complete daily bars, so a partial bar is treated as no
    bar; the resulting hole starts a fresh rolling window through the
    contiguous-segment logic in `prepare_market`.
    """
    daily = (
        frame.set_index("timestamp")
        .resample("1D")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            quote_volume=("quote_volume", "sum"),
            hours=("close", "size"),
        )
        .dropna(subset=["close"])
        .reset_index()
        .rename(columns={"timestamp": "date"})
    )
    daily = daily.loc[daily["hours"].eq(HOURS_PER_DAY)]
    daily["symbol"] = frame["symbol"].iloc[0]
    return daily


def _process(path_text: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    frame = _read_symbol(Path(path_text))
    if frame is None:
        return None
    return _daily_from_hourly(frame), frame


def build_cache(source: Path = DEFAULT_SOURCE, workers: int = 16) -> None:
    """Parse the cached hourly archive into two project-local parquet files."""
    files = sorted(
        path for path in source.glob("*.parquet") if eligible_symbol(path.stem)
    )
    if not files:
        raise SystemExit(f"no eligible parquet files under {source}")
    print(f"reading {len(files)} symbols from {source}", flush=True)

    daily_parts: list[pd.DataFrame] = []
    hourly_parts: list[pd.DataFrame] = []
    with ProcessPoolExecutor(workers) as pool:
        for index, result in enumerate(
            pool.map(_process, [str(path) for path in files], chunksize=8), start=1
        ):
            if result is None:
                continue
            daily_parts.append(result[0])
            hourly_parts.append(result[1])
            if index % 100 == 0:
                print(f"  {index}/{len(files)}", flush=True)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    daily = pd.concat(daily_parts, ignore_index=True).sort_values(["symbol", "date"])
    daily.to_parquet(DAILY_CACHE, index=False)
    hourly = pd.concat(hourly_parts, ignore_index=True).sort_values(
        ["symbol", "timestamp"]
    )
    hourly[["timestamp", "symbol", "open", "low"]].to_parquet(HOURLY_CACHE, index=False)
    print(
        f"wrote {len(daily):,} daily bars and {len(hourly):,} hourly bars "
        f"for {daily['symbol'].nunique()} symbols",
        flush=True,
    )


@dataclass(frozen=True)
class HourlyIndex:
    """Per-symbol hourly arrays supporting searchsorted lookups."""

    timestamps: dict[str, np.ndarray]
    opens: dict[str, np.ndarray]
    lows: dict[str, np.ndarray]

    def __contains__(self, symbol: object) -> bool:
        return symbol in self.timestamps

    def first_trade_through(
        self,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        limit: float,
    ) -> pd.Timestamp | None:
        """First hourly bar in [start, end) whose low trades below the limit."""
        times = self.timestamps.get(symbol)
        if times is None:
            return None
        left = int(np.searchsorted(times, start.value, side="left"))
        right = int(np.searchsorted(times, end.value, side="left"))
        if right <= left:
            return None
        window = self.lows[symbol][left:right]
        hits = np.flatnonzero(window < limit)
        if hits.size == 0:
            return None
        return pd.Timestamp(times[left + int(hits[0])], tz="UTC")

    def open_at(
        self,
        symbol: str,
        moment: pd.Timestamp,
        tolerance_hours: int = 24,
    ) -> tuple[float, pd.Timestamp] | None:
        """Open of the first hourly bar at or after `moment`, within tolerance."""
        times = self.timestamps.get(symbol)
        if times is None:
            return None
        index = int(np.searchsorted(times, moment.value, side="left"))
        if index >= times.size:
            return None
        gap = times[index] - moment.value
        if gap > tolerance_hours * HOUR_NS:
            return None
        return float(self.opens[symbol][index]), pd.Timestamp(times[index], tz="UTC")


def load_hourly_index(path: Path = HOURLY_CACHE) -> HourlyIndex:
    if not path.exists():
        raise SystemExit(f"missing {path}; run scripts/intraday.py --build first")
    frame = pd.read_parquet(path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values(["symbol", "timestamp"])
    timestamps: dict[str, np.ndarray] = {}
    opens: dict[str, np.ndarray] = {}
    lows: dict[str, np.ndarray] = {}
    for symbol, group in frame.groupby("symbol", sort=False):
        timestamps[symbol] = group["timestamp"].to_numpy(dtype="datetime64[ns]").astype(
            "int64"
        )
        opens[symbol] = group["open"].to_numpy(dtype=float)
        lows[symbol] = group["low"].to_numpy(dtype=float)
    return HourlyIndex(timestamps, opens, lows)


def load_daily(path: Path = DAILY_CACHE) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"missing {path}; run scripts/intraday.py --build first")
    frame = pd.read_parquet(path)
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    return frame


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    if args.build:
        build_cache(args.source, args.workers)
    else:
        daily = load_daily()
        print(daily.groupby("symbol")["date"].agg(["min", "max", "size"]).describe())


if __name__ == "__main__":
    main()
