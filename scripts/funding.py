#!/usr/bin/env python3
"""Parse cached perpetual funding rates into a searchable index.

A long perpetual position pays funding whenever the rate is positive, which it
usually is in a rising market. Spot has no such cost, so a futures run that
omits funding flatters itself. The cached archive covers 2020-01 through
2026-06, so the cost can be charged from the real per-symbol series rather than
assumed.

Funding is realized at exit in the simulation: the cost is the sum over every
funding stamp inside the holding window of `rate * quantity * mark`, where the
mark is the hourly open at that stamp.
"""

from __future__ import annotations

import os
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import DATA_DIR  # noqa: E402
from intraday import eligible_symbol  # noqa: E402

# A local mirror of the venue's monthly funding-rate archives, one directory
# per symbol.
DEFAULT_SOURCE = Path(
    os.environ.get("MOMENTUM_CORRECTION_FUNDING_DIR", DATA_DIR / "external" / "funding")
)
FUNDING_CACHE = DATA_DIR / "futures_funding.parquet"


def _read_symbol(symbol_dir_text: str) -> pd.DataFrame | None:
    symbol_dir = Path(symbol_dir_text)
    symbol = symbol_dir.name
    frames: list[pd.DataFrame] = []
    for archive in sorted(symbol_dir.glob("*.zip")):
        try:
            with zipfile.ZipFile(archive) as bundle:
                name = bundle.namelist()[0]
                frame = pd.read_csv(bundle.open(name))
        except (zipfile.BadZipFile, IndexError, pd.errors.EmptyDataError):
            continue
        if frame.empty or "calc_time" not in frame:
            continue
        frames.append(frame)
    if not frames:
        return None
    data = pd.concat(frames, ignore_index=True)
    data["time"] = pd.to_datetime(data["calc_time"], unit="ms", utc=True)
    data["rate"] = pd.to_numeric(data["last_funding_rate"], errors="coerce")
    data = (
        data.dropna(subset=["time", "rate"])
        .sort_values("time")
        .drop_duplicates("time", keep="last")
    )
    if data.empty:
        return None
    data["symbol"] = symbol
    return data[["time", "symbol", "rate"]]


def build_cache(source: Path = DEFAULT_SOURCE, workers: int = 16) -> None:
    directories = sorted(
        path for path in source.iterdir() if path.is_dir() and eligible_symbol(path.name)
    )
    if not directories:
        raise SystemExit(f"no eligible funding directories under {source}")
    print(f"reading funding for {len(directories)} symbols", flush=True)
    parts: list[pd.DataFrame] = []
    with ProcessPoolExecutor(workers) as pool:
        for result in pool.map(
            _read_symbol, [str(path) for path in directories], chunksize=8
        ):
            if result is not None:
                parts.append(result)
    data = pd.concat(parts, ignore_index=True).sort_values(["symbol", "time"])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data.to_parquet(FUNDING_CACHE, index=False)
    print(
        f"wrote {len(data):,} funding stamps for {data['symbol'].nunique()} symbols, "
        f"{data['time'].min():%Y-%m} to {data['time'].max():%Y-%m}",
        flush=True,
    )


@dataclass(frozen=True)
class FundingIndex:
    times: dict[str, np.ndarray]
    rates: dict[str, np.ndarray]

    def cost(
        self,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        quantity: float,
        mark: object,
    ) -> float:
        """Funding paid by a long of `quantity` held over (start, end].

        `mark` is an object exposing `open_at(symbol, moment)`; the notional is
        marked at each funding stamp rather than held at the entry notional, so
        a position that appreciates pays funding on the larger notional.
        """
        times = self.times.get(symbol)
        if times is None or quantity <= 0:
            return 0.0
        left = int(np.searchsorted(times, start.value, side="right"))
        right = int(np.searchsorted(times, end.value, side="right"))
        if right <= left:
            return 0.0
        total = 0.0
        for offset in range(left, right):
            stamp = pd.Timestamp(times[offset], tz="UTC")
            quote = mark.open_at(symbol, stamp, tolerance_hours=2)
            if quote is None:
                continue
            total += float(self.rates[symbol][offset]) * quantity * quote[0]
        return total


def load_funding_index(path: Path = FUNDING_CACHE) -> FundingIndex:
    if not path.exists():
        raise SystemExit(f"missing {path}; run scripts/funding.py --build first")
    frame = pd.read_parquet(path)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    frame = frame.sort_values(["symbol", "time"])
    times: dict[str, np.ndarray] = {}
    rates: dict[str, np.ndarray] = {}
    for symbol, group in frame.groupby("symbol", sort=False):
        times[symbol] = group["time"].to_numpy(dtype="datetime64[ns]").astype("int64")
        rates[symbol] = group["rate"].to_numpy(dtype=float)
    return FundingIndex(times, rates)


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
        frame = pd.read_parquet(FUNDING_CACHE)
        print(frame.groupby(frame["time"].dt.year)["rate"].describe())


if __name__ == "__main__":
    main()
