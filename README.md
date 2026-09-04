# Kalshi hourly BTC collector

A **measurement instrument, not a trading bot.** It places no orders and holds no Kalshi
credentials. It answers one question:

> When Kalshi prices a $100 BTC bucket at X¢, how often does BTC actually settle there?

See [SPEC.md](SPEC.md) for the design and [STEP0-FINDINGS.md](STEP0-FINDINGS.md) for the
live-API verification that amended it.

## Architecture

Perishable vs durable drives everything:

| Mark | Job | Criticality |
|---|---|---|
| `HH:56` | **Snapshot** — full ladder + `ref_spot` | Perishable. The ladder exists for 4 minutes then is gone. Retries, heartbeat, alerting. |
| `HH:20` | **Backfill sweep** — settlement for any pending hour | Durable. Kalshi keeps settled events addressable by ticker; best effort, retries next hour. |
| startup | Backfill immediately | Catches up after restart, reclaim, or rebuild. |

A day of downtime costs that day's snapshots and nothing else.

**The perishable set is exactly the T−4 quote state:** `yes_bid`, `yes_ask`, `volume`,
`open_interest`. Everything else — strikes, `result`, settlement value, even `ref_spot` —
is reconstructable after the fact.

## Settlement comes from Kalshi, exactly

`expiration_value` on the settled market object is the **true BRTI settlement value to the
cent**, populated on every market in the event. No trade reconstruction is needed for the
settlement leg.

The three-venue composite (Coinbase + Kraken + Bitstamp) is retained as a **basis
diagnostic**: `basis = expiration_value − composite_close`, measured every hour. That makes
`reconstruction_ok` a real check — does our composite land in the bucket Kalshi actually
settled YES? — rather than self-consistency.

`ref_spot` is now the only reconstructed quantity, and therefore the sole source of error
in `move`.

## Usage

```bash
pip install -r requirements.txt
python -m kalshi_collector.main selftest    # verify the environment first
python -m kalshi_collector.main snapshot    # one-shot
python -m kalshi_collector.main backfill    # one-shot
python -m kalshi_collector.main run         # long-running (systemd)
python analyze.py --data data                # the deliverable
```

## Deployment

```bash
sudo ./deploy/setup.sh https://github.com/<you>/kalshi-EV-tracker.git
sudo -u kalshi /opt/kalshi-collector/.venv/bin/python -m kalshi_collector.main selftest
sudo systemctl enable --now kalshi-collector
```

Configuration via `/opt/kalshi-collector/.env` — see [deploy/env.example](deploy/env.example).

### Shipping a code change

You never hand-edit files on the box. `.env`, `service-account.json` and `data/` are
untracked and excluded from every sync, so deploys never touch your credentials or your
data.

**With a git remote (recommended):**

```bash
git push                                        # from your laptop
ssh <vm> 'sudo /opt/kalshi-collector/deploy/update.sh'
```

**Without one:**

```bash
./deploy/push.sh opc@<vm-ip>                    # rsync + deploy in one step
```

`update.sh` pulls, installs deps, runs the self-test against the live APIs, and only then
restarts. **If the self-test fails it leaves the running service untouched** rather than
replacing a working collector with a broken one.

It also **refuses to restart between HH:53 and HH:58**, because the ladder is perishable —
a restart through `HH:56` costs that hour permanently. Settlements are durable and
repopulate via the startup backfill, so nothing else is at risk. Override with `FORCE=1`
if you must.

## Output

`data/snapshots.csv` holds the **full ladder** (~188 buckets/hour), unfiltered.
`data/settlements.csv` joins on `capture_id` + `market_ticker`. `data/runs.log` records
every outcome including failures — a silent gap is worse than a recorded failure, and
`no_market_found` is distinguishable from a timeout.

The Google Sheet receives **live buckets only** (`yes_bid > 0`, ~11–14/hour). The full
188-bucket ladder would hit Sheets' hard 10M-cell ceiling in ~79 days.

## Discipline constraints

- **Never store or derive a midpoint.** `yes_bid` and `yes_ask` are separate columns. You
  buy at the ask; edge computed off the mid is phantom edge.
- **Prices are already decimal dollars.** The API returns `"0.0200"`. Do **not** divide by
  100 — that would silently destroy every price while each row still looked valid.
- **No filtering at collection time.** Filtering happens on the way to the Sheet, never on
  the way to disk; `ladder_bid_sum` and the wrong-event tripwire need every bucket.
- **`close_et_hour` uses `zoneinfo`,** never a fixed offset, or the time-of-day analysis
  silently shifts an hour at the DST change.
- **Cursor pagination is mandatory** — `limit=200` truncates a 238-market ladder.
- **Series is `KXBTC`.** `KXBTCD` is also hourly, also BTC, and a different product.

## Tripwire

`ladder_ask_sum` cannot sit in 1.00–1.20: ~174 of 188 buckets carry the $0.01 minimum ask,
so it scales with `n_buckets` (measured 2.99–3.34). Use instead:

- `ladder_bid_sum ≤ 1.0 ≤ ladder_ask_sum` — no-arbitrage bracket
- `ladder_ask_sum_live ∈ [1.00, 1.20]` — measured 1.08 live

A value wildly outside means the ladder query is picking up the wrong event, which would
silently poison the dataset while every row still looked plausible.
