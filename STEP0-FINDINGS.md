# Step 0 — live API verification findings

Verified against the live Kalshi API on **2026-09-04 ~15:20 UTC**, before writing any
collector code, as required by SPEC.md §"Step 0". Per the spec: *the live API wins.*

**Headline: one finding removes an entire subsystem, and two acceptance criteria are
unachievable as written.** Recommend amending the spec before build.

---

## 1. Series identification — CONFIRMED, and the warning was justified

The spec's caution about picking the wrong product was well placed. Three BTC time-series
products exist and only one is the range ladder:

| Ticker | Title | Frequency | Verdict |
|---|---|---|---|
| **`KXBTC`** | **Bitcoin range** | **hourly** | ✅ **This is the target.** |
| `KXBTCD` | Bitcoin price Above/below | hourly | ❌ Binary above/below — wrong product, same frequency. Easy to grab by mistake. |
| `KXBTC15M` | Bitcoin price up down | fifteen_min | ❌ Wrong product and cadence. |

`KXBTC` settlement source confirmed as **CF Benchmarks BRTI**, and
`settlement_timer_seconds: 60` on every market confirms the 60-second averaging mechanic
the spec's variance adjustment (`σ²(T − 2δ/3)`) depends on.

Event tickers are deterministic: `KXBTC-YYMMMDDHH`, e.g. `KXBTC-26SEP0411` closes
2026-09-04 15:00 UTC. Hour is **ET**.

---

## 2. 🔴 Kalshi publishes the exact settlement value — the reconstruction subsystem is unnecessary

**This is the most consequential finding.** Spec Step 0 item 4 asked whether the settled
market object exposes a settlement value directly. **It does.**

```
KXBTC-26SEP0411  expiration_value = "78935.64"   winner: $78,900 to 78,999.99  ✓
KXBTC-26SEP0323  expiration_value = "80824.03"   winner: $80,800 to 80,899.99  ✓
KXBTC-26AUG2814  expiration_value = "77887.63"   winner: $77,800 to 77,899.99  ✓
```

`expiration_value` is populated on **250/250** settled markets sampled, to the cent, on
*every* market in the event (not only the winner). That is the **true BRTI settlement
value** — the exact number the spec's entire three-venue trade-reconstruction pipeline
exists to approximate.

### What this deletes

The multi-venue historical-trade reconstruction is no longer on the critical path:

- No Coinbase/Kraken/Bitstamp trade fetching for settlement.
- No 1-second grid resampling or equal-weighting.
- No exchange retention window as a hard backfill deadline.
- No persistent-basis problem on the settlement leg — it *is* BRTI, not a proxy.
- `n_samples`, `sample_span_seconds`, `sample_stdev` become unnecessary for settlement.

The backfill sweep collapses to: one `GET /markets?event_ticker={deterministic}` per
missing hour, which returns `result` **and** `expiration_value` together.

**Verified working a week back** (`KXBTC-26AUG2814`), so the durability claim is now much
stronger than it was for exchange trade history.

### What this changes about where the error lives

`move = settlement_value − ref_spot`. With settlement now exact BRTI, **`ref_spot` becomes
the only reconstructed quantity and the sole source of error in the headline variable.**
The reliability attention budgeted for the settlement path should move there.

A residual subtlety: settlement is true BRTI while `ref_spot` is a venue composite, so the
basis no longer cancels in the subtraction. **For the primary deliverable this is harmless**
— a constant basis shifts `move` by a constant and leaves `σ(move)` untouched. It matters
only if the basis is time-varying.

**Recommendation:** keep the three-venue composite, but **demote it from "the settlement
value" to a basis diagnostic.** Reconstructing the composite at close *as well* gives you
`basis = expiration_value − composite(close)` measured every single hour — which turns
`reconstruction_ok` from a self-consistency check into a real one, exactly as the spec
hoped, and quantifies how much time-varying basis is polluting sigma.

---

## 3. 🔴 Field names in the spec are stale — and one would cause a silent 100× error

Every price/size field has been renamed. **Prices are already decimal dollars, returned as
strings.**

| SPEC.md says | Live API | Note |
|---|---|---|
| `yes_bid` | `yes_bid_dollars` | string, e.g. `"0.0200"` |
| `yes_ask` | `yes_ask_dollars` | string |
| `last_price` | `last_price_dollars` | string |
| `volume` | `volume_fp` | string, e.g. `"1591.00"` |
| `open_interest` | `open_interest_fp` | string |
| `floor_strike` | `floor_strike` | ✅ unchanged, number |
| `cap_strike` | `cap_strike` | ✅ unchanged, number |
| `status`, `result` | same | ✅ |
| — | **`expiration_value`** | ⭐ see §2 |

> **The spec's discipline rule "convert cents to decimal fractions on write" is now
> actively wrong.** The API already returns `0.0200`. Applying the documented `/100`
> conversion would store `0.0002` and silently destroy every price in the dataset — while
> every row still looked structurally valid. This is precisely the class of bug the spec's
> own discipline section exists to prevent, so it needs deleting explicitly rather than
> just quietly not doing it.

**Also: absent fields are omitted, not null.** `cap_strike` is missing entirely on
`strike_type: greater` markets and `floor_strike` on `less`. Use `.get()`; direct indexing
will raise on the tail buckets.

---

## 4. 🔴 Acceptance criterion 4 (`ladder_ask_sum` 1.00–1.20) is unachievable

Measured on live ladders:

| Event | Buckets | `ask_sum` | `bid_sum` | Buckets with a bid |
|---|---|---|---|---|
| KXBTC-26SEP0412 (near) | 188 | **3.340** | 0.760 | 14 |
| KXBTC-26SEP0417 (far) | 50 | **1.570** | 0.870 | 10 |

The cause is structural, not a bug: **~174 of the 188 buckets are worthless and quote a
$0.01 minimum ask.** That alone contributes $1.74 before any real probability mass. So
`ask_sum` scales with `n_buckets` and can never sit in 1.00–1.20 on a 188-bucket ladder.

As written, criterion 4 fires every hour — which is worse than having no tripwire, because
a check that always fails gets muted, and it was meant to catch the "wrong event" failure
that silently poisons the dataset.

**Recommended replacement.** Bid-side has no minimum-tick floor (dead buckets bid `0.0000`),
so record **both** and use the bracket:

- `ladder_bid_sum` ≤ 1.0 ≤ `ladder_ask_sum` — a true no-arbitrage sanity bracket.
- `ladder_ask_sum_live` — asks summed over buckets with `yes_bid > 0` only. This is the
  meaningful overround measure and *should* land near 1.00–1.20.
- Keep raw `ladder_ask_sum` and `n_buckets` for diagnostics.

Retune criterion 4 to `ladder_bid_sum ∈ [0.70, 1.00]` and `ladder_ask_sum_live ∈ [1.00, 1.20]`.

---

## 5. 🔴 The Google Sheet plan exceeds Google's hard cell limit in ~11 weeks

The spec sizes the Sheet at "~480 rows/day → ~175k rows in a year." That assumed ~20
buckets. **Actual ladder is 188 buckets.**

```
188 buckets × 24 hours            =   4,512 rows/day
4,512 × ~28 columns               = 126,336 cells/day
Google Sheets hard cap            = 10,000,000 cells per spreadsheet
                                    → limit reached in ~79 days
```

At one year the full ladder is ~1.65M rows / ~46M cells — **4.6× over a hard limit that
cannot be raised.** The Sheet is the offsite copy, so this fails exactly when the dataset
becomes valuable.

**Recommendation: write only the live ladder to Sheets** — buckets with `yes_bid > 0`, ~14
per hour — while `snapshots.csv` keeps the full 188. That is ~336 rows/day, ~9,400
cells/day, comfortably under the cap for years, and the live buckets are the only ones any
strategy could transact in anyway. Full-fidelity offsite backup should be a periodic CSV
push (git or object storage), not the Sheet.

---

## 6. Pagination is mandatory

`limit=200` returned exactly 200 markets and a `cursor`; full pagination returned **238**.
Without cursor-following the ladder is silently truncated — corrupting `n_buckets`, both
ladder sums, and potentially dropping the winning bucket. Loop until `cursor` is empty.

---

## 7. Bucket width varies with time-to-close, not just the 5pm hour

| Event | Time to close | Width |
|---|---|---|
| KXBTC-26SEP0412 | near | **$100** (`floor` 68,200 → 86,799.99, `cap−floor` = 99.99) |
| KXBTC-26SEP0417 | ~6h out | **$500** |

The ladder refines from $500 to $100 as the close approaches, so at T−4 we should reliably
see $100. The spec's framing ("the 5pm ET expiry with its wider buckets") is incomplete —
width is a function of time-to-close. `bucket_width` already captures it; the point is that
**analysis must not assume $100**, and must note `cap − floor = 99.99`, not 100.00.

---

## 8. Tail buckets need explicit handling

Every event carries exactly one `strike_type: greater` and one `less` — unbounded tails
(`"$66,999.99 or below"`). `bucket_width` is undefined for these. They must still be
included in ladder sums, and `spot_offset_from_floor` is meaningless for the `less` bucket.
Store `bucket_width` as blank and handle in analysis rather than dropping the rows.

---

## 9. Base URL — both work

`https://api.elections.kalshi.com/trade-api/v2` and
`https://external-api.kalshi.com/trade-api/v2` returned byte-identical results.
**Recommend `api.elections.kalshi.com`** as primary; the other is a viable failover.

Public market data required **no authentication** throughout, consistent with the non-goal
of holding Kalshi credentials.

---

## 10. Refinement to the perishable/durable principle

The spec's organising principle survives, but §2 sharpens it. `result`, `settlement_value`,
and strike bounds are all durable via Kalshi. `ref_spot` is *also* durable, since it can be
reconstructed from exchange trade history after the fact.

> **The perishable set is exactly the quote state at T−4: `yes_bid`, `yes_ask`, `volume`,
> `open_interest`.** Nothing else in the schema is perishable.

This tightens rather than weakens the design: the snapshot path is the only thing needing
retries, alerting, and a heartbeat, and everything else can be rebuilt from scratch at any
time — including after total data loss, provided the snapshot rows survive.

---

## Proposed spec amendments

1. Set series to `KXBTC`; document `KXBTCD` as the confusable near-miss.
2. Replace all field names with the `_dollars` / `_fp` forms; **delete the cents→decimal
   conversion rule** and parse strings to `Decimal`/`float`.
3. Use `expiration_value` as `settlement_value`. Demote trade reconstruction to a
   per-hour basis diagnostic.
4. Backfill = `GET /markets?event_ticker=KXBTC-YYMMMDDHH`. Drop the exchange retention
   constant.
5. Replace criterion 4 with the bid/ask bracket in §4; add `ladder_bid_sum` and
   `ladder_ask_sum_live` to the schema.
6. Sheets receives live buckets only (§5); full ladder to CSV plus a periodic offsite push.
7. Mandate cursor pagination.
8. Note `cap − floor = 99.99`; handle unbounded tails and omitted fields.
9. Criteria 3 and 5 need rewriting — with settlement authoritative, `reconstruction_ok` is
   self-consistent and `n_samples ≥ 55` is moot unless the basis diagnostic is retained.

---

## Open questions

1. **Keep the three-venue composite as a basis diagnostic, or drop it entirely?** Keeping
   it costs one extra call per hour and gives a measured basis plus a genuine integrity
   check. Dropping it makes the collector materially simpler and still yields a valid sigma.
   **My recommendation: keep it** — it is the only independent check on the one remaining
   reconstructed quantity.
2. **Should `ref_spot` also be backfilled from trade history** as a cross-check against the
   live capture? Cheap, and it would validate the live path.
3. Confirm the Sheets column set given the schema changes above, and whether the
   `kalshi-sizing.xlsx` `Data log` tab needs updating to match.
