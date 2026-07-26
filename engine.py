"""Pure feature, portfolio-simulation, and performance functions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import sqrt
from typing import Literal

import numpy as np
import pandas as pd

Mode = Literal[
    "full",
    "momentum_only",
    "positive_momentum_correction",
    "passive_limit",
]


@dataclass(frozen=True)
class BacktestConfig:
    volume_count: int = 50
    volume_lookback: int = 30
    momentum_lookback: int = 20
    volatility_lookback: int = 20
    momentum_percentile: float = 0.80
    correction_sigma: float = 1.0
    order_life_days: int = 3
    holding_days: int = 5
    max_symbol_fraction: float = 0.10
    max_gross_fraction: float = 1.0
    initial_equity: float = 100_000.0
    entry_maker_bps: float = 10.0
    entry_taker_bps: float = 10.0
    exit_taker_bps: float = 10.0
    market_entry_slippage_bps: float = 5.0
    exit_slippage_bps: float = 5.0
    start: str = "2020-01-01"
    end: str = "2026-06-30"
    # Optional market-wide regime gate. The source post does not define one; an
    # earlier post by the same author mentions "a simple regime filter" without
    # saying what it is, so any setting here is a reconstruction hypothesis and
    # is labelled as such wherever it is reported.
    regime_symbol: str = ""
    regime_kind: Literal["ema", "sma"] = "sma"
    regime_span: int = 0
    regime_confirm_days: int = 1

    def validate(self) -> None:
        if min(
            self.volume_count,
            self.volume_lookback,
            self.momentum_lookback,
            self.volatility_lookback,
            self.order_life_days,
            self.holding_days,
        ) <= 0:
            raise ValueError("lookbacks, counts, and holding periods must be positive")
        if not 0 < self.momentum_percentile <= 1:
            raise ValueError("momentum_percentile must be in (0, 1]")
        if self.correction_sigma < 0:
            raise ValueError("correction_sigma must be non-negative")
        if not 0 < self.max_symbol_fraction <= self.max_gross_fraction <= 1:
            raise ValueError("require 0 < symbol cap <= gross cap <= 1")
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        if self.regime_span < 0 or self.regime_confirm_days < 1:
            raise ValueError("regime span must be non-negative and confirm >= 1")
        if self.regime_span and not self.regime_symbol:
            raise ValueError("regime_span requires regime_symbol")
        if self.regime_kind not in {"ema", "sma"}:
            raise ValueError("regime_kind must be 'ema' or 'sma'")


@dataclass
class Order:
    symbol: str
    signal_date: pd.Timestamp
    active_date: pd.Timestamp
    expiry_date: pd.Timestamp
    limit: float
    score: float
    reserved: float
    kind: Literal["limit", "market"]
    momentum: float
    sigma: float
    volume_rank: float


@dataclass
class Position:
    symbol: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    target_exit_date: pd.Timestamp
    entry_price: float
    quantity: float
    entry_cost: float
    entry_fee: float
    reserved: float
    momentum: float
    sigma: float
    correction_sigma: float
    volume_rank: float
    # Populated only under hourly execution, where the fill moment is observed
    # and the exit is scheduled exactly H*24h later rather than at a day open.
    entry_time: pd.Timestamp | None = None
    target_exit_time: pd.Timestamp | None = None


def config_dict(config: BacktestConfig) -> dict[str, object]:
    return asdict(config)


def _contiguous_segment(frame: pd.DataFrame) -> pd.Series:
    gaps = frame.groupby("symbol", sort=False)["date"].diff().dt.days
    return gaps.ne(1).groupby(frame["symbol"], sort=False).cumsum()


def prepare_market(
    frame: pd.DataFrame,
    config: BacktestConfig,
) -> pd.DataFrame:
    """Compute point-in-time universe and momentum features."""
    config.validate()
    required = {
        "date",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "quote_volume",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")

    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"], utc=True).dt.normalize()
    numeric = ["open", "high", "low", "close", "quote_volume"]
    data[numeric] = data[numeric].apply(pd.to_numeric, errors="coerce")
    data = (
        data.dropna(subset=["date", "symbol", *numeric])
        .loc[lambda x: (x[numeric] >= 0).all(axis=1) & x["close"].gt(0)]
        .sort_values(["symbol", "date"])
        .drop_duplicates(["symbol", "date"], keep="last")
        .reset_index(drop=True)
    )
    data["segment"] = _contiguous_segment(data)
    group_keys = ["symbol", "segment"]
    groups = data.groupby(group_keys, sort=False, group_keys=False)

    data["log_close"] = np.log(data["close"])
    data["return"] = groups["log_close"].diff()
    data["volume_sum"] = groups["quote_volume"].transform(
        lambda x: x.rolling(
            config.volume_lookback,
            min_periods=config.volume_lookback,
        ).sum()
    )
    data["sigma"] = groups["return"].transform(
        lambda x: x.rolling(
            config.volatility_lookback,
            min_periods=config.volatility_lookback,
        ).std(ddof=1)
    )
    lagged = groups["log_close"].shift(config.momentum_lookback)
    data["momentum"] = (
        (data["log_close"] - lagged)
        / (data["sigma"] * sqrt(config.momentum_lookback))
    )
    data.loc[~np.isfinite(data["momentum"]), "momentum"] = np.nan

    eligible = data["volume_sum"].notna() & data["momentum"].notna()
    data["volume_rank"] = np.nan
    data.loc[eligible, "volume_rank"] = (
        data.loc[eligible]
        .groupby("date", sort=False)["volume_sum"]
        .rank(method="first", ascending=False)
    )
    data["in_universe"] = data["volume_rank"].le(config.volume_count)
    data["momentum_percentile"] = np.nan
    in_universe = data["in_universe"]
    data.loc[in_universe, "momentum_percentile"] = (
        data.loc[in_universe]
        .groupby("date", sort=False)["momentum"]
        .rank(method="average", pct=True)
    )
    data["signal"] = (
        data["in_universe"]
        & data["momentum"].gt(0)
        & data["momentum_percentile"].ge(config.momentum_percentile)
    )
    data["regime_on"] = regime_series(data, config)
    return data.drop(columns=["log_close", "segment"])


def regime_series(data: pd.DataFrame, config: BacktestConfig) -> pd.Series:
    """Market-wide on/off gate, aligned to every row by date.

    The gate reads the reference symbol's own closes through day t only: an
    average is taken over closes up to and including t, and the confirmation
    window ends at t. A signal stamped t therefore uses nothing later than its
    own close, exactly like the momentum features beside it.
    """
    if not config.regime_span or not config.regime_symbol:
        return pd.Series(True, index=data.index)
    reference = (
        data.loc[data["symbol"].eq(config.regime_symbol), ["date", "close"]]
        .drop_duplicates("date")
        .sort_values("date")
        .set_index("date")["close"]
    )
    if reference.empty:
        raise ValueError(f"regime symbol {config.regime_symbol} absent from the market")
    average = (
        reference.ewm(span=config.regime_span, adjust=False).mean()
        if config.regime_kind == "ema"
        else reference.rolling(config.regime_span, min_periods=config.regime_span).mean()
    )
    above = reference.gt(average)
    confirmed = (
        above.rolling(config.regime_confirm_days, min_periods=config.regime_confirm_days)
        .sum()
        .eq(config.regime_confirm_days)
    )
    # Before the reference symbol has enough history the gate is closed, which
    # keeps the rule from trading on an unformed average.
    return data["date"].map(confirmed).fillna(False).astype(bool)


def _candidate_mask(day: pd.DataFrame, mode: Mode) -> pd.Series:
    gate = day["regime_on"] if "regime_on" in day else True
    if mode in {"full", "momentum_only"}:
        return day["signal"] & gate
    if mode == "positive_momentum_correction":
        return day["in_universe"] & day["momentum"].gt(0) & gate
    if mode == "passive_limit":
        return day["in_universe"] & gate
    raise ValueError(f"unknown mode: {mode}")


def _still_valid(row: pd.Series, mode: Mode) -> bool:
    """A resting order is pulled when the gate closes, not just when the
    coin leaves the signal set: standing down means standing down."""
    if "regime_on" in row and not bool(row["regime_on"]):
        return False
    if mode in {"full", "momentum_only"}:
        return bool(row["signal"])
    if mode == "positive_momentum_correction":
        return bool(row["in_universe"] and row["momentum"] > 0)
    return bool(row["in_universe"])


def capped_proportional_allocations(
    scores: dict[str, float],
    total: float,
    cap: float,
) -> dict[str, float]:
    """Allocate proportionally, redistributing amounts above a per-name cap."""
    if total <= 0 or cap <= 0 or not scores:
        return {key: 0.0 for key in scores}
    remaining = {key: max(float(value), 0.0) for key, value in scores.items()}
    allocations = {key: 0.0 for key in scores}
    budget = min(total, cap * len(scores))

    while remaining and budget > 1e-10:
        score_sum = sum(remaining.values())
        weights = (
            {key: value / score_sum for key, value in remaining.items()}
            if score_sum > 0
            else {key: 1 / len(remaining) for key in remaining}
        )
        capped: list[str] = []
        round_budget = budget
        for key, weight in weights.items():
            room = cap - allocations[key]
            proposed = round_budget * weight
            added = min(room, proposed)
            allocations[key] += added
            budget -= added
            if room - added <= 1e-10:
                capped.append(key)
        for key in capped:
            remaining.pop(key, None)
        if not capped:
            break
    return allocations


def simulate(
    market: pd.DataFrame,
    config: BacktestConfig,
    mode: Mode = "full",
    hourly: object | None = None,
    funding: object | None = None,
) -> dict[str, pd.DataFrame | dict[str, object]]:
    """Run a cash-only event simulation from daily signal bars.

    With `hourly=None` the execution model is the daily-bar proxy: a limit
    fills when the daily low trades through it (low < limit) at the resting
    limit, and the exit takes the open of the target date. The fill moment
    inside the day is unobserved, so the realized holding period is uncertain
    by up to a day, which is tolerable at a five-day hold and fatal at a
    one-day hold.

    With an `HourlyIndex`, the fill is the first hourly bar whose low trades
    through the limit and the exit is the hourly open at exactly
    `fill + holding_days*24h`. Fill timing is then resolved to the hour, so the
    holding period is the rule's holding period. Neither model reconstructs
    queue position, partial fills, spread, or depth.
    """
    config.validate()
    start = pd.Timestamp(config.start, tz="UTC")
    end = pd.Timestamp(config.end, tz="UTC")
    data = market.loc[
        market["date"].between(
            start - pd.Timedelta(days=max(config.order_life_days, 1)),
            end,
        )
    ].copy()
    data = data.sort_values(["date", "symbol"]).reset_index(drop=True)
    dates = sorted(date for date in data["date"].unique() if start <= date <= end)
    daily = {date: group.set_index("symbol") for date, group in data.groupby("date")}
    cash = config.initial_equity
    reserved = 0.0
    orders: dict[str, Order] = {}
    positions: dict[str, Position] = {}
    last_marks: dict[str, float] = {}
    trades: list[dict[str, object]] = []
    order_log: list[dict[str, object]] = []
    equity_rows: list[dict[str, object]] = []
    counters = {
        "orders": 0,
        "fills": 0,
        "cancels_expired": 0,
        "cancels_signal": 0,
        "cancels_missing": 0,
        "forced_exits": 0,
        "delayed_exits": 0,
    }

    maker_rate = config.entry_maker_bps / 10_000
    taker_rate = config.entry_taker_bps / 10_000
    market_slippage = config.market_entry_slippage_bps / 10_000
    exit_rate = config.exit_taker_bps / 10_000
    exit_slippage = config.exit_slippage_bps / 10_000

    for date in dates:
        day = daily[date]
        day_end = date + pd.Timedelta(days=1)
        for symbol, row in day.iterrows():
            last_marks[symbol] = float(row["close"])

        # Scheduled exits run before new fills: at the day's open under the
        # daily proxy, at the scheduled hour under hourly execution.
        for symbol, position in list(positions.items()):
            if hourly is None:
                if date < position.target_exit_date:
                    continue
                quote = (
                    (float(day.loc[symbol, "open"]), date)
                    if symbol in day.index
                    else None
                )
            else:
                if position.target_exit_time >= day_end:
                    continue
                quote = hourly.open_at(symbol, position.target_exit_time)
            if quote is None:
                trades.append(
                    {
                        **asdict(position),
                        "exit_date": date,
                        "exit_price": 0.0,
                        "exit_fee": 0.0,
                        "pnl": -position.entry_cost,
                        "return_on_reserved": -position.entry_cost
                        / position.reserved,
                        "holding_days_actual": (date - position.entry_date).days,
                        "holding_hours_actual": np.nan,
                        "exit_reason": "missing_exit_zero_recovery",
                        "mode": mode,
                    }
                )
                counters["forced_exits"] += 1
                positions.pop(symbol)
                continue
            raw_price, exit_moment = quote
            exit_price = raw_price * (1 - exit_slippage)
            exit_notional = position.quantity * exit_price
            exit_fee = exit_notional * exit_rate
            entry_moment = position.entry_time or position.entry_date
            # A long perpetual pays funding while the position is open; spot
            # has no equivalent, so it is charged only when a funding series
            # is supplied.
            funding_cost = (
                funding.cost(
                    symbol, entry_moment, exit_moment, position.quantity, hourly
                )
                if funding is not None and hourly is not None
                else 0.0
            )
            cash += exit_notional - exit_fee - funding_cost
            pnl = exit_notional - exit_fee - funding_cost - position.entry_cost
            held_hours = (exit_moment - entry_moment).total_seconds() / 3600
            if hourly is not None and exit_moment > position.target_exit_time:
                counters["delayed_exits"] += 1
            trades.append(
                {
                    **asdict(position),
                    "exit_date": exit_moment,
                    "exit_price": exit_price,
                    "exit_fee": exit_fee,
                    "funding_cost": funding_cost,
                    "pnl": pnl,
                    "return_on_reserved": pnl / position.reserved,
                    "holding_days_actual": (
                        exit_moment.normalize() - position.entry_date
                    ).days,
                    "holding_hours_actual": held_hours,
                    "exit_reason": "scheduled",
                    "mode": mode,
                }
            )
            positions.pop(symbol)

        # Orders placed at prior closes are active during this day.
        for symbol, order in list(orders.items()):
            if date < order.active_date or date > order.expiry_date:
                continue
            if symbol not in day.index:
                continue
            row = day.loc[symbol]
            entry_moment = date
            if order.kind == "limit":
                entry_price = order.limit
                fee_rate = maker_rate
                if hourly is None:
                    filled = float(row["low"]) < order.limit
                else:
                    hit = hourly.first_trade_through(
                        symbol, date, day_end, order.limit
                    )
                    filled = hit is not None
                    if filled:
                        entry_moment = hit
            else:
                filled = True
                entry_price = float(row["open"]) * (1 + market_slippage)
                fee_rate = taker_rate
            if not filled:
                continue

            quantity = order.reserved / (entry_price * (1 + fee_rate))
            entry_notional = quantity * entry_price
            entry_fee = entry_notional * fee_rate
            entry_cost = entry_notional + entry_fee
            cash -= entry_cost
            reserved -= order.reserved
            hold = pd.Timedelta(days=config.holding_days)
            positions[symbol] = Position(
                symbol=symbol,
                signal_date=order.signal_date,
                entry_date=date,
                target_exit_date=date + hold,
                entry_time=entry_moment if hourly is not None else None,
                target_exit_time=(
                    entry_moment + hold if hourly is not None else None
                ),
                entry_price=entry_price,
                quantity=quantity,
                entry_cost=entry_cost,
                entry_fee=entry_fee,
                reserved=order.reserved,
                momentum=order.momentum,
                sigma=order.sigma,
                correction_sigma=config.correction_sigma,
                volume_rank=order.volume_rank,
            )
            counters["fills"] += 1
            order_log.append(
                {
                    **asdict(order),
                    "status": "filled",
                    "status_date": date,
                    "entry_price": entry_price,
                }
            )
            orders.pop(symbol)

        # Cancel stale or invalid orders after they had today's fill chance.
        for symbol, order in list(orders.items()):
            reason: str | None = None
            if date >= order.expiry_date:
                reason = "expired"
                counters["cancels_expired"] += 1
            elif symbol not in day.index:
                reason = "missing"
                counters["cancels_missing"] += 1
            elif not _still_valid(day.loc[symbol], mode):
                reason = "signal"
                counters["cancels_signal"] += 1
            if reason:
                reserved -= order.reserved
                order_log.append(
                    {
                        **asdict(order),
                        "status": f"canceled_{reason}",
                        "status_date": date,
                        "entry_price": np.nan,
                    }
                )
                orders.pop(symbol)

        marked_positions = sum(
            position.quantity * last_marks.get(symbol, position.entry_price)
            for symbol, position in positions.items()
        )
        equity = cash + marked_positions

        # Create new orders after the close using today's finalized features.
        candidates = day.loc[_candidate_mask(day, mode)].copy()
        candidates = candidates.loc[
            ~candidates.index.isin(set(orders) | set(positions))
        ]
        available = max(cash - reserved, 0.0)
        gross_room = max(
            config.max_gross_fraction * equity - marked_positions - reserved,
            0.0,
        )
        budget = min(available, gross_room)
        if not candidates.empty and budget > 0:
            scores = {
                symbol: (
                    max(float(row["momentum"]), 0.0)
                    if mode != "passive_limit"
                    else 1.0
                )
                for symbol, row in candidates.iterrows()
            }
            allocations = capped_proportional_allocations(
                scores,
                budget,
                config.max_symbol_fraction * equity,
            )
            for symbol, allocation in allocations.items():
                if allocation <= 1e-8:
                    continue
                row = candidates.loc[symbol]
                kind: Literal["limit", "market"] = (
                    "market" if mode == "momentum_only" else "limit"
                )
                limit = float(row["close"]) * np.exp(
                    -config.correction_sigma * float(row["sigma"])
                )
                order = Order(
                    symbol=symbol,
                    signal_date=date,
                    active_date=date + pd.Timedelta(days=1),
                    expiry_date=date + pd.Timedelta(days=config.order_life_days),
                    limit=limit,
                    score=scores[symbol],
                    reserved=allocation,
                    kind=kind,
                    momentum=float(row["momentum"]),
                    sigma=float(row["sigma"]),
                    volume_rank=float(row["volume_rank"]),
                )
                orders[symbol] = order
                reserved += allocation
                counters["orders"] += 1

        marked_positions = sum(
            position.quantity * last_marks.get(symbol, position.entry_price)
            for symbol, position in positions.items()
        )
        equity = cash + marked_positions
        equity_rows.append(
            {
                "date": date,
                "equity": equity,
                "cash": cash,
                "reserved": reserved,
                "invested": marked_positions,
                "open_positions": len(positions),
                "open_orders": len(orders),
                "mode": mode,
            }
        )

    # Close remaining positions at the final observable close.
    if dates:
        final_date = dates[-1]
        day = daily[final_date]
        for symbol, position in list(positions.items()):
            mark = (
                float(day.loc[symbol, "close"])
                if symbol in day.index
                else 0.0
            )
            exit_price = mark * (1 - exit_slippage)
            exit_notional = position.quantity * exit_price
            exit_fee = exit_notional * exit_rate
            close_out = final_date + pd.Timedelta(days=1)
            funding_cost = (
                funding.cost(
                    symbol,
                    position.entry_time or position.entry_date,
                    close_out,
                    position.quantity,
                    hourly,
                )
                if funding is not None and hourly is not None
                else 0.0
            )
            cash += exit_notional - exit_fee - funding_cost
            pnl = exit_notional - exit_fee - funding_cost - position.entry_cost
            trades.append(
                {
                    **asdict(position),
                    "exit_date": final_date,
                    "exit_price": exit_price,
                    "exit_fee": exit_fee,
                    "funding_cost": funding_cost,
                    "pnl": pnl,
                    "return_on_reserved": pnl / position.reserved,
                    "holding_days_actual": (final_date - position.entry_date).days,
                    # The close-out marks the final day's close, so the
                    # liquidation moment is the end of that day.
                    "holding_hours_actual": (
                        final_date
                        + pd.Timedelta(days=1)
                        - (position.entry_time or position.entry_date)
                    ).total_seconds()
                    / 3600,
                    "exit_reason": (
                        "sample_end"
                        if symbol in day.index
                        else "sample_end_missing_zero_recovery"
                    ),
                    "mode": mode,
                }
            )
            positions.pop(symbol)
        if equity_rows:
            equity_rows[-1]["equity"] = cash
            equity_rows[-1]["cash"] = cash
            equity_rows[-1]["invested"] = 0.0
            equity_rows[-1]["open_positions"] = 0

    equity_frame = pd.DataFrame(equity_rows)
    if not equity_frame.empty:
        equity_frame["daily_return"] = equity_frame["equity"].pct_change().fillna(0)
        equity_frame["peak"] = equity_frame["equity"].cummax()
        equity_frame["drawdown"] = equity_frame["equity"] / equity_frame["peak"] - 1

    return {
        "equity": equity_frame,
        "trades": pd.DataFrame(trades),
        "orders": pd.DataFrame(order_log),
        "counters": counters,
    }


def expected_shortfall(values: pd.Series, level: float) -> float:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty:
        return float("nan")
    cutoff = clean.quantile(1 - level)
    return float(clean.loc[clean <= cutoff].mean())


def performance_summary(
    result: dict[str, pd.DataFrame | dict[str, object]],
    initial_equity: float,
) -> dict[str, float | int | str]:
    equity = result["equity"]
    trades = result["trades"]
    counters = result["counters"]
    assert isinstance(equity, pd.DataFrame)
    assert isinstance(trades, pd.DataFrame)
    assert isinstance(counters, dict)
    if equity.empty:
        return {"error": "no equity observations"}

    start = equity["date"].iloc[0]
    end = equity["date"].iloc[-1]
    years = max((end - start).days / 365.25, 1 / 365.25)
    final = float(equity["equity"].iloc[-1])
    returns = equity["daily_return"]
    std = float(returns.std(ddof=1))
    drawdown = equity["drawdown"]

    underwater = drawdown.lt(0)
    groups = underwater.ne(underwater.shift()).cumsum()
    durations = underwater.groupby(groups).sum()
    max_duration = int(durations.max()) if not durations.empty else 0

    pnl = trades["pnl"] if not trades.empty else pd.Series(dtype=float)
    trade_returns = (
        trades["return_on_reserved"] if not trades.empty else pd.Series(dtype=float)
    )
    wins = pnl.loc[pnl > 0].sum()
    losses = -pnl.loc[pnl < 0].sum()
    fees = (
        trades["entry_fee"].sum() + trades["exit_fee"].sum()
        if not trades.empty
        else 0.0
    )
    funding_paid = (
        trades["funding_cost"].sum()
        if not trades.empty and "funding_cost" in trades
        else 0.0
    )
    summary: dict[str, float | int | str] = {
        "start": str(start.date()),
        "end": str(end.date()),
        "initial_equity": initial_equity,
        "final_equity": final,
        "total_return": final / initial_equity - 1,
        "cagr": (final / initial_equity) ** (1 / years) - 1
        if final > 0
        else -1.0,
        "annualized_volatility": std * sqrt(365),
        "sharpe_zero_cash": float(returns.mean() / std * sqrt(365))
        if std > 0
        else float("nan"),
        "max_drawdown": float(drawdown.min()),
        "max_drawdown_days": max_duration,
        "daily_es_90": expected_shortfall(returns, 0.90),
        "daily_es_95": expected_shortfall(returns, 0.95),
        "daily_es_99": expected_shortfall(returns, 0.99),
        "trades": int(len(trades)),
        "hit_rate": float((pnl > 0).mean()) if len(pnl) else float("nan"),
        "mean_trade_return": float(trade_returns.mean())
        if len(trade_returns)
        else float("nan"),
        "median_trade_return": float(trade_returns.median())
        if len(trade_returns)
        else float("nan"),
        "trade_es_95": expected_shortfall(trade_returns, 0.95),
        "profit_factor": float(wins / losses) if losses > 0 else float("inf"),
        "total_fees": float(fees),
        # Positive means the book paid funding on net, negative means it
        # received. Always zero unless a funding series was supplied.
        "total_funding": float(funding_paid),
        "average_invested_fraction": float(
            (equity["invested"] / equity["equity"]).mean()
        ),
        "average_reserved_fraction": float(
            (equity["reserved"] / equity["equity"]).mean()
        ),
        "orders": int(counters.get("orders", 0)),
        "fills": int(counters.get("fills", 0)),
        "fill_rate": (
            float(counters.get("fills", 0) / counters["orders"])
            if counters.get("orders", 0)
            else float("nan")
        ),
        "forced_exits": int(counters.get("forced_exits", 0)),
        "delayed_exit_days": int(counters.get("delayed_exits", 0)),
    }
    return summary
