# Source and interpretation boundary

- Author: [Pavel | Robuxio (@PKycek)](https://x.com/PKycek)
- Post:
  [2080649886517637282](https://x.com/PKycek/status/2080649886517637282)
- Published: 2026-07-24 13:42:44 UTC
- Retrieved: 2026-07-24 via the public FxTwitter API

The post gives three rules:

1. measure momentum;
2. enter a correction with a limit order;
3. exit after \(X\) days.

It proposes two ordered relationships: stronger momentum should imply a
stronger signal, and a deeper correction should imply a stronger entry.
The author interprets shallow corrections as predominantly momentum
exposure and deeper corrections as adding more mean-reversion exposure.

The attached image is a cumulative-P&L curve labeled "Momentum Correction"
covering roughly 2020 through July 2026. The post says it uses the top 50
coins by volume without survivorship bias. The plotted curve rises sharply
during 2024--2026 and finishes near 950% cumulative P&L.

## What the chart itself shows

The attached image was retrieved and measured rather than read by eye; the
extraction is reproducible with `scripts/source_chart.py` and the image is
kept at `docs/source_chart.jpg`. Calibrating against the axis gridlines
recovers a curve that starts at +2%, peaks at +996% and ends at +955%,
consistent with a post whose text claims roughly 950%.

Two properties of the curve carry information the text does not.

**It is flat most of the time.** Only 27% of pixel columns show any vertical
movement, each column spanning about 2.2 days. The same measurement applied to
this project's baseline, binned to the same width, gives 80%. Whatever
produced the source curve books P&L far less often than a top-50 universe with
a daily signal does.

**It does not lose money in 2022.** Digitised year-end values, in points of
cumulative P&L:

| Year end | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Source | -3 | 183 | 139 | 164 | 431 | 917 | 940 |

The curve gives back 44 points across 2022 and trades in a 120 to 187 band all
year. An always-in-the-market version of these rules lost 67% of equity over
the same year. A strategy that is merely flat through a bear market of that
size was not in the market for it.

## Related posts by the same author

An earlier post describes a closely related setup as buying corrections in the
highest-momentum coins, **holding for one day**, and applying a **simple
regime filter** that it never defines:

- [LinkedIn post](https://www.linkedin.com/posts/pavelkycek_simple-strategies-work-in-crypto-equity-activity-7372569491874136064-IDSO)

A later post publishes a regime rule directly:

- [X post 2080978478078230711](https://x.com/PKycek/status/2080978478078230711),
  published 2026-07-25: "Buy BTC if C > MA(50) vs. buy and hold. I first shared
  this simple model in June 2022. It was inspired by a basic moving-average
  model applied to the S&P 500."

Its chart is captioned `BTC/USD: Close > SMA(50) Timing vs Buy & Hold,
2018-01-01 to 2022-06-01 | growth of $1` and plots three series: buy and hold,
SMA50 timing gross, and SMA50 timing at **10 bp per side**. That establishes
three things about how the author works which the momentum-correction post
leaves open: daily closes, a **simple** 50-period average rather than an
exponential one, and a 10 bp per side cost assumption, which happens to match
the fee model frozen in `README.md` before this post was seen.

None of this states that the momentum-correction equity curve used a regime
filter. It establishes that the author uses one elsewhere and which average he
favours, which is enough to make a gated variant worth testing and not enough
to call the result a reproduction. See the reconstruction section of the
generated report.

The post does **not** publish:

- the momentum definition or lookback;
- the volume lookback or exact universe reconstruction;
- the correction definition or depth;
- the limit-order lifetime, queue model, or gap-fill convention;
- the value of \(X\);
- portfolio sizing or overlapping-position rules;
- transaction costs, liquidity constraints, or execution venue;
- trade-level data or code.

The post describes a directional strategy on the underlying coins. It does
not describe an option strategy. `README.md` formalizes one falsifiable spot
implementation and clearly labels every parameter not supplied by the
source.

## Reproduction verdict

The frozen five-day baseline does **not** reproduce the source claim. Its
clean 2024--2026 return is -48.29%, while the source chart rises materially
over the same broad period. The source chart also reports additive
"Cumulative PnL", whereas this project reports a cash-constrained compounded
equity account.

The mismatch is not evidence that the source chart is false; it means the
published information is insufficient to reconstruct it. Material unknowns
include the exact momentum and correction formulas, the value of `X`, the
regime filter, venue, cost model, and P&L aggregation. Results from alternative
parameters are diagnostics and must not be relabeled as a reproduction after
observing the source curve.
