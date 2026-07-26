# Audit

A review of the implementation against the frozen specification in
`README.md`, covering the mathematics, the event logic, and the standard
failure modes of a backtest. Every claim marked *verified* is enforced by a
test in `tests/test_invariants.py` or reproduced by the query shown beside it.

## 1. Look-ahead

The one error that invalidates everything else. Three independent checks:

**Truncation invariance (verified).** For a cutoff date $T$, features computed
on the full history and features computed on history truncated at $T$ must
agree exactly for every row stamped $T$. If any feature consulted a later bar,
deleting later bars would change it. The test runs three cutoffs across
`momentum`, `sigma`, `volume_sum`, `volume_rank` and `signal`.

**Formula reproduction (verified).** For a sampled row, $\hat\sigma_{u,t}$ and
$m_{u,t}$ are recomputed by hand from the raw close series up to and including
$t$, and must match to 10 decimal places:

$$\hat\sigma_{u,t}=\sqrt{\tfrac{1}{L_\sigma-1}\sum_{j=0}^{L_\sigma-1}(r_{u,t-j}-\bar r_{u,t})^2},
\qquad m_{u,t}=\frac{\log(C_{u,t}/C_{u,t-L_m})}{\hat\sigma_{u,t}\sqrt{L_m}}.$$

**Event ordering (verified).** An order created from the close of day $t$ has
`active_date` $=t+1$, and the fill loop refuses any date before it, so a signal
can never trade on its own bar. Cross-sectional ranks (`volume_rank`,
`momentum_percentile`) are computed within a single date, so they use only
information available at that close.

Rolling windows are reset at listing gaps by `_contiguous_segment`, so a
20-day volatility estimate can never span a delisting hole.

## 2. Survivorship

The spot universe is built by enumerating the venue's archive rather than its
current symbol list, so pairs that were later delisted stay in history:

| | Symbols | Series ending before 2026-06 |
|---|---:|---:|
| Spot daily | 599 | 171 (28.5%) |
| Futures hourly | 786 | 29 (3.7%) |

77 of the delisted spot symbols were actually traded by the baseline, and
13.7% of all baseline trades are in names that later disappeared. The spot run
is therefore free of survivorship bias in any material sense.

The futures archive is a different matter. Perpetuals are delisted far less
often than spot pairs, but a 3.7% attrition rate over six years is low enough
that the futures full-sample figures should be read as flattered relative to
spot for that reason alone, independently of the venue's other differences.
This is why the daily-versus-hourly comparison re-runs the daily proxy *on the
futures data* rather than comparing across the two studies.

## 3. Accounting

**Cash conservation (verified).** With every position closed at the end of the
run, final equity must equal initial equity plus the sum of realized trade
P&L. Any leak in the reserve, fill, or exit path breaks this identity.

Reservation mechanics were checked by hand and by test:

- an order reserves cash at placement, so a later fill cannot create hindsight
  leverage;
- quantity solves $q\,P^{\mathrm{in}}(1+f^{\mathrm{in}}) = R$ exactly, so the
  fill consumes precisely its reservation and `cash - reserved` is unchanged
  by the fill itself;
- cancels release the reservation;
- equity is $\text{cash} + \sum q_u B_u$, with reserved-but-unfilled cash
  counted as cash rather than as exposure.

**Caps (verified).** Invested plus reserved never exceeds
$\bar\rho E_t$, and no single order reserves more than $\rho E_t$.

## 4. Execution realism

**Fills (verified).** A limit fills only on a strict trade-through, and always
at the limit price, never at the lower traded price. The daily path uses
$\mathrm{Low}_t < L$; the hourly path uses the first hourly bar with
$\mathrm{Low}_h < L$ and asserts that bar really did trade through.

**Exits (verified).** The daily path exits no earlier than
$t_{\text{entry}} + H$ days. The hourly path exits at exactly $H\cdot 24$
hours after the fill, checked to the hour across every scheduled trade.

**Costs.** 10 bps each side plus 5 bps of exit slippage, with a friction grid
out to 100 bps round trip. A resting limit never receives price improvement.

## 5. Statistics

Standard definitions, checked for the usual mistakes:

- CAGR uses the calendar span of the equity index, $(E_T/E_0)^{365.25/\Delta}-1$;
- volatility and Sharpe annualize by $\sqrt{365}$, consistent with a market
  that trades every day, and the Sharpe is explicitly named
  `sharpe_zero_cash` because it assumes no return on idle cash;
- expected shortfall at level $\alpha$ is the mean of returns at or below the
  $(1-\alpha)$ quantile, not the quantile itself;
- maximum drawdown duration counts the longest consecutive underwater run;
- the moving-block bootstrap resamples 30-day blocks with replacement and
  truncates to the original length, preserving short-range dependence.

Profit concentration above 100% is reported as such and explained: it means
the remaining trades lost money in aggregate.

## 6. Known limitations

These are modelling choices, not defects, but each one flatters or distorts
the result in a direction worth stating.

1. **No lot-size or notional filters.** Specification section 5 rounds
   quantity to the venue step and skips orders below the minimum notional. The
   implementation treats positions as infinitely divisible. This is optimistic
   for small allocations in high-priced names.
2. **Bar-level trade-through is not queue position.** A low below the limit
   proves the market printed there, not that this order filled. Real fill
   probability is lower, especially for marginal touches.
3. **Fill timing inside the hour.** The hourly path stamps the fill at the
   bar's open, so the true fill may be up to 59 minutes later and the exit
   inherits that error. It is a resolution limit, not a bias.
4. **Stale marking across data gaps.** A symbol with no bar on a day keeps its
   last known mark until it reappears or the exit forces a zero-recovery
   close. The equity path between those points is stale.
5. **No borrow, funding, or yield on idle cash in the spot run.** Cash sits at
   zero return, which understates a real account's carry and overstates the
   relative appeal of staying invested.
6. **Funding in the futures run is a tail artifact.** Charging it raises the
   result, with 80% of the receipts coming from 1% of trades on newly listed
   perpetuals at the hourly funding cap. It is excluded from the headline and
   attributed separately.
7. **Universe size early in the sample.** The top-50 rule is fully populated
   from 2020-01 onward; before that the eligible pool is smaller, which is
   outside the backtest window but worth knowing if the start date moves.
8. **Percentile threshold granularity.** With 50 names, the
   $p_{u,t}\ge 0.80$ rule admits roughly the top 10, so the effective
   selection is coarse and moves in steps of one name.

## 7. What would change the conclusion

The result is negative out of sample at every predeclared parameter cell once
funding is excluded, so the conclusion is robust to the parameters that were
declared. It is not robust to:

- an execution model with real queue position and partial fills, which can
  only lower the fill rate on marginal touches;
- a venue change, since the spot and futures universes differ materially;
- the unknown regime filter mentioned in the author's earlier post, which is
  not public and is not implemented here.
