#!/usr/bin/env python3
"""Parse verified monthly daily-klines into one parquet per symbol."""

from __future__ import annotations

import argparse
import io
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from paths import DATA_DIR, PARQUET_DIR, RAW_DIR, ensure_directories  # noqa: E402

COLS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
]
KEEP = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
]


def read_zip(path: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            raw = archive.read(name)
            first = raw.split(b"\n", 1)[0].lower()
            skiprows = 1 if first.startswith(b"open_time") else 0
            frames.append(
                pd.read_csv(
                    io.BytesIO(raw),
                    header=None,
                    skiprows=skiprows,
                    names=COLS,
                    usecols=KEEP,
                )
            )
    return pd.concat(frames, ignore_index=True)


def parse_symbol(symbol_dir: Path) -> tuple[str, int, str | None]:
    symbol = symbol_dir.name
    files = sorted((symbol_dir / "1d").glob("*.zip"))
    if not files:
        return symbol, 0, None
    try:
        frame = pd.concat([read_zip(path) for path in files], ignore_index=True)
        timestamps = pd.to_numeric(frame["open_time"], errors="coerce")
        timestamps = timestamps.where(timestamps < 10**14, timestamps // 1000)
        frame["date"] = pd.to_datetime(timestamps, unit="ms", utc=True)
        numeric = ["open", "high", "low", "close", "volume", "quote_volume"]
        frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
        frame["trades"] = pd.to_numeric(frame["trades"], errors="coerce")
        frame["symbol"] = symbol
        frame = (
            frame[["date", "symbol", *numeric, "trades"]]
            .dropna(subset=["date", *numeric])
            .drop_duplicates("date", keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
        frame.to_parquet(PARQUET_DIR / f"{symbol}.parquet", index=False)
        return symbol, len(frame), None
    except Exception as exc:
        return symbol, 0, str(exc)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DATA_DIR / "manifest.txt",
    )
    args = parser.parse_args()
    ensure_directories()
    manifest_symbols = {
        Path(line.strip()).parts[-3]
        for line in args.manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    symbols = sorted(
        path
        for path in RAW_DIR.iterdir()
        if path.is_dir() and path.name in manifest_symbols
    )
    if not symbols:
        raise SystemExit(f"no symbol directories under {RAW_DIR}")
    failures: list[tuple[str, str]] = []
    rows = 0
    with ProcessPoolExecutor(max_workers=8) as executor:
        for completed, (symbol, count, error) in enumerate(
            executor.map(parse_symbol, symbols),
            1,
        ):
            rows += count
            if error:
                failures.append((symbol, error))
            if completed % 100 == 0 or completed == len(symbols):
                print(
                    f"{completed}/{len(symbols)} parsed; "
                    f"{rows} rows; {len(failures)} failed",
                    flush=True,
                )
    failure_path = PARQUET_DIR / "parse_failures.tsv"
    failure_path.write_text(
        "".join(f"{symbol}\t{error}\n" for symbol, error in failures),
        encoding="utf-8",
    )
    if failures:
        raise SystemExit(f"{len(failures)} symbols failed; see {failure_path}")


if __name__ == "__main__":
    main()
