# What this instrument is actually testing

The collector is scaffolding. This is the claim it exists to falsify.

## The operational question

> When Kalshi prices a $100 BTC bucket at X¢, how often does BTC actually settle there?

That is the *measurement*. The *hypothesis* is sharper, because the design already commits
to a mechanism.

## H₀ — the null

**Kalshi's hourly BTC range markets are efficiently priced at the ask.** Across ask bands,
ET hours, and distances from spot, the realized YES rate equals the quoted `yes_ask` within
sampling error. Any apparent edge is overround, noise, or selection.

This is the default and the collector is built to give it every chance to survive: prices
are recorded at the ask you would actually pay, never the mid, and no filtering happens at
collection time.

## H₁ — the alternative, stated as a mechanism

**The market prices the settlement distribution as if settlement were a point observation
of a roughly-normal process at the close. It is neither.**

Two documented facts make that model wrong in specific, opposite-signed ways:

1. **Settlement is a 60-second average, not a point.** The official value is the mean of 60
   RTI prices over the final minute. Averaging shrinks effective variance to
   `σ²(T − 2δ/3)`; at T−4 with δ=1min the effective window is 3.33 minutes, about **9% less
   sigma** than a naive point-settlement model. This makes the true distribution
   **narrower** than a point model implies.
2. **Five-minute crypto returns are fat-tailed**, not normal — Student-t with ν≈4. This
   makes the true distribution **heavier in the tails** than a normal implies.

## The distinctive fingerprint

This is the part that makes the hypothesis worth testing rather than merely asserting.

The two effects act on **different regions of the ladder and in opposite directions**, so
together they predict a specific *shape* of calibration error, not just a direction:

| Region | Averaging effect (narrower) | Fat tails (heavier) | Net prediction |
|---|---|---|---|
| At/near the money | underpriced | — | **underpriced** |
| Intermediate | overpriced | overpriced | **overpriced** |
| Far tail | — | underpriced | **underpriced** |

A **W-shaped** calibration residual across distance-from-spot is a much harder pattern to
produce by chance than a single directional bias. If the residual curve is flat, or
monotonic, or U-shaped, the mechanism above is wrong even if some edge exists.

## The seasonal corollary

σ is not constant across the day — overnight ET hours are materially quieter. **If the
market applies a single σ across all hours**, then low-volatility hours have near-money
buckets underpriced and high-volatility hours the reverse. `close_et_hour` exists to test
exactly this, and it is why missing the 2am–6am ET hours would be disqualifying rather than
merely inconvenient: those gaps are not random with respect to the variable under test.

## Decision rule

The hypothesis is **supported** only if all of:

1. σ(`move`) is stable enough to be estimated (~200–300 settled hours), and differs
   materially by `close_et_hour`.
2. A Student-t fit (ν≈4) beats a normal fit on calibration, and the 60-second-averaging
   model beats the point-settlement model. **Both models must be compared explicitly** —
   this is the leading candidate edge and it is testable directly.
3. The calibration residual shows the predicted shape, not merely nonzero edge.
4. The edge **survives the ask**, not the mid, and survives block-bootstrapped error bars
   by day.

It is **rejected** if realized rates track asks within bootstrapped error, or if edge exists
but with no interpretable structure — which would indicate an artifact rather than a
mechanism.

## What this instrument cannot tell you

Stated plainly, because the temptation to over-read a calibration table is the main risk
here:

- **Nothing about execution.** A quoted ask is not a guaranteed fill, and says nothing
  about depth. `yes_ask_size` is not currently recorded; edge that exists only in size you
  cannot get is not edge.
- **Nothing about persistence.** This is observational. An edge visible in history may be
  competed away, and trading on it changes the thing being measured.
- **Less than the row count suggests.** Outcomes are roughly independent hour to hour, but
  **model errors are not** — volatility clusters, so consecutive hours are mispriced in the
  same direction. Effective sample size is well below the number of rows. Block-bootstrap
  by day.
- **Nothing above the noise floor of `ref_spot`.** Settlement is exact (Kalshi's
  `expiration_value`), but `move` is only as good as the composite at T−4. The measured
  `basis` is the check on that; a constant basis is harmless to σ, a drifting one is not.

## The one-sentence version

> Hourly BTC range markets are priced off a point-settlement, thin-tailed, season-less
> model of a process that is actually 60-second-averaged, fat-tailed, and strongly
> time-of-day dependent — and the resulting calibration error is large enough, and
> structured enough, to survive paying the ask.
