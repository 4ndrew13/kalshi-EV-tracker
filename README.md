# Kalshi hourly BTC collector

A **measurement instrument, not a trading bot.** It places no orders and holds no Kalshi
credentials. It serves to answer this hypothesis:

**Hypothesis**
Hourly BTC range markets are priced off a point-settlement, thin-tailed, season-less model of a process that is actually 60-second-averaged, fat-tailed, and strongly time-of-day dependent — and the resulting calibration error is large enough, and structured enough, to survive paying the ask

See [SPEC.md](SPEC.md) for the design and [STEP0-FINDINGS.md](STEP0-FINDINGS.md) for the
live-API verification that amended it.

## Architecture

# Hosted on Oracle Cloud Instance. You can get your own [here](https://www.oracle.com/cloud/)

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

## Output

`data/snapshots.csv` holds the **full ladder** (~188 buckets/hour), unfiltered.
`data/settlements.csv` joins on `capture_id` + `market_ticker`. `data/runs.log` records
every outcome including failures — a silent gap is worse than a recorded failure, and
`no_market_found` is distinguishable from a timeout.

The Google Sheet receives **live buckets only** (`yes_bid > 0`, ~11–14/hour). The full
188-bucket ladder would hit Sheets' hard 10M-cell ceiling in ~79 days.

