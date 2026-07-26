#!/usr/bin/env python3
"""Create a read-only current order plan; this script cannot submit orders."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from engine import (  # noqa: E402
    BacktestConfig,
    capped_proportional_allocations,
    prepare_market,
)
from paths import RESULTS_DIR, ensure_directories  # noqa: E402

BASE = "https://data-api.binance.vision"
STABLE_BASES = {
    "AEUR",
    "BFDUSD",
    "BFUSD",
    "BUSD",
    "DAI",
    "EURI",
    "EUR",
    "FDUSD",
    "PAX",
    "RLUSD",
    "TUSD",
    "USDC",
    "USDE",
    "USDP",
    "USDS",
    "USD1",
    "UST",
    "USTC",
    "XUSD",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
WRAPPED_BASES = {"BETH", "BTCB", "WBETH", "WBNB", "WBTC", "WETH"}


def get_json(path: str, params: dict[str, str] | None = None) -> object:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    url = f"{BASE}{path}{query}"
    error: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.loads(response.read())
        except Exception as exc:
            error = exc
            if attempt + 1 < 5:
                time.sleep(2**attempt)
    raise RuntimeError(f"failed to retrieve {url}: {error}")


def filter_value(symbol: dict[str, object], kind: str, field: str) -> str:
    filters = symbol.get("filters", [])
    assert isinstance(filters, list)
    for item in filters:
        if isinstance(item, dict) and item.get("filterType") == kind:
            value = item.get(field)
            if value is not None:
                return str(value)
    return "0"


def allowed(symbol: dict[str, object]) -> bool:
    base = str(symbol.get("baseAsset", ""))
    return (
        symbol.get("quoteAsset") == "USDT"
        and symbol.get("status") == "TRADING"
        and bool(symbol.get("isSpotTradingAllowed", True))
        and base not in STABLE_BASES
        and base not in WRAPPED_BASES
        and not base.endswith(LEVERAGED_SUFFIXES)
    )


def fetch_klines(symbol: str) -> tuple[str, list[object] | None, str | None]:
    try:
        rows = get_json(
            "/api/v3/klines",
            {"symbol": symbol, "interval": "1d", "limit": "90"},
        )
        if not isinstance(rows, list):
            raise ValueError(f"unexpected kline payload: {type(rows)}")
        return symbol, rows, None
    except Exception as exc:
        return symbol, None, str(exc)


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a read-only LIMIT_MAKER order plan",
    )
    parser.add_argument("--equity", type=Decimal, required=True)
    parser.add_argument("--available-cash", type=Decimal)
    parser.add_argument(
        "--output",
        type=Path,
        default=RESULTS_DIR / "live_plan.json",
    )
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    if args.equity <= 0:
        parser.error("--equity must be positive")
    available = args.available_cash if args.available_cash is not None else args.equity
    if available < 0 or available > args.equity:
        parser.error("--available-cash must be between zero and equity")

    ensure_directories()
    exchange = get_json("/api/v3/exchangeInfo")
    if not isinstance(exchange, dict) or not isinstance(exchange.get("symbols"), list):
        raise SystemExit("unexpected exchangeInfo response")
    symbols = {
        str(item["symbol"]): item
        for item in exchange["symbols"]
        if isinstance(item, dict) and allowed(item)
    }
    print(f"fetching 90 daily bars for {len(symbols)} active symbols", flush=True)

    frames: list[pd.DataFrame] = []
    errors: dict[str, str] = {}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for completed, (symbol, rows, error) in enumerate(
            executor.map(fetch_klines, symbols),
            1,
        ):
            if error or rows is None:
                errors[symbol] = error or "missing rows"
                continue
            parsed = []
            for row in rows:
                if not isinstance(row, list) or len(row) < 9:
                    continue
                close_time = int(row[6])
                if close_time >= now_ms:
                    continue
                parsed.append(
                    {
                        "date": pd.to_datetime(int(row[0]), unit="ms", utc=True),
                        "symbol": symbol,
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "quote_volume": float(row[7]),
                    }
                )
            if parsed:
                frames.append(pd.DataFrame(parsed))
            if completed % 100 == 0 or completed == len(symbols):
                print(
                    f"{completed}/{len(symbols)} fetched; {len(errors)} errors",
                    flush=True,
                )
    if not frames:
        raise SystemExit("no current kline data retrieved")

    config = BacktestConfig()
    features = prepare_market(pd.concat(frames, ignore_index=True), config)
    latest_date = features["date"].max()
    latest = features.loc[features["date"].eq(latest_date)].set_index("symbol")
    candidates = latest.loc[latest["signal"]].copy()
    scores = {
        symbol: float(row["momentum"]) for symbol, row in candidates.iterrows()
    }
    budget = min(float(available), config.max_gross_fraction * float(args.equity))
    allocations = capped_proportional_allocations(
        scores,
        budget,
        config.max_symbol_fraction * float(args.equity),
    )

    orders: list[dict[str, object]] = []
    signal_finalized = latest_date + pd.Timedelta(days=1)
    cancel_at = signal_finalized + pd.Timedelta(days=config.order_life_days)
    for symbol, allocation in allocations.items():
        if allocation <= 0:
            continue
        row = candidates.loc[symbol]
        metadata = symbols[symbol]
        tick = Decimal(filter_value(metadata, "PRICE_FILTER", "tickSize"))
        step = Decimal(filter_value(metadata, "LOT_SIZE", "stepSize"))
        minimum_quantity = Decimal(filter_value(metadata, "LOT_SIZE", "minQty"))
        min_notional_text = filter_value(metadata, "NOTIONAL", "minNotional")
        if min_notional_text == "0":
            min_notional_text = filter_value(
                metadata,
                "MIN_NOTIONAL",
                "minNotional",
            )
        min_notional = Decimal(min_notional_text)
        raw_limit = Decimal(str(row["close"])) * Decimal(
            str(np_exp(-config.correction_sigma * float(row["sigma"])))
        )
        limit = floor_step(raw_limit, tick)
        fee_rate = Decimal(str(config.entry_maker_bps / 10_000))
        quantity = floor_step(
            Decimal(str(allocation)) / (limit * (1 + fee_rate)),
            step,
        )
        notional = limit * quantity
        if quantity < minimum_quantity or notional < min_notional:
            continue
        orders.append(
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "LIMIT_MAKER",
                "price": decimal_text(limit),
                "quantity": decimal_text(quantity),
                "reserved_usdt": float(notional * (1 + fee_rate)),
                "signal_date": str(latest_date.date()),
                "cancel_at_utc": cancel_at.isoformat(),
                "momentum": float(row["momentum"]),
                "momentum_percentile": float(row["momentum_percentile"]),
                "daily_sigma": float(row["sigma"]),
                "volume_rank": int(row["volume_rank"]),
            }
        )

    payload = {
        "mode": "READ_ONLY_DRY_RUN",
        "execution_authorized": False,
        "deployment_status": "BLOCKED_BASELINE_FAILED_OUT_OF_SAMPLE",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "signal_date": str(latest_date.date()),
        "equity_usdt_input": float(args.equity),
        "available_cash_usdt_input": float(available),
        "orders": orders,
        "data_errors": errors,
        "required_before_submission": [
            "reconcile account balances, positions, and existing open orders",
            "refresh exchangeInfo and revalidate every order filter",
            "confirm the daily bars are finalized and timestamps are current",
            "apply the account's actual maker commission",
            "persist client order IDs and cancellation state",
            "obtain manual approval",
        ],
        "note": "This file is an order plan. The script has no signing or order endpoint code.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"{len(orders)} candidate orders -> {args.output}")


def np_exp(value: float) -> float:
    # Kept local so the live tool does not need to expose numpy values to JSON.
    import math

    return math.exp(value)


if __name__ == "__main__":
    main()
