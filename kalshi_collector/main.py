"""Collector entry point. Long-running, self-timed against the wall clock.

Perishable vs durable drives everything:

  HH:56  snapshot  -- the ladder exists for four minutes then is gone forever.
                      Retried hard, heartbeated, alerted.
  HH:20  backfill  -- Kalshi keeps settled events addressable by ticker, so this
                      is best effort and simply retries next hour.
  startup backfill -- catches up after any restart, reclaim, or rebuild.

A day of downtime costs that day's snapshots and nothing else.
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from . import kalshi, refprice, sheets, store
from .config import (BACKFILL_MINUTE, ET, HEALTHCHECK_URL, SNAPSHOT_MINUTE)
from .store import SETTLEMENT_COLS, SNAPSHOT_COLS

log = logging.getLogger("collector")

# The Sheet's "Data log" tab predates the schema changes in STEP0-FINDINGS, so
# write its exact 23-column order rather than reshaping someone's spreadsheet.
# Settlement columns stay blank: settlement is DURABLE (recoverable from Kalshi
# forever), so the Sheet's job as offsite copy only has to protect the
# perishable half -- the quote state, which nothing can reconstruct.
SHEET_COLS = [
    "capture_id", "close_utc", "close_et_hour", "minutes_to_close",
    "event_ticker", "market_ticker", "floor_strike", "cap_strike",
    "bucket_width", "yes_bid", "yes_ask", "last_price", "volume",
    "open_interest", "ref_spot", "ref_source", "spot_offset_from_floor",
    "ladder_ask_sum", "n_buckets",
    "settlement_value", "move", "result", "reconstruction_ok",
]

# Written by the backfill sweep, not the snapshot.
SETTLEMENT_FIELDS = ["settlement_value", "move", "result", "reconstruction_ok"]


def _now():
    return datetime.now(timezone.utc)


def _heartbeat(ok=True):
    if not HEALTHCHECK_URL:
        return
    try:
        url = HEALTHCHECK_URL if ok else HEALTHCHECK_URL.rstrip("/") + "/fail"
        requests.get(url, timeout=10)
    except Exception as exc:
        log.warning("heartbeat failed: %s", exc)


# ---------------------------------------------------------------- snapshot
def snapshot(target_close=None):
    now = _now()
    if target_close is None:
        target_close = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    try:
        ladder = kalshi.get_ladder(target_close)
    except kalshi.NoMarketFound as exc:
        # Its own outcome. If the series is renamed this must not look like a timeout.
        store.log_run("no_market_found", str(exc))
        _heartbeat(ok=False)
        return 0
    except Exception as exc:
        store.log_run("snapshot_failed", f"{type(exc).__name__}: {exc}")
        _heartbeat(ok=False)
        return 0

    spot, ref_source, _per = refprice.live_spot()
    stats = kalshi.ladder_stats(ladder)

    capture_at = _now()
    capture_id = capture_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    minutes_to_close = round((target_close - capture_at).total_seconds() / 60.0, 3)

    rows = []
    for m in ladder:
        floor = m["floor_strike"]
        rows.append({
            "capture_id": capture_id,
            "close_utc": target_close.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "close_et_hour": target_close.astimezone(ET).hour,
            "minutes_to_close": minutes_to_close,
            "event_ticker": m["event_ticker"],
            "market_ticker": m["market_ticker"],
            "strike_type": m["strike_type"],
            "floor_strike": floor,
            "cap_strike": m["cap_strike"],
            "bucket_width": m["bucket_width"],
            "yes_bid": m["yes_bid"],
            "yes_ask": m["yes_ask"],
            "last_price": m["last_price"],
            "volume": m["volume"],
            "open_interest": m["open_interest"],
            "is_live": m["is_live"],
            "ref_spot": spot,
            "ref_source": ref_source,
            "spot_offset_from_floor": (round(spot - floor, 4)
                                       if (spot is not None and floor is not None) else None),
            **stats,
        })

    n = store.append_snapshots(rows)          # disk first, always
    live = [r for r in rows if r["is_live"]]
    sheets.append(live, SHEET_COLS)            # then the Sheet, never fatal

    store.log_run("snapshot_ok",
                  f"event={ladder[0]['event_ticker']} rows={n} live={len(live)} "
                  f"mtc={minutes_to_close} spot={spot} src={ref_source} "
                  f"bid_sum={stats['ladder_bid_sum']} ask_sum_live={stats['ladder_ask_sum_live']}")
    _heartbeat(ok=True)
    return n


# ---------------------------------------------------------------- backfill
def _settle_rows(pending, ladder, diag):
    exp = next((m["expiration_value"] for m in ladder if m["expiration_value"] is not None), None)
    if exp is None:
        return None

    comp = diag.get("composite_close")
    basis = round(exp - comp, 4) if comp is not None else None

    # The real integrity check: does OUR composite land in the bucket Kalshi
    # actually settled YES? Checking expiration_value against its own winner
    # would be self-consistency and prove nothing.
    winner = next((m for m in ladder if str(m["result"]).lower() == "yes"), None)
    ok = ""
    if comp is not None and winner is not None:
        lo, hi = winner["floor_strike"], winner["cap_strike"]
        ok = int((lo is None or comp >= lo) and (hi is None or comp <= hi))

    ref = pending["ref_spot"]
    out = []
    for m in ladder:
        res = str(m["result"]).lower()
        out.append({
            "capture_id": pending["capture_id"],
            "close_utc": pending["close_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event_ticker": m["event_ticker"],
            "market_ticker": m["market_ticker"],
            "settlement_value": exp,
            "move": round(exp - ref, 4) if ref is not None else None,
            "result": 1 if res == "yes" else (0 if res == "no" else ""),
            "reconstruction_ok": ok,
            "composite_close": comp,
            "basis": basis,
            "basis_venues": diag.get("basis_venues", ""),
            "n_samples": diag.get("n_samples", 0),
            "sample_span_seconds": diag.get("sample_span_seconds"),
            "sample_stdev": diag.get("sample_stdev"),
        })
    return out


def backfill(limit=48):
    pend = store.pending_captures()[:limit]
    if not pend:
        store.log_run("backfill_ok", "nothing pending")
        return 0
    filled = 0
    for p in pend:
        et = p["event_ticker"] or kalshi.event_ticker_for(p["close_utc"])
        try:
            ladder = kalshi.get_settled_event(et)
        except kalshi.NoMarketFound as exc:
            store.log_run("backfill_no_market", f"{et}: {exc}")
            continue
        except Exception as exc:
            store.log_run("backfill_failed", f"{et}: {type(exc).__name__}: {exc}")
            continue
        if ladder is None:
            log.info("%s not settled yet; will retry next sweep", et)
            continue

        diag = {}
        try:
            diag = refprice.reconstruct_close(p["close_utc"])
        except Exception as exc:
            log.warning("basis reconstruction failed for %s: %s", et, exc)

        rows = _settle_rows(p, ladder, diag)
        if rows is None:
            store.log_run("backfill_no_expiration_value", et)
            continue
        store.append_settlements(rows)
        # Mirror into the Sheet so the Calibration tab can join result to ask.
        sheets.update_settlements(
            p["capture_id"], {r["market_ticker"]: r for r in rows},
            SHEET_COLS, SETTLEMENT_FIELDS)
        filled += 1
        log.info("backfilled %s: settlement=%s basis=%s venues=%s",
                 et, rows[0]["settlement_value"], rows[0]["basis"], rows[0]["basis_venues"])
    store.log_run("backfill_ok", f"pending={len(pend)} filled={filled}")
    return filled


# ---------------------------------------------------------------- scheduler
def sheets_sync():
    """One-off: push settlement values into Sheet rows that were appended before
    the settlement half existed. Idempotent -- safe to re-run."""
    import csv as _csv
    from .config import SETTLEMENTS_CSV
    if not SETTLEMENTS_CSV.exists():
        log.info("no settlements.csv yet")
        return 0
    with open(SETTLEMENTS_CSV, newline="") as fh:
        rows = list(_csv.DictReader(fh))
    by_capture = {}
    for r in rows:
        by_capture.setdefault(r["capture_id"], {})[r["market_ticker"]] = r
    total = 0
    for cid, by_ticker in sorted(by_capture.items()):
        n = sheets.update_settlements(cid, by_ticker, SHEET_COLS, SETTLEMENT_FIELDS)
        log.info("synced %s -> %d sheet rows", cid, n)
        total += n
    store.log_run("sheets_sync", f"captures={len(by_capture)} rows_updated={total}")
    return total


def _next_mark(now):
    marks = []
    for minute, fn, name in ((SNAPSHOT_MINUTE, snapshot, "snapshot"),
                             (BACKFILL_MINUTE, backfill, "backfill")):
        t = now.replace(minute=minute, second=0, microsecond=0)
        if t <= now:
            t += timedelta(hours=1)
        marks.append((t, fn, name))
    return min(marks, key=lambda x: x[0])


def run_forever():
    store.log_run("startup", "running catch-up backfill")
    try:
        backfill()
    except Exception as exc:
        store.log_run("startup_backfill_failed", f"{type(exc).__name__}: {exc}")

    while True:
        now = _now()
        when, fn, name = _next_mark(now)
        wait = (when - now).total_seconds()
        log.info("next: %s at %s (%.0fs)", name, when.strftime("%H:%M:%SZ"), wait)
        time.sleep(max(1.0, wait))
        try:
            fn()
        except Exception as exc:
            log.exception("%s crashed", name)
            store.log_run(f"{name}_crashed", f"{type(exc).__name__}: {exc}")
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description="Kalshi hourly BTC collector")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "snapshot", "backfill", "selftest", "sheets-sync"])
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if a.command == "run":
        run_forever()
    elif a.command == "snapshot":
        snapshot()
    elif a.command == "backfill":
        backfill()
    elif a.command == "sheets-sync":
        sheets_sync()
    elif a.command == "selftest":
        from .selftest import run as st
        sys.exit(0 if st() else 1)


if __name__ == "__main__":
    main()
