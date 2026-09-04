"""Append-only CSV -- the system of record.

Two files joined on capture_id + market_ticker, which sidesteps in-place CSV
mutation entirely.

snapshots.csv holds the FULL ladder, unfiltered. Filtering happens on the way to
the Sheet, never on the way to disk: ladder_bid_sum and the wrong-event tripwire
both require every bucket.
"""
import csv
import logging
import os
from datetime import datetime, timezone

from .config import SNAPSHOTS_CSV, SETTLEMENTS_CSV, RUNS_LOG, DATA_DIR

log = logging.getLogger(__name__)

SNAPSHOT_COLS = [
    "capture_id", "close_utc", "close_et_hour", "minutes_to_close",
    "event_ticker", "market_ticker", "strike_type",
    "floor_strike", "cap_strike", "bucket_width",
    "yes_bid", "yes_ask", "last_price", "volume", "open_interest", "is_live",
    "ref_spot", "ref_source", "spot_offset_from_floor",
    "ladder_bid_sum", "ladder_ask_sum", "ladder_ask_sum_live",
    "n_buckets", "n_buckets_live",
]

SETTLEMENT_COLS = [
    "capture_id", "close_utc", "event_ticker", "market_ticker",
    "settlement_value", "move", "result", "reconstruction_ok",
    "composite_close", "basis", "basis_venues",
    "n_samples", "sample_span_seconds", "sample_stdev",
]


def _append(path, cols, rows):
    if not rows:
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    # Write through to disk before anything else can fail. A Sheets outage at
    # 3am must never cost a row.
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)
        fh.flush()
        os.fsync(fh.fileno())
    return len(rows)


def append_snapshots(rows):
    return _append(SNAPSHOTS_CSV, SNAPSHOT_COLS, rows)


def append_settlements(rows):
    return _append(SETTLEMENTS_CSV, SETTLEMENT_COLS, rows)


def _read(path):
    if not path.exists():
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def pending_captures():
    """Snapshot hours with no settlement rows yet, oldest first.

    Driven off the CSV rather than any in-memory state, so a restart or a full
    rebuild resumes correctly -- which is what makes the durable half of the
    design actually durable.
    """
    snaps = _read(SNAPSHOTS_CSV)
    done = {r["capture_id"] for r in _read(SETTLEMENTS_CSV)}
    seen, out = set(), []
    for r in snaps:
        cid = r["capture_id"]
        if cid in done or cid in seen:
            continue
        seen.add(cid)
        try:
            close = datetime.fromisoformat(r["close_utc"].replace("Z", "+00:00"))
        except Exception:
            continue
        if close >= datetime.now(timezone.utc):
            continue          # not closed yet
        out.append({
            "capture_id": cid,
            "close_utc": close,
            "event_ticker": r["event_ticker"],
            "ref_spot": float(r["ref_spot"]) if r.get("ref_spot") else None,
        })
    return sorted(out, key=lambda x: x["close_utc"])


def log_run(outcome, detail=""):
    """Every run outcome, including failures. A silent gap is worse than a
    recorded failure; no_market_found must not look like a timeout."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts}\t{outcome}\t{detail}\n"
    with open(RUNS_LOG, "a") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    log.info("run outcome: %s %s", outcome, detail)
