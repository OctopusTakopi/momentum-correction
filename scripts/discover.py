#!/usr/bin/env python3
"""Discover all historical Binance Spot USDT daily-kline archives."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from paths import DATA_DIR, ensure_directories  # noqa: E402

BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
PREFIX = "data/spot/monthly/klines/"

STABLE_BASES = {
    "AEUR",
    "BFDUSD",
    "BFUSD",
    "BKRW",
    "BRL",
    "BUSD",
    "DAI",
    "EURI",
    "EUR",
    "FDUSD",
    "GBP",
    "GUSD",
    "IDRT",
    "JPY",
    "PAX",
    "RLUSD",
    "RUB",
    "SUSD",
    "TUSD",
    "UAH",
    "USDC",
    "USDE",
    "USDP",
    "USDS",
    "USD1",
    "UST",
    "USTC",
    "VAI",
    "XUSD",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
WRAPPED_BASES = {"BETH", "BTCB", "WBETH", "WBNB", "WBTC", "WETH"}


def fetch_xml(params: dict[str, str], retries: int = 5) -> ET.Element:
    url = f"{BUCKET}?{urllib.parse.urlencode(params)}"
    error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return ET.fromstring(response.read())
        except Exception as exc:
            error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"failed to list {url}: {error}")


def list_items(prefix: str, delimiter: str | None = None) -> list[str]:
    marker = ""
    output: list[str] = []
    while True:
        params = {"prefix": prefix, "max-keys": "1000"}
        if delimiter:
            params["delimiter"] = delimiter
        if marker:
            params["marker"] = marker
        root = fetch_xml(params)
        if delimiter:
            page = [
                node.findtext(NS + "Prefix")
                for node in root.findall(NS + "CommonPrefixes")
            ]
        else:
            page = [
                node.findtext(NS + "Key") for node in root.findall(NS + "Contents")
            ]
        page = [value for value in page if value]
        output.extend(page)
        if root.findtext(NS + "IsTruncated", "false") != "true":
            return output
        next_marker = root.findtext(NS + "NextMarker")
        marker = next_marker or (page[-1] if page else "")
        if not marker:
            raise RuntimeError(f"truncated empty listing for {prefix}")


def allowed_symbol(symbol: str) -> bool:
    if not symbol.endswith("USDT"):
        return False
    base = symbol[:-4]
    return (
        base not in STABLE_BASES
        and base not in WRAPPED_BASES
        and not base.endswith(LEVERAGED_SUFFIXES)
        and base != "USDT"
    )


def month_from_key(key: str) -> str:
    return Path(key).stem[-7:]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2019-10")
    parser.add_argument(
        "--end",
        default=None,
        help="last complete monthly archive, YYYY-MM",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DATA_DIR / "manifest.txt",
    )
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()

    ensure_directories()
    if args.end is None:
        today = date.today()
        year = today.year if today.month > 1 else today.year - 1
        month = today.month - 1 if today.month > 1 else 12
        args.end = f"{year:04d}-{month:02d}"
    if args.start > args.end:
        parser.error("--start must not be after --end")

    prefixes = list_items(PREFIX, delimiter="/")
    symbols = sorted(
        {
            prefix.removeprefix(PREFIX).strip("/")
            for prefix in prefixes
            if allowed_symbol(prefix.removeprefix(PREFIX).strip("/"))
        }
    )
    print(f"{len(symbols)} historical non-stable USDT spot symbols", flush=True)

    def per_symbol(symbol: str) -> tuple[str, list[str]]:
        prefix = f"{PREFIX}{symbol}/1d/"
        keys = [
            key
            for key in list_items(prefix)
            if key.endswith(".zip")
            and args.start <= month_from_key(key) <= args.end
        ]
        return symbol, sorted(keys)

    selected: list[str] = []
    coverage: dict[str, dict[str, object]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for completed, (symbol, keys) in enumerate(
            executor.map(per_symbol, symbols),
            1,
        ):
            selected.extend(keys)
            months = [month_from_key(key) for key in keys]
            coverage[symbol] = {
                "archives": len(keys),
                "first": min(months) if months else None,
                "last": max(months) if months else None,
            }
            if completed % 100 == 0 or completed == len(symbols):
                print(f"{completed}/{len(symbols)} symbols listed", flush=True)

    if not selected:
        raise SystemExit("no matching archives discovered")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(f"{key}\n" for key in sorted(selected)),
        encoding="utf-8",
    )
    active_coverage = {k: v for k, v in coverage.items() if v["archives"]}
    metadata = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "requested_start": args.start,
        "requested_end": args.end,
        "symbols": len(active_coverage),
        "archives": len(selected),
        "stable_bases_excluded": sorted(STABLE_BASES),
        "wrapped_bases_excluded": sorted(WRAPPED_BASES),
        "leveraged_suffixes_excluded": list(LEVERAGED_SUFFIXES),
        "coverage": active_coverage,
    }
    args.output.with_suffix(".json").write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "symbols": metadata["symbols"],
                "archives": metadata["archives"],
                "start": args.start,
                "end": args.end,
                "manifest": str(args.output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
