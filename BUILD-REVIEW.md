# Kalshi hourly BTC collector — build review & prerequisites

Review of `kalshi-collector-spec.md` before any code is written, incorporating six
revisions agreed after the first pass. Covers: what the project is, the corrections that
supersede the original spec, what I need from you (and whether each ask is *actually*
required), and the Oracle Cloud setup.

Status: repo is empty, no commits. Nothing built yet.

---

## 1. Understanding of the project

A **measurement instrument, not a trading bot.** It answers exactly one question:

> When Kalshi prices a $100 BTC bucket at X¢, how often does BTC actually settle in
> that bucket?

**The settlement value is reconstructed, not read.** This is the load-bearing idea.
Kalshi only reports *which $100 bucket* won, which censors the outcome to a $100
interval — far too coarse to estimate a ~$60 sigma from. So the collector independently
reconstructs the settlement value by averaging reference prices over the final 60
seconds, mirroring the documented settlement mechanic.

`reconstruction_ok` is the integrity check: the reconstructed value must fall inside the
bucket that actually settled YES. If that flag is false more than ~5% of the time, the
reference feed is wrong and every sigma estimate downstream is untrustworthy.

**`move` (= `settlement_value − ref_spot`) is the real payload.** Everything else is
scaffolding for it. Continuous data converges in ~200–300 settled hours; distinguishing a
53% from a 58% win rate binomially would need ~780.

### Discipline constraints that matter more than the code

Most likely to be quietly violated during implementation, so stated explicitly:

- **Never store or derive a midpoint.** Store `yes_bid` and `yes_ask` separately. You buy
  at the ask; edge computed off the mid is phantom edge that evaporates in live trading.
- **No filtering at collection time.** Record every hour, including the 5pm ET expiry with
  its wider buckets. Filter in analysis, never in collection.
- **No strategy logic in the collector.** No two-bucket selection, no 0.75 filter, no
  position sizing. Record the full ladder; apply rules later.
- **Resolve series → event → markets on every run.** Never hardcode an event ticker.
- **Read bucket bounds from `floor_strike` / `cap_strike`.** Never parse the ticker string.
- **Convert cents → decimal fractions on write.** CSV stores `0.53`, not `53`.
- **Two files, not one.** `snapshots.csv` and `settlements.csv`, joined on
  `capture_id` + `market_ticker`. Sidesteps in-place CSV mutation entirely.
- **No order placement, ever. No credentials that could place an order.**

### The overnight-coverage argument

Missing the 2am–6am ET hours isn't a random gap, it's a **systematic bias toward
high-volatility hours** that inflates the sigma estimate. This is a correctness
requirement, not a convenience one, and it's what rules out a laptop.

### The actual deliverable

Acceptance criterion 5 — the analysis script printing settled-hour count, standard
deviation of `move` (overall and by `close_et_hour`), and a calibration table of ask bands
vs. realized YES rate. The collector is scaffolding for that script.

---

## 2. Corrections superseding the original spec

Six revisions, ordered by how much damage they'd do unnoticed.

### 2.1 — Reference price must be a multi-exchange composite, not Coinbase alone

**Supersedes:** the spec's "an exchange mid is a proxy… acceptable noise."

That framing is wrong. BRTI is a **volume-weighted composite across multiple constituent
exchanges with outlier filtering**. A single venue carries a **persistent basis** against
it — not zero-mean noise. A persistent basis shifts *every* `move` in the same direction,
which systematically misclassifies buckets near their boundaries. It would never be
visible as noise in the data; it would just quietly bias the answer.

**Use an equal- or volume-weighted average of three BRTI constituents: Coinbase, Kraken,
Bitstamp.** Costs one extra HTTP call per sample and removes a bias you'd otherwise never
detect.

> **Consequence worth flagging:** the composite must be used on **both legs**. `move` is
> `settlement_value − ref_spot`. Basis cancels out of that subtraction only if both terms
> come from the same composite. Computing `ref_spot` from Coinbase alone while computing
> `settlement_value` from the 3-venue composite would reintroduce the exact bias this
> change exists to remove — in a form that looks like real BTC movement.

`ref_source` records which composite and weighting was used, so a later switch to true
BRTI is correctable rather than a break in the series.

### 2.2 — Settlement capture moves to the next hour's run

**Supersedes:** the spec's `T + 2 minutes` settlement capture.

`T+2` is optimistic. Kalshi markets don't reliably reach settled status that fast, and the
collector would silently record nulls that are indistinguishable from genuine failures.

**New run structure**, one job per hour:

| Time | Action |
|---|---|
| `HH:40` | Wake. Immediately capture settlement for the **previous** hour's market — 40+ minutes stale, guaranteed final. |
| `HH:56` | Decision snapshot: full ladder + `ref_spot`. |
| `HH:59:00 – HH+1:00:00` | Sample the reference composite for the final 60 seconds. |
| `HH+1:00:00` | Exit. |

The job now ends exactly at the close rather than spanning into the next hour. **Overlap
risk goes to zero** and settlement is never captured too early.

> **Consequence worth flagging:** the most recent hour is always unsettled until the next
> run completes. If the collector stops permanently, the final hour's settlement is never
> captured. Cosmetic for a long run; just don't mistake that trailing blank for a bug.

### 2.3 — Timezone conversion must be tz-aware

`close_et_hour` must be derived via `zoneinfo.ZoneInfo("America/New_York")`, **never** a
hardcoded UTC−5 offset. A fixed offset silently shifts the entire time-of-day analysis by
one hour across the November DST transition — and time-of-day seasonality is precisely the
analysis this project exists to produce. Store `close_utc` as the canonical value and
derive ET from it at write time.

### 2.4 — Log sampling metadata, not just the average

One-per-second HTTP polling yields 55–60 samples at irregular spacing with occasional
timeouts. An average alone can't distinguish a clean window from a degraded one.

**Add columns:** `n_samples`, `sample_span_seconds`, `sample_stdev`.

**Better: use websocket ticker subscriptions for that final minute** instead of REST
polling. Every tick, no rate-limit exposure, and a materially better average. Public
market-data websockets on all three venues are unauthenticated. REST polling stays as the
fallback path if a socket fails to connect or drops mid-window — with `n_samples` and
`ref_source` recording which path was actually used.

> **Addition I'd make:** `ref_spot` at `HH:56` should be a short average too — say 5
> seconds — rather than one instantaneous tick. `move` is `settlement_value − ref_spot`,
> so single-tick noise in `ref_spot` propagates directly into `move` and **inflates the
> sigma estimate** with variance that isn't real BTC movement. Cheap to fix, and it's the
> headline number.

### 2.5 — "No market found" is an outcome, not an exception

If the series gets renamed, or an hour genuinely has no event, that must be a **logged
outcome row** in `runs.log` — not an uncaught exception that looks identical to a network
failure. Enumerate the outcomes explicitly so gaps are diagnosable after the fact:

`OK` · `NO_MARKET_FOUND` · `NETWORK_FAIL` · `LATE_WAKE` · `PARTIAL_SAMPLES` ·
`SETTLEMENT_NOT_FINAL`

A silent gap is worse than a recorded failure; an ambiguous one is nearly as bad.

### 2.6 — Hosting is Oracle Cloud

Decided. See §4. This moots the GitHub Actions cron-jitter problem entirely — a VM runs a
self-scheduled process with no jitter — and also moots the public-vs-private repo question,
since the CSVs live on the VM rather than in git.

---

## 3. What I need from you

Short version: **no API keys of any kind**, and no hard blockers.

| Ask | Truly necessary? | Detail |
|---|---|---|
| Kalshi API key | **No.** | Public market data needs no authentication, and the design forbids holding credentials that could place an order. Do not create one. |
| Coinbase / Kraken / Bitstamp keys | **No.** | All three expose public market data — REST and websocket — without authentication. |
| CF Benchmarks BRTI access | **No — but tell me either way.** | BRTI is the true settlement index but is generally licensed/paid. The 3-venue composite (§2.1) is the fallback and is good enough to remove the single-venue basis. If you *do* have access, it's a straight upgrade and `ref_source` makes the transition traceable. |
| Language choice | **No.** | Defaulting to Python (stdlib + `requests` + a websocket client). |
| Live verification of Kalshi endpoints | **No, but recommended.** | The spec notes its endpoint list came from secondary sources. I'd hit the live API once to confirm series discovery and field names before building on assumptions. |
| **Oracle Cloud setup** | **Yes — see §4.** | The only section with real action items for you. |

Absent further input I proceed with: 3-venue composite, Python, Oracle Cloud, and a live
endpoint check first.

---

## 4. Oracle Cloud — what's needed from you

### 4.1 Account signup

- **A credit or debit card.** Required for identity verification. Always Free resources
  are not charged, but the card is mandatory to create the account.
- **A phone number** for SMS verification.
- **Home region choice — this is permanent and cannot be changed later.** Pick one
  geographically near you. Region choice also determines capacity availability (below).

### 4.2 Instance shape — pick the boring one

| Shape | Spec | Verdict |
|---|---|---|
| **VM.Standard.E2.1.Micro** (AMD) | 1 OCPU, 1 GB RAM | **Recommended.** Always available, no capacity fights. This workload is a Python script that sleeps most of the hour — 1 GB is ample. |
| VM.Standard.A1.Flex (ARM Ampere) | up to 4 OCPU, 24 GB | Far more machine than needed, and **"out of capacity" errors are the single most common Oracle Free Tier frustration** in popular regions. Not worth the hassle here. |

**OS:** Ubuntu 22.04 or 24.04 LTS. Leave the VM clock on UTC — the code is tz-aware and
derives ET itself (§2.3).

### 4.3 The gotcha that will actually bite you — and why `look_busy()` is the wrong fix

**Oracle reclaims idle Always Free compute instances.** A collector that sleeps ~50
minutes of every hour is exactly the profile that gets flagged.

Oracle's published criteria: an Always Free instance is considered idle when, across a
**7-day** window, CPU utilization at the **95th percentile is under 20%** *and* network
utilization is under 20% (memory is a third condition, but only on A1 ARM shapes). All
conditions must hold simultaneously, so clearing any single one is enough. Oracle then
**stops** the instance rather than deleting it — recoverable, but for an unattended
collector a stopped instance is a multi-day silent gap, which is precisely the systematic
coverage bias this project cares about most.

#### Why not `look_busy()`

An artificial keepalive is the intuitive fix and it's the wrong one:

- **It has to be a real burn, not a heartbeat.** Beating a *95th-percentile* CPU threshold
  over 7 days means sustaining >20% of a core essentially continuously. On a 1 OCPU micro
  that is a permanent busy-loop, not a token ping. A periodic nudge doesn't move a p95.
- **The network threshold is worse.** Sustained bandwidth is a far more expensive way to
  look busy than CPU, so CPU is the only realistic lever — and it's still a real one.
- **The thresholds are Oracle's, not a contract.** They can be retuned without notice.
  Anything calibrated to sit just above a published number is fragile by construction.
- **It's the thing the policy exists to catch.** Burning cycles to appear active on a free
  instance is a gray-area workaround for capacity you're being given for nothing.
- **It buys nothing over the sanctioned fix, which is free.**

#### The actual fix

**Upgrade the account to Pay As You Go.** Always Free resources remain free under PAYG —
you are not charged for staying inside the free allotments — but the tenancy becomes
**exempt from idle-instance reclamation entirely**. One setting, no cost, no ongoing
fragility, no wasted cycles. Do it before you start collecting, not after you lose a week
of data.

If you'd still rather not convert the account, the honest fallback isn't `look_busy()` —
it's §4.7. Detecting a stop within the hour is worth more than trying to prevent one.

### 4.4 Networking

**Nothing to configure.** The collector makes outbound HTTPS/WSS calls only. No inbound
ports, no security-list ingress rules, no load balancer. Leave the default egress rules
alone. The only inbound access you need is SSH on 22, which the default VCN already
permits.

### 4.5 What I need from you specifically

1. **SSH access, or not — your call.**
   - *You grant me access:* provide the instance's public IP and put an SSH key I can use
     on the box. I do the full setup and verify it end to end.
   - *You keep the keys:* I write the code plus a single idempotent `setup.sh`, you run
     it and paste back the output. Slower to iterate, and you retain sole control of the
     machine. Given this VM holds no credentials and touches no funds, either is defensible.
2. **The public IP and OS image you chose**, once the instance is up.
3. **Confirmation you've upgraded to PAYG** (§4.3), or an explicit decision to accept the
   reclamation risk and rely on the §4.7 dead-man's switch to catch it instead.
4. **An email address for the dead-man's-switch alerts** (§4.7), if you want that wired up.

### 4.6 How the process will be managed

A **long-running Python process under systemd** with `Restart=always`, rather than a cron
or systemd timer per hour. Reasons: the process manages its own wall-clock marks (§2.2) so
there's no scheduler jitter at all; systemd restarts it on crash; and `journalctl` gives
you a second, independent record alongside `runs.log`.

**Data durability:** CSVs on a single free-tier VM are a single point of failure. I'll add
a daily `git push` of the CSVs to a private repo, or an `rclone`/`rsync` copy off-box —
your preference. Losing 300 hours of collection to a disk event would be avoidable and
extremely annoying.

### 4.7 Dead-man's switch — worth more than any keepalive

Whatever we do about §4.3, the collector should **ping an external monitor at the end of
every successful run** (healthchecks.io has a free tier sized for exactly this). If no
ping arrives within the grace window, you get alerted.

This is strictly more valuable than `look_busy()`, because it catches *every* failure mode
with one mechanism — instance reclaimed, process crashed with systemd unable to restart it,
VM network dead, Kalshi series renamed, disk full — rather than defending against one of
them. It also runs no risk of being silently defeated by a threshold change.

The failure this project cannot tolerate is a **long unnoticed gap**, since gaps aren't
random with respect to time of day. A dead-man's switch converts any such gap from
"discovered weeks later during analysis" into "an alert within the hour."

---

## 5. Proposed build order

1. **Live check of the Kalshi API** — confirm base URL, series discovery, and exact field
   names on the market object. Docs are the source of truth, not the spec's endpoint list.
2. **Reference price module** — 3-venue composite (§2.1), websocket-first with REST
   fallback (§2.4), used for *both* `ref_spot` and `settlement_value`.
3. **`collector.py`** — self-timed long-running process on the §2.2 schedule. Writes
   `snapshots.csv`, `settlements.csv`, `runs.log` with the §2.5 outcome codes. Retry with
   backoff on every network call; a missed capture is a permanently lost row.
4. **Deploy to Oracle** — systemd unit, off-box backup.
5. **`analyze.py`** — acceptance criterion 5. The actual deliverable.
6. **Watch the first 72 hours** against the acceptance criteria: no gaps in `runs.log`,
   `reconstruction_ok` >95%, `ladder_ask_sum` within 1.00–1.20.

`ladder_ask_sum` is worth treating as an early tripwire — a value wildly outside 1.00–1.20
almost certainly means the ladder query is picking up markets from the **wrong event**,
which would silently poison the entire dataset while every individual row still looks
plausible.
