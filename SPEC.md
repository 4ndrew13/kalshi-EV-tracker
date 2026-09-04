# Kalshi hourly BTC collector — build spec v3

Supersedes v2. Two substantive changes: settlement values are now reconstructed from
historical trade data rather than sampled live, and the hosting section reflects a
ship-it-and-see posture on OCI idle reclamation.

## Purpose

Measure whether Kalshi's hourly Bitcoin range markets are mispriced, before risking more
capital on them. This is a **measurement instrument, not a trading bot.** It places no
orders and holds no Kalshi credentials.

The one question it exists to answer:

> When Kalshi prices a $100 BTC bucket at X¢, how often does BTC actually settle in that
> bucket?

## Non-goals

- No order placement, ever. No Kalshi API credentials on the box.
- No strategy logic in the collector — no two-bucket selection, no 0.75 filter, no
  position sizing. Record the full ladder; apply rules in analysis.
- No filtering at collection time. Record every hour including the 5pm ET expiry with its
  wider buckets. Filter in analysis, never in collection.
- No database. CSV on disk plus a Google Sheet.

## The organising principle: perishable vs durable

**This distinction drives the whole architecture. Everything else follows from it.**

| Data | Lifetime | Consequence |
|---|---|---|
| **Ladder snapshot** at T−4 | Exists for four minutes, then gone forever | Perishable. Every reliability mechanism in this build exists to protect it. A missed snapshot is a permanently lost row. |
| **Settlement value** | Reconstructable from public trade history for months | Durable. Can be backfilled lazily, in bulk, weeks later. Needs no real-time reliability at all. |

So: the snapshot runs on a tight schedule and gets retries, alerting, and a heartbeat. The
settlement is a **backfill sweep** that finds any row with a missing `settlement_value` and
fills it, whenever it happens to run.

The payoff is that a day of downtime costs you that day's snapshots and nothing else. On a
free-tier VM that may be reclaimed without warning, that is the difference between losing a
day and losing the dataset.

## Step 0 — verify before writing anything

**Non-negotiable, do this first.** Every endpoint and field name below came from secondary
sources and may be stale.

Kalshi (https://docs.kalshi.com):

1. Base URL for public market data (believed `https://external-api.kalshi.com/trade-api/v2`).
2. The series ticker for hourly BTC *range* markets, via `GET /series`. Do not assume a
   naming pattern — several BTC series exist (up/down, above-strike, ranges) and they are
   different products. **Getting this wrong produces a dataset that looks perfectly valid
   and answers a question nobody asked.**
3. Exact field names on the market object: `yes_bid`, `yes_ask`, `volume`,
   `open_interest`, `floor_strike`, `cap_strike`, `status`, `result`.
4. Whether the settled market object exposes a settlement value directly. If it does, log
   it — that makes `reconstruction_ok` a real check rather than a self-consistency test.

Exchanges — confirm each venue's historical trades endpoint and **how far back it is
queryable**. That retention window is the hard deadline on the backfill sweep and must be
recorded in the code as a constant.

Report findings before proceeding. If anything differs from this spec, the live API wins.

## Run cycle

One long-running Python process under systemd, self-timed against wall clock.

| Mark | Action | Criticality |
|---|---|---|
| `HH:56` | **Snapshot.** Full ladder + reference spot. | Perishable — retry hard, alert on failure. |
| `HH:20` | **Backfill sweep.** Any snapshot row lacking `settlement_value` whose hour has closed. | Durable — best effort, silent retry next hour. |
| startup | Run the backfill sweep immediately. | Catches up after any restart or rebuild. |

Both runs are short — a handful of HTTP calls, under a minute each. No sleeping, no run
spanning an hour boundary.

`minutes_to_close` is logged **as measured**, not as the nominal 4. Drift then lives in the
data instead of corrupting it, and becomes an analysable variable in its own right.

## Reference price and settlement reconstruction

The settlement mechanic, from the market disclosure:

> The price used to determine this market is based on CF Benchmarks' corresponding Real
> Time Index (RTI). At the last minute before expiration, 60 RTI prices are collected. The
> official and final value is the average of these prices.

Kalshi only reports which $100 bucket won, which censors the outcome to a $100 interval —
far too coarse to estimate a ~$60 sigma from. So reconstruct the settlement value.

**Reconstruct from historical trades, not live sampling.** Pull every trade between
`HH:59:00` and `HH+1:00:00` from each venue after the fact. This is strictly better than
polling: real tick data rather than 60 polled snapshots, no timing precision requirement,
and any past hour can be rebuilt on demand.

**Method:**

1. Fetch trades in the window from Coinbase, Kraken, and Bitstamp (`BTC-USD`).
2. Resample each venue onto a 1-second grid — last trade at or before each second,
   forward-filled. This mirrors the index's own once-per-second sampling more closely than
   a trade-weighted mean does.
3. Equal-weight the three venues at each second, then average the 60 grid points.

**Use a multi-venue composite, not a single exchange.** A single venue carries a
*persistent basis* against BRTI, not zero-mean noise, and a persistent basis shifts every
`move` in the same direction — systematically misclassifying buckets near their boundaries.
Averaging venues largely cancels it. Record contributing venues in `ref_source`; if a venue
is missing, record which ones were actually used for that hour.

`ref_spot` at snapshot time uses the same composite, fetched live.

If BRTI itself becomes available later it slots in as another `ref_source` value with no
other change. Treat it as an upgrade, not a prerequisite.

**Integrity check.** `reconstruction_ok` is true when the reconstructed value falls inside
the bucket that actually settled YES. Below ~95% true, the reference feed is wrong and every
sigma estimate downstream is untrustworthy. Surface this prominently in `analyze.py`.

## Discipline constraints

The parts most likely to be quietly violated during implementation:

- **Never store or derive a midpoint.** `yes_bid` and `yes_ask` as separate columns. You buy
  at the ask; edge computed off the mid is phantom edge that evaporates live. This is the
  single most common fatal bug in this kind of project.
- **Resolve series to event to markets every run.** Never hardcode an event ticker; events
  are short-lived and a hardcoded one breaks within the hour.
- **Read bucket bounds from `floor_strike` / `cap_strike`,** never parsed from the ticker
  string. Ticker encoding varies by series and changes without warning.
- **Convert cents to decimal fractions on write.** API returns cents; CSV stores `0.53`.
- **`close_et_hour` must use `zoneinfo.ZoneInfo("America/New_York")`,** never a hardcoded
  UTC-5. Get this wrong and the entire time-of-day analysis silently shifts by an hour when
  DST changes — which is exactly the analysis this exists to support.
- **Set the VM clock to UTC.** One unambiguous clock in logs; convert to ET in code only.
- **Every run outcome goes to `runs.log`,** including failures. A silent gap is worse than a
  recorded failure. `no_market_found` must be its own logged outcome, distinguishable from a
  network error — if the series gets renamed, that needs to look different from a timeout.

## Output

### Local CSV — the system of record

Two append-only files, joined on `capture_id` + `market_ticker`. Two files rather than one
sidesteps in-place CSV mutation entirely.

**`snapshots.csv`** — one row per bucket per hour, written at `HH:56`:

```
capture_id, close_utc, close_et_hour, minutes_to_close, event_ticker, market_ticker,
floor_strike, cap_strike, bucket_width, yes_bid, yes_ask, last_price, volume,
open_interest, ref_spot, ref_source, spot_offset_from_floor, ladder_ask_sum, n_buckets
```

**`settlements.csv`** — one row per bucket per hour, written by the backfill sweep:

```
capture_id, market_ticker, settlement_value, move, result, reconstruction_ok,
n_samples, sample_span_seconds, sample_stdev
```

Key columns:

| Column | Meaning |
|---|---|
| `capture_id` | ISO8601 UTC of the snapshot. Groups all buckets in one hour. |
| `close_et_hour` | 0-23, closing hour in Eastern. The seasonality key. |
| `bucket_width` | `cap - floor`. Identifies the wide 5pm hours. |
| `spot_offset_from_floor` | `ref_spot - floor_strike`. Drives coverage symmetry. |
| `ladder_ask_sum` | Sum of `yes_ask` across the whole ladder. The overround measure. |
| `move` | `settlement_value - ref_spot`. **The continuous variable the whole exercise rests on.** |
| `result` | 1 = this bucket settled YES, 0 = NO, blank = pending backfill. |
| `n_samples` | Price observations contributing to the reconstructed average — 1-second grid points populated by real trades. |
| `sample_span_seconds`, `sample_stdev` | Window health. Distinguishes a clean reconstruction from a thin or violent one. Without these you cannot tell a good average from a bad one after the fact. |

### Google Sheet — the live view and offsite copy

Append to a Google Sheet matching the `Data log` tab in `kalshi-sizing.xlsx` — the 23
snapshot and settlement columns, then `n_samples`, `sample_span_seconds`, `sample_stdev`.

- **Service account, not an API key.** API keys only authorise reads of public documents.
  Create a service account, download the JSON key, share the sheet with the service
  account's email address. Headless forever, no OAuth flow.
- The JSON key is the only credential on the box. It can touch one spreadsheet and nothing
  else. It must never be committed to a repo.
- Batch all ~20 buckets into one `spreadsheets.values.append` per run. Far below rate limits.
- **CSV write happens first and must succeed independently.** A Sheets failure at 3am must
  never lose a row. No API call belongs in the analysis path.
- **The Sheet must be working from day one, before there is data worth losing.** It is the
  offsite copy, and on a free-tier VM that is not optional.
- At ~480 rows/day the sheet reaches ~175k rows in a year and range formulas get sluggish.
  Keep the log tab formula-free, put calibration formulas on a separate tab, plan to roll to
  a new tab periodically.

## Hosting

**OCI Always Free VM. Prefer Ampere A1 (2 OCPU / 12GB); fall back to AMD Micro
(1 OCPU / 1GB) if A1 capacity is unavailable in region.** Either is vastly more than this
workload needs — it uses roughly 50MB and negligible CPU. A1 is ARM, so dependencies need
aarch64 wheels; `requests`, `gspread`, and `pandas` all ship them.

Note the A1 allowance was halved on 15 June 2026 from 4 OCPU / 24GB to 2 OCPU / 12GB. The
two AMD Micros are a **separate** allowance, not carved out of the A1 grant, so both can run
simultaneously.

- **systemd service, not cron.** `Restart=always`, `RestartSec=30`. The process manages its
  own wall-clock marks. A crash at 3am self-heals in 30 seconds instead of losing every hour
  until noticed.
- **Heartbeat.** Ping a Healthchecks.io check on each successful snapshot. On a VM, silence
  looks exactly like success.
- **If the sports scanner is ever added, put it on a separate AMD Micro,** not this box. It
  needs Kalshi credentials and this collector must never be able to reach them.

### Idle reclamation — accepted risk, documented remedy

Oracle may reclaim idle Always Free compute instances, judged over a 7-day period on CPU
95th percentile, network utilization, and memory utilization — with **memory applying to A1
shapes only**. Current docs put the CPU threshold at 20%; older community reports cite 10%,
so the numbers move. **The conditions are ANDed: exceeding any one of them clears you.**

This collector will sit well below all three. **The decision is to ship anyway and react if
flagged**, on the basis that the perishable/durable split plus the Google Sheet means being
reclaimed costs a VM and a day of snapshots, not the dataset.

Do not pre-emptively add artificial load. If a stop notification arrives, escalate in this
order:

1. **On A1 — hold resident memory above the threshold** (>20% of 12GB ≈ 2.4GB). Cheapest
   lever by far: no CPU burn, no fake traffic. Give it real work where possible — keep
   recent trade history and the snapshot dataset resident in memory rather than re-querying
   — and pad the remainder.
2. **On AMD Micro — memory is not a criterion**, so this lever does not exist and the
   instance is *harder* to keep alive despite the folklore that Micros are reclaimed less
   often. The E2.1.Micro is burstable off a 1/8 OCPU baseline, so sustained CPU load gets
   throttled. If flagged here, migrating to A1 is a better move than fighting it.
3. **Upgrade to Pay-As-You-Go.** Removes idle reclamation entirely while Always Free
   resources stay uncharged. Set a budget alert at $1 as a tripwire. Held in reserve because
   it introduces billing exposure.
4. **Rebuild elsewhere.** The process is self-timed and host-agnostic by design; moving it
   is a systemd unit and a credentials file. Run the backfill sweep on startup and every
   settlement since the outage repopulates automatically. Only the snapshots from the gap
   are lost.

**Whatever happens, the reclamation risk never reaches zero** — Oracle halved the free tier
this summer by silently editing a documentation page with no announcement. Protection comes
from the Sheet and the backfill, not from defeating the check.

## Build order

1. Step 0 verification. Report findings before writing code.
2. `collector.py` — snapshot path first, with retries and `runs.log`.
3. Backfill sweep, including the startup catch-up.
4. Google Sheets append, added once CSV writing is proven.
5. systemd unit and heartbeat.
6. `analyze.py` — **the actual deliverable.**

## Acceptance criteria

1. Runs unattended for 72 hours with no gaps in `runs.log`.
2. `snapshots.csv` has one row per bucket per hour, both bid and ask populated.
3. `reconstruction_ok` true for >95% of settled hours.
4. `ladder_ask_sum` between 1.00 and 1.20. **Early tripwire** — wildly outside that band
   almost certainly means the ladder query is picking up markets from the wrong event, which
   would silently poison the dataset.
5. `n_samples` at least 55 for >95% of hours.
6. Killing the process for two hours and restarting it results in those hours' settlements
   being backfilled automatically, with only the snapshots missing. **Test this deliberately
   before trusting it.**
7. `analyze.py` reads both CSVs and prints: settled-hour count; standard deviation of `move`,
   overall and grouped by `close_et_hour`; and a calibration table of ask bands versus
   realized YES rate.

Criterion 7 is the deliverable. Everything else is scaffolding for it.

## Analysis notes

- **Estimate sigma from `move`, not from win rates.** Continuous data converges far faster:
  ~200-300 settled hours for a usable sigma, versus ~780 to distinguish a 53% from a 58% win
  rate binomially.
- Fit a Student-t (start at nu around 4) alongside a normal. Five-minute crypto returns are
  fat-tailed and a normal fit overstates the odds of staying inside a range.
- Effective variance is `sigma^2 * (T - 2d/3)` with d = 1 minute, because of the 60-second
  averaging — at T = 4 min the effective window is 3.33 min, about 9% less sigma than a naive
  point-settlement model. **Whether the market prices this is the leading candidate edge.**
  Test it explicitly by comparing both models' calibration.
- Outcomes are roughly independent hour to hour, but **model errors are not** — vol clusters,
  so consecutive hours are mispriced in the same direction. Effective sample size is below row
  count. Block-bootstrap by day rather than assuming iid.
- `minutes_to_close` varies with scheduling drift. Once there is enough data, check whether
  calibration differs by entry time — that is a free finding the design produces as a
  byproduct.
