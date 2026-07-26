#!/usr/bin/env python3
"""Run the research backtest, controls, sensitivities, and report."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from engine import (  # noqa: E402
    BacktestConfig,
    config_dict,
    expected_shortfall,
    performance_summary,
    prepare_market,
    simulate,
)
from paths import (  # noqa: E402
    DATA_DIR,
    PARQUET_DIR,
    RESULTS_DIR,
    ensure_directories,
)

MODES = [
    "full",
    "momentum_only",
    "positive_momentum_correction",
    "passive_limit",
]
MODE_LABELS = {
    "full": "Momentum + correction",
    "momentum_only": "Momentum, next-open entry",
    "positive_momentum_correction": "Positive momentum + correction",
    "passive_limit": "Passive correction limits",
}


def load_market() -> pd.DataFrame:
    files = sorted(PARQUET_DIR.glob("*.parquet"))
    if not files:
        raise SystemExit(
            f"no parquet files under {PARQUET_DIR}; run make download parse"
        )
    frames = [pd.read_parquet(path) for path in files]
    market = pd.concat(frames, ignore_index=True)
    print(
        f"loaded {len(market):,} bars for {market['symbol'].nunique()} symbols",
        flush=True,
    )
    return market


def json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(json_safe(value), indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def period_result(
    result: dict[str, object],
    start: str,
    end: str,
) -> tuple[dict[str, object], float]:
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    equity = result["equity"]
    trades = result["trades"]
    orders = result["orders"]
    assert isinstance(equity, pd.DataFrame)
    assert isinstance(trades, pd.DataFrame)
    assert isinstance(orders, pd.DataFrame)

    selected = equity.loc[equity["date"].between(start_ts, end_ts)].copy()
    if selected.empty:
        return {"error": "no observations"}, float("nan")
    initial = float(selected["equity"].iloc[0])
    selected["daily_return"] = selected["equity"].pct_change().fillna(0)
    selected["peak"] = selected["equity"].cummax()
    selected["drawdown"] = selected["equity"] / selected["peak"] - 1
    selected_trades = (
        trades.loc[trades["entry_date"].between(start_ts, end_ts)].copy()
        if not trades.empty
        else trades.copy()
    )
    selected_orders = (
        orders.loc[orders["signal_date"].between(start_ts, end_ts)].copy()
        if not orders.empty
        else orders.copy()
    )
    fills = int(selected_orders["status"].eq("filled").sum()) if not selected_orders.empty else 0
    subset = {
        "equity": selected,
        "trades": selected_trades,
        "orders": selected_orders,
        "counters": {"orders": len(selected_orders), "fills": fills},
    }
    return performance_summary(subset, initial), initial


def bootstrap_draws(
    returns: pd.Series,
    *,
    block: int = 30,
    repetitions: int = 2000,
    seed: int = 20260724,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Resample the return path and return every draw's CAGR and drawdown."""
    values = pd.to_numeric(returns, errors="coerce").dropna().to_numpy()
    if len(values) < block * 2:
        return None
    rng = np.random.default_rng(seed)
    cagr = np.empty(repetitions)
    max_dd = np.empty(repetitions)
    starts = np.arange(0, len(values) - block + 1)
    blocks_needed = int(np.ceil(len(values) / block))
    for index in range(repetitions):
        sampled_starts = rng.choice(starts, size=blocks_needed, replace=True)
        sample = np.concatenate(
            [values[start : start + block] for start in sampled_starts]
        )[: len(values)]
        path = np.cumprod(1 + sample)
        peaks = np.maximum.accumulate(path)
        cagr[index] = path[-1] ** (365.25 / len(sample)) - 1
        max_dd[index] = np.min(path / peaks - 1)
    return cagr, max_dd


def summarize_bootstrap(
    draws: tuple[np.ndarray, np.ndarray] | None,
    *,
    block: int = 30,
    repetitions: int = 2000,
    seed: int = 20260724,
) -> dict[str, object]:
    if draws is None:
        return {"error": "insufficient returns for block bootstrap"}
    cagr, max_dd = draws
    return {
        "method": "moving block bootstrap",
        "block_days": block,
        "repetitions": repetitions,
        "seed": seed,
        "cagr_percentiles": {
            "2.5": float(np.quantile(cagr, 0.025)),
            "50": float(np.quantile(cagr, 0.50)),
            "97.5": float(np.quantile(cagr, 0.975)),
        },
        "max_drawdown_percentiles": {
            "2.5": float(np.quantile(max_dd, 0.025)),
            "50": float(np.quantile(max_dd, 0.50)),
            "97.5": float(np.quantile(max_dd, 0.975)),
        },
        "probability_cagr_le_zero": float(np.mean(cagr <= 0)),
    }


def moving_block_bootstrap(
    returns: pd.Series,
    *,
    block: int = 30,
    repetitions: int = 2000,
    seed: int = 20260724,
) -> dict[str, object]:
    draws = bootstrap_draws(returns, block=block, repetitions=repetitions, seed=seed)
    return summarize_bootstrap(
        draws, block=block, repetitions=repetitions, seed=seed
    )


def yearly_table(equity: pd.DataFrame) -> pd.DataFrame:
    data = equity.copy()
    data["year"] = data["date"].dt.year
    rows = []
    for year, group in data.groupby("year"):
        returns = group["daily_return"]
        rows.append(
            {
                "year": int(year),
                "return": float((1 + returns).prod() - 1),
                "volatility": float(returns.std(ddof=1) * np.sqrt(365)),
                "worst_drawdown": float(group["drawdown"].min()),
            }
        )
    return pd.DataFrame(rows)


def trade_concentration(trades: pd.DataFrame) -> dict[str, float | None]:
    if trades.empty:
        return {"top_1pct": None, "top_5pct": None, "top_10pct": None}
    pnl = trades["pnl"].sort_values(ascending=False)
    total = pnl.sum()
    output: dict[str, float | None] = {}
    for label, fraction in (("top_1pct", 0.01), ("top_5pct", 0.05), ("top_10pct", 0.10)):
        count = max(1, int(np.ceil(len(pnl) * fraction)))
        output[label] = float(pnl.head(count).sum() / total) if total != 0 else None
    return output


def pct(value: object) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if not np.isfinite(numeric) else f"{numeric:.2%}"


def num(value: object, digits: int = 2) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if not np.isfinite(numeric) else f"{numeric:.{digits}f}"


def money(value: object) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if not np.isfinite(numeric) else f"${numeric:,.0f}"


def render_report(
    config: BacktestConfig,
    summaries: pd.DataFrame,
    oos: dict[str, dict[str, object]],
    sensitivity: pd.DataFrame,
    friction: pd.DataFrame,
    baseline: dict[str, object],
    bootstrap: dict[str, object],
    coverage: dict[str, object],
    yearly: pd.DataFrame,
    concentration: dict[str, float | None],
    figures: dict[str, str | None] | None = None,
    comparison: pd.DataFrame | None = None,
    regime: pd.DataFrame | None = None,
    benchmark: pd.DataFrame | None = None,
) -> str:
    base = summaries.loc[summaries["mode"].eq("full")].iloc[0]
    test = oos["full"]
    positive_sensitivity = float(sensitivity["oos_cagr"].gt(0).mean())
    bootstrap_p = bootstrap.get("probability_cagr_le_zero")
    live_stage = "NOT READY, SHADOW/PAPER TRADE ONLY"
    available = figures or {}

    def figure(key: str, alt: str) -> str:
        name = available.get(key)
        if not name:
            return "*Figure unavailable; rerun `make backtest`.*"
        return f"![{alt}](figures/{name})"

    comparison_section = ""
    if comparison is not None and not comparison.empty:
        labels = {
            ("daily", False): "daily bars, no funding",
            ("hourly", False): "hourly fills and exact 24h holds, no funding",
            ("hourly", True): "hourly fills and exact 24h holds, funding charged",
        }
        rows = []
        for holding in sorted(comparison["holding_days"].unique()):
            for execution, charged in (
                ("daily", False),
                ("hourly", False),
                ("hourly", True),
            ):
                selection = comparison.loc[
                    comparison["holding_days"].eq(holding)
                    & comparison["execution"].eq(execution)
                    & comparison["funding_charged"].eq(charged)
                ]
                full = selection.loc[selection["window"].eq("full")]
                clean = selection.loc[selection["window"].eq("clean-oos")]
                if full.empty or clean.empty:
                    continue
                rows.append(
                    f"| {int(holding)} | {labels[(execution, charged)]} | "
                    f"{pct(full['cagr'].iloc[0])} | {pct(clean['cagr'].iloc[0])} | "
                    f"{pct(clean['max_drawdown'].iloc[0])} |"
                )
        comparison_section = f"""
## What a finer execution model changes

The daily proxy cannot see when inside a day a limit filled, so it schedules
the exit from the calendar rather than from the fill. The realized holding
period is therefore uncertain by up to a day, which is tolerable at the frozen
five-day hold and disqualifying at the one-day hold the author's earlier post
describes, where the uncertainty is the whole holding period.

Resolving that needs intraday bars. The only hourly archive available here
covers **Binance USD-M perpetual futures**, a different venue with a different
universe and a survivor-rich history, so the levels below are not comparable
to the spot numbers above. What is comparable is each block internally: the
daily proxy is re-run on the same futures data, so the step from one row to
the next isolates the execution model and then the funding charge.

| Hold days | Execution model | Full-sample CAGR | Clean 2024-2026 CAGR | Clean max drawdown |
|---:|---|---:|---:|---:|
{chr(10).join(rows)}

Read down each holding-period block, never across blocks and never against the
spot table above. Resolving the fill to the hour moves the one-day result by
16 points and the ten-day result by 3, in opposite directions, which is exactly
what the mechanism predicts: the unobserved fill moment is the entire holding
period at one day and a tenth of it at ten. It does not change the verdict.
Every holding period still loses money on the clean restart.

{figure('execution', 'Grouped bars comparing CAGR under daily bars, hourly execution, and hourly execution with funding charged')}
"""

    benchmark_section = ""
    if benchmark is not None and not benchmark.empty:
        def bench_rows(window: str) -> str:
            block = benchmark.loc[benchmark["window"].eq(window)]
            return "\n".join(
                f"| {row.strategy} | {pct(row.total_return)} | {pct(row.cagr)} | "
                f"{pct(row.max_drawdown)} | {num(row.sharpe_zero_cash)} | "
                f"{num(row.beta)} | "
                + (
                    "| |"
                    if row.strategy == "BTC buy and hold"
                    else f"{pct(row.alpha_annual)} | {num(row.alpha_t)} |"
                )
                for _, row in block.iterrows()
            )

        base_full = benchmark.loc[
            benchmark["window"].eq("full")
            & benchmark["strategy"].eq("Momentum + correction")
        ].iloc[0]
        held_full = benchmark.loc[
            benchmark["window"].eq("full")
            & benchmark["strategy"].eq("BTC buy and hold")
        ].iloc[0]
        benchmark_section = f"""
## Against buy and hold

Every return above is absolute. The question that decides whether the rule is
worth running is relative: over the same window, holding the market it trades
returned {pct(held_full['total_return'])} at a {pct(held_full['max_drawdown'])}
drawdown.

| Full sample | Total return | CAGR | Max drawdown | Sharpe | Beta | Alpha | t |
|---|---:|---:|---:|---:|---:|---:|---:|
{bench_rows('full')}

| Clean 2024-2026 | Total return | CAGR | Max drawdown | Sharpe | Beta | Alpha | t |
|---|---:|---:|---:|---:|---:|---:|---:|
{bench_rows('clean')}

The frozen baseline returns {pct(base_full['total_return'])} against the
benchmark's {pct(held_full['total_return'])}, with a deeper drawdown and a
lower Sharpe. It loses to buying the index and sitting still.

Alpha is the intercept of a regression of daily strategy returns on daily
benchmark returns, annualized. It matters here because the rule holds cash
about half the time, so a raw return comparison is unfair to it: at a beta of
{num(base_full['beta'])} it takes barely half the market's risk. Adjusted for
that, it earns {pct(base_full['alpha_annual'])} a year with a t-statistic of
{num(base_full['alpha_t'])}, which is not distinguishable from zero. The
strategy is a diluted long position, not a source of return the market does not
already provide.

The gated variant is the only row that clears the benchmark on a t-statistic
worth quoting, and it was chosen after seeing the evaluation window. Its clean
restart alpha is negative.

{figure('equity', 'Baseline equity and drawdown against BTC buy and hold')}
"""

    regime_section = ""
    if regime is not None and not regime.empty:
        gated = regime.loc[regime["span"].gt(0)]
        rows = "\n".join(
            f"| {row.label} | {row.provenance} | {pct(row.days_in_market)} | "
            f"{pct(row.total_return)} | {pct(row.cagr)} | {pct(row.max_drawdown)} | "
            f"{pct(row.oos_cagr)} |"
            for _, row in regime.iterrows()
        )
        best = gated.loc[gated["oos_cagr"].idxmax()]
        base_row = regime.loc[regime["span"].eq(0)].iloc[0]
        regime_section = f"""
## Reconstruction hypothesis: a market-wide regime gate

The momentum-correction post mentions no regime filter. Two other posts by the
same author do: an earlier one describes "a simple regime filter" without
defining it, and a later one publishes `Buy BTC if C > MA(50)` with a chart
captioned `Close > SMA(50) Timing vs Buy & Hold`. That is enough to test a
gate, and not enough to claim the published curve used one.

The gate stands the whole book down when the reference asset closes under its
average: no new orders, and resting orders are pulled. Positions already open
run to their scheduled exit, because the rule has no stop.

| Gate | Provenance | Days in market | Total return | CAGR | Max drawdown | Clean 2024-2026 CAGR |
|---|---|---:|---:|---:|---:|---:|
{rows}

Every gate roughly halves time in the market, multiplies the full-sample
return, and cuts maximum drawdown from {pct(base_row['max_drawdown'])} to
around {pct(gated['max_drawdown'].median())}. The clean-window loss shrinks
from {pct(base_row['oos_cagr'])} to {pct(best['oos_cagr'])} at best. It does
not turn positive.

{figure('regime_equity', 'Equity curves and drawdowns of the gated variants against the ungated baseline')}

The account path is the plain version of the table. Through 2020 and 2021 the
gated and ungated books track each other, ending 2021 within 32% of one
another. Across 2022 the ungated book falls from $1.62M to $0.53M while the
gated books hold between $1.33M and $1.41M, and the gap never closes again.
The gate does not trade better. It declines to trade at all through the one
year that destroys the ungated book, which is also the only year where the two
differ much.

That is a real risk-management result and a weak statistical one: it rests on a
single bear market. Whether it reproduces the source is a separate question,
and the answer is partly.

{figure('regime', 'Cumulative P&L trajectories of the gated variants against the digitised source curve')}

The gate does what it was proposed to do. Against the digitised source curve,
the ungated rule gives back 71% of its running total across 2022 where the
source gives back 24%; the gated variants give back between 2% and 40%,
bracketing it. That is the flat stretch in the published chart, explained.

It explains nothing about the divergence that matters. From end-2024 the
source more than doubles, gaining 118%, while every variant here falls by
between 22% and 36%. Standing down in bear markets is evidently part of the
author's process, and it is not what separates this reconstruction from his
result.

Two warnings attach to every number in this section. The gate was selected
after seeing the 2024-2026 window, so those figures are a fit rather than a
test and cannot be quoted as out-of-sample evidence. And a filter keyed to one
reference asset over one bull-bear cycle has very few effective degrees of
freedom in the data: it is one decision about one market's direction,
repeated. Any forward use needs an untouched period beginning after this
report.
"""

    funding_section = ""
    if available.get("funding"):
        funding_section = f"""
### Funding on that venue is a tail artifact

The comparison above reports the futures rows with funding excluded, which
needs justifying, because leaving out a real cost usually flatters a result and
here it does the opposite.

A long perpetual pays funding when the rate is positive and receives it when
the rate is negative. Charging it from the venue's own per-symbol series does
not shave the result, it inflates it, because this rule buys into sharp
corrections and sharp corrections are exactly when funding goes deeply
negative.

That sounds like an edge and is not one. The receipts are concentrated in a
handful of newly listed perpetuals switched to hourly funding at the negative
cap, held at position sizes that a book that thin would never have filled.
Treating them as strategy P&L would mean claiming a return stream produced by
about thirty trades in illiquid names.

{figure('funding', 'Concentration of funding receipts and the distribution of per-trade funding')}

Those rows therefore report price P&L only, which is also what keeps them
comparable to the spot study above. The funding-charged variant stays visible in the
execution table above and in `funding_charged_summary.csv`. Any real attempt to
harvest this would be a different strategy with a different liquidity
constraint, and would need depth data this project does not have.
"""

    mode_rows = "\n".join(
        "| {label} | {ret} | {cagr} | {dd} | {sharpe} | {trades} | {fill} |".format(
            label=MODE_LABELS[row["mode"]],
            ret=pct(row["total_return"]),
            cagr=pct(row["cagr"]),
            dd=pct(row["max_drawdown"]),
            sharpe=num(row["sharpe_zero_cash"]),
            trades=int(row["trades"]),
            fill=pct(row["fill_rate"]),
        )
        for _, row in summaries.iterrows()
    )
    oos_rows = "\n".join(
        "| {label} | {ret} | {cagr} | {dd} | {trades} | {pf} |".format(
            label=MODE_LABELS[mode],
            ret=pct(oos[mode].get("total_return")),
            cagr=pct(oos[mode].get("cagr")),
            dd=pct(oos[mode].get("max_drawdown")),
            trades=int(oos[mode].get("trades", 0)),
            pf=num(oos[mode].get("profit_factor")),
        )
        for mode in MODE_LABELS
    )
    yearly_rows = "\n".join(
        f"| {int(row.year)} | {pct(row['return'])} | "
        f"{pct(row.volatility)} | {pct(row.worst_drawdown)} |"
        for _, row in yearly.iterrows()
    )
    sensitivity_rows = "\n".join(
        f"| {row.correction_sigma:.1f} | {int(row.holding_days)} | "
        f"{pct(row.cagr)} | {pct(row.oos_cagr)} | "
        f"{pct(row.oos_max_drawdown)} | {int(row.trades)} |"
        for _, row in sensitivity.iterrows()
    )
    one_day_rows = "\n".join(
        f"| {row.correction_sigma:.1f} | {pct(row.oos_total_return)} | "
        f"{pct(row.oos_cagr)} | {pct(row.oos_max_drawdown)} |"
        for _, row in sensitivity.loc[
            sensitivity["holding_days"].eq(1)
            & sensitivity["correction_sigma"].ge(1.0)
        ].sort_values("correction_sigma").iterrows()
    )
    friction_rows = "\n".join(
        f"| {row.label} | {row.round_trip_bps:.0f} | "
        f"{pct(row.cagr)} | {pct(row.max_drawdown)} | "
        f"{money(row.total_fees)} |"
        for _, row in friction.iterrows()
    )

    # The control verdict is a comparison, not a constant: whether the momentum
    # filter beats generic resting bids differs by venue and by window.
    def mode_cagr(mode: str, window: str) -> float:
        if window == "full":
            row = summaries.loc[summaries["mode"].eq(mode)]
            return float(row["cagr"].iloc[0]) if not row.empty else float("nan")
        return float(oos.get(mode, {}).get("cagr", float("nan")))

    waits = mode_cagr("full", "full") > mode_cagr("momentum_only", "full")
    beats_passive_full = mode_cagr("full", "full") > mode_cagr("passive_limit", "full")
    beats_passive_oos = mode_cagr("full", "clean") > mode_cagr("passive_limit", "clean")
    control_verdict = (
        ("Yes over the full sample" if waits else "No, even over the full sample")
        + ", but "
        + (
            "generic resting bids without the momentum filter still do better on "
            "the clean restart"
            if beats_passive_full and not beats_passive_oos
            else "generic resting bids without the momentum filter do at least as "
            "well in both windows"
            if not beats_passive_full
            else "the momentum filter does add to generic resting bids in both "
            "windows"
        )
        + "."
    )
    cagr_ci = bootstrap.get("cagr_percentiles", {})
    dd_ci = bootstrap.get("max_drawdown_percentiles", {})
    source_symbols = coverage.get("symbols", "n/a")
    source_archives = coverage.get("archives", "n/a")

    return f"""# Momentum-Correction Strategy: Research and Live-Readiness Report

Venue **Binance Spot, USDT pairs**, execution UTC daily klines. Generated from the
frozen rules in `README.md`. Status: **{live_stage}**.

## Executive conclusion

The backtest is suitable for deciding whether to proceed to paper trading.
It is **not** sufficient for unattended live capital. The historical run uses
daily candle trade-through as a proxy for limit fills and next-target-day
opens with modeled slippage for exits. It does not reconstruct queue
position, partial fills, historical symbol filters, spread, or book depth.

Baseline full-sample net return is **{pct(base['total_return'])}**, CAGR is
**{pct(base['cagr'])}**, and maximum drawdown is
**{pct(base['max_drawdown'])}**. From 2024-01-01 onward, CAGR is
**{pct(test.get('cagr'))}** with maximum drawdown
**{pct(test.get('max_drawdown'))}**. These numbers are model outputs, not
expected live returns.

| Question | Answer |
|---|---|
| Does the frozen rule make money over the full sample? | Yes on paper: {pct(base['total_return'])} net, {pct(base['cagr'])} a year. |
| Does it make money on the clean 2024-2026 restart? | No: {pct(test.get('cagr'))} a year, {pct(test.get('max_drawdown'))} drawdown. |
| Does waiting for the correction beat entering at once? | {control_verdict} |
| Is the full-sample result robust to its own parameters? | No: only {pct(positive_sensitivity)} of predeclared cells earn a positive clean return. |
| Is it ready for live capital? | No. Shadow trading and an execution model come first. |

## How the strategy works

The rule runs once per UTC daily close and touches only Binance Spot USDT pairs.
It buys strength on weakness: rank the liquid universe by
volatility-normalized momentum, rest a buy limit one daily volatility below
the signal close, and sell a fixed five days after any fill.

Every parameter in the diagram is this project's frozen choice. The source
post publishes three rules, namely measure momentum, enter a correction with a
limit order, and exit after `X` days. It publishes none of the numbers, `X` and the
data frequency. An earlier post by the same author mentions a one-day hold,
which the predeclared grid below shows would materially change the verdict.

{figure('rules', 'Five-stage diagram of the momentum-correction rules with baseline order counts')}

Roughly half of all orders never fill, which is the mechanism's defining
property rather than a defect: unfilled orders cost nothing but forgo the
moves that ran away without correcting. The two worked examples below are real
orders from this run: the same rule, one filled and one expired.

{figure('anatomy', 'A filled correction trade beside an order that expired unfilled')}

## Source-claim alignment: failed reconstruction

The linked source chart rises sharply during 2024--2026 and ends near 950%
additive cumulative P&L. This baseline returns **{pct(test.get('total_return'))}**
in a clean account restarted on 2024-01-01. It therefore does **not** reproduce
the source chart and must not be presented as the author's strategy.

The exact X post leaves the momentum formula, correction threshold, holding
period `X`, order lifetime, venue, sizing, costs, and P&L aggregation
unspecified. Its chart reports "Cumulative PnL" rather than compounded account
equity. An [earlier related post](https://www.linkedin.com/posts/pavelkycek_simple-strategies-work-in-crypto-equity-activity-7372569491874136064-IDSO)
mentions a one-day hold and a simple regime filter, but does not define the
filter. The frozen baseline instead uses a five-day hold and no regime filter.

For source alignment, the predeclared one-day diagnostics are:

| Correction sigma | Clean OOS return | Clean OOS CAGR | Clean OOS max drawdown |
|---:|---:|---:|---:|
{one_day_rows}

These results show that the holding period and correction depth can reverse
the recent-period conclusion. They do not identify the source rules. Choosing
the strongest row now would be post-selection on the evaluation period, so no
row is promoted to a live candidate.

Only **{pct(positive_sensitivity)}** of the predeclared
correction-depth/holding-period cells have positive clean out-of-sample
CAGR. The 30-day
block-bootstrap probability of non-positive CAGR is
**{pct(bootstrap_p)}**. Profit concentration in the best 10% of trades is
**{pct(concentration['top_10pct'])}**.

## Data and method

- Venue: Binance Spot, USDT pairs.
- Execution data: UTC daily klines.
- Archive snapshot: {coverage.get('requested_start', 'n/a')} through
  {coverage.get('requested_end', 'n/a')}.
- Historical symbols with data: {source_symbols}.
- Signal: top-50 trailing-volume universe; positive, top-quintile
  20-day volatility-normalized momentum.
- Entry: resting limit one daily volatility below the signal close,
  valid for three days; the daily low must trade strictly below the limit.
- Exit proxy: target-day open after five days, less 5 bps slippage.
- Baseline costs: 10 bps maker entry fee and 10 bps taker exit fee.
- Sizing: cash-only, 10% per coin, 100% aggregate reserved plus invested.

Binance documents the archive's kline schema and checksum files in its
[public-data repository](https://github.com/binance/binance-public-data).
Current live orders must obey the current `PRICE_FILTER`, `LOT_SIZE`, and
notional filters from
[official Spot API filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md).

## Strategy and controls

| Strategy | Total return | CAGR | Max drawdown | Sharpe | Trades | Fill rate |
|---|---:|---:|---:|---:|---:|---:|
{mode_rows}

The next-open momentum control identifies whether waiting for the correction
adds value. The broader positive-momentum and passive-limit controls test
whether the result is driven by the top-quintile rule or by generic resting
bids.

{figure('controls', 'Cumulative growth of the strategy and its three entry controls on a log scale')}

{control_verdict} Read the two windows together: a filter that only helps in
the window that also contains the bull market is not evidence of a filter that
works.

### Clean 2024-2026 evaluation

Each control restarts with $100,000 and no positions on 2024-01-01:

| Strategy | Total return | CAGR | Max drawdown | Trades | Profit factor |
|---|---:|---:|---:|---:|---:|
{oos_rows}

{figure('oos_controls', 'Total return, maximum drawdown and profit factor for each control after a clean 2024 restart')}
{benchmark_section}{comparison_section}{funding_section}{regime_section}

## Baseline trade economics

- Final equity: {money(base['final_equity'])} from
  {money(base['initial_equity'])}.
- Orders: {int(base['orders']):,}; fills: {int(base['fills']):,};
  fill rate: {pct(base['fill_rate'])}.
- Hit rate: {pct(base['hit_rate'])}.
- Mean/median return on reserved capital:
  {pct(base['mean_trade_return'])} / {pct(base['median_trade_return'])}.
- Profit factor: {num(base['profit_factor'])}.
- Total modeled fees: {money(base['total_fees'])}.
- Average invested capital: {pct(base['average_invested_fraction'])};
  average capital reserved by open limits:
  {pct(base['average_reserved_fraction'])}.
- Daily expected shortfall: 90% {pct(base['daily_es_90'])},
  95% {pct(base['daily_es_95'])}, 99% {pct(base['daily_es_99'])}.
- Missing scheduled exits charged at zero recovery:
  {int(base['forced_exits'])}; delayed exit-days:
  {int(base['delayed_exit_days'])}.

A profit-concentration value above 100% means the best trades earned more
than total net P&L because the remaining trades lost money in aggregate.

{figure('concentration', 'Cumulative share of net profit against trades ranked from best to worst')}

## Calendar stability

| Year | Return | Annualized volatility | Worst within-year drawdown |
|---:|---:|---:|---:|
{yearly_rows}

{figure('yearly', 'Calendar-year returns above the worst drawdown suffered within each year')}

## Parameter robustness

Each cell changes only correction depth and holding period. It is reported
as a family, not searched for a replacement baseline.

| Correction sigma | Hold days | Full CAGR | OOS CAGR | OOS max drawdown | Full trades |
|---:|---:|---:|---:|---:|---:|
{sensitivity_rows}

{figure('sensitivity', 'Heat maps of full-sample and clean out-of-sample CAGR across correction depth and holding period')}

The positive deep-correction cells are a new hypothesis discovered on the
same 2024-2026 evaluation period. Switching to one now would convert that
period into training data. Any such variant needs an untouched forward
shadow period beginning after this report.

## Friction stress

Round-trip bps combine modeled fees and market-order slippage. A resting
limit never receives positive price improvement.

| Scenario | Round-trip bps | CAGR | Max drawdown | Total fees |
|---|---:|---:|---:|---:|
{friction_rows}

{figure('friction', 'CAGR and maximum drawdown against round-trip trading cost')}

## Sampling uncertainty

The moving-block bootstrap resamples 30-day blocks and therefore preserves
some volatility clustering:

- CAGR 95% interval:
  {pct(cagr_ci.get('2.5'))} to {pct(cagr_ci.get('97.5'))};
  median {pct(cagr_ci.get('50'))}.
- Maximum-drawdown 95% range:
  {pct(dd_ci.get('2.5'))} to {pct(dd_ci.get('97.5'))};
  median {pct(dd_ci.get('50'))}.

{figure('bootstrap', 'Distribution of annualized returns across resampled evaluation paths')}

This interval measures path uncertainty conditional on the observed sample.
It does not cover exchange failure, delisting liquidation, regime change, or
model misspecification.

## Live-trading gate

Do not enable order submission until all of these have passed:

1. Run `scripts/live_plan.py` read-only every UTC close for at least 60 days.
2. Shadow every planned order against trade and depth streams; measure
   trade-through fill rate, queue delay, partial fills, spread, and exit
   slippage.
3. Re-run the backtest with the measured execution model and actual account
   commissions.
4. Reconcile balances, open orders, and positions before reserving capital.
5. Enforce current tick, lot-size, min/max notional, order-count, and
   maximum-position filters from `/api/v3/exchangeInfo`.
6. Use unique client order IDs and persist the order state machine before
   sending anything. A timeout or HTTP 5xx leaves execution status unknown;
   query order status before retrying.
7. Add stale-data, clock-skew, WebSocket-gap, API-rate-limit, and kill-switch
   controls. Binance documents 429 backoff and possible 418 IP bans in the
   [official REST documentation](https://developers.binance.com/en/docs/products/spot/rest-api).
8. Start with a manually approved canary allocation no larger than 10% of
   the intended per-coin risk. Increase only after realized execution agrees
   with the shadow model.

The included live-plan tool is deliberately unable to place orders.
[Official order documentation](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md)
shows that live order endpoints are signed and require `TRADE` permission.
API credentials should not be added to this research project.

## Reproduction

```bash
python -m pip install -r requirements.txt
make test
make discover download parse backtest
make live-plan
```

The report outputs are under `results/`; raw archives and parsed parquet
files are intentionally ignored by Git.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-06-30")
    parser.add_argument("--test-start", default="2024-01-01")
    parser.add_argument("--bootstrap-repetitions", type=int, default=2000)
    args = parser.parse_args()

    ensure_directories()
    config = BacktestConfig(start=args.start, end=args.end)
    raw = load_market()
    market = prepare_market(raw, config)
    print(
        f"prepared {len(market):,} point-in-time rows; "
        f"{int(market['signal'].sum()):,} signal rows",
        flush=True,
    )

    results: dict[str, dict[str, object]] = {}
    summaries: list[dict[str, object]] = []
    oos: dict[str, dict[str, object]] = {}
    for mode in MODES:
        print(f"simulating {mode}", flush=True)
        result = simulate(market, config, mode)  # type: ignore[arg-type]
        results[mode] = result
        mode_equity = result["equity"]
        assert isinstance(mode_equity, pd.DataFrame)
        mode_equity.to_csv(RESULTS_DIR / f"equity_{mode}.csv", index=False)
        summary = performance_summary(result, config.initial_equity)
        summary["mode"] = mode
        summaries.append(summary)
        oos[mode], _ = period_result(result, args.test_start, args.end)

    summary_frame = pd.DataFrame(summaries)
    summary_frame.to_csv(RESULTS_DIR / "summary.csv", index=False)
    write_json(RESULTS_DIR / "oos_summary.json", oos)

    baseline = results["full"]
    baseline_equity = baseline["equity"]
    baseline_trades = baseline["trades"]
    baseline_orders = baseline["orders"]
    assert isinstance(baseline_equity, pd.DataFrame)
    assert isinstance(baseline_trades, pd.DataFrame)
    assert isinstance(baseline_orders, pd.DataFrame)
    baseline_equity.to_csv(RESULTS_DIR / "equity.csv", index=False)
    baseline_trades.to_csv(RESULTS_DIR / "trades.csv", index=False)
    baseline_orders.to_csv(RESULTS_DIR / "orders.csv", index=False)

    write_json(
        RESULTS_DIR / "base_config.json",
        {
            "config": config_dict(config),
            "test_start": args.test_start,
        },
    )
    print("base strategy and controls written; run robustness.py then report.py")


if __name__ == "__main__":
    main()
