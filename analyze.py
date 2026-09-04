#!/usr/bin/env python3
"""The actual deliverable. Everything else is scaffolding for this.

Reads snapshots.csv + settlements.csv and reports:
  - count of settled hours
  - stdev of `move`, overall and by close_et_hour
  - a calibration table: ask bands vs realized YES rate
  - data-health checks that would otherwise silently invalidate the above
"""
import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path


def read(p):
    p = Path(p)
    if not p.exists():
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--live-only", action="store_true",
                    help="calibration over buckets that had a bid (the tradeable ladder)")
    a = ap.parse_args()

    snaps = read(Path(a.data) / "snapshots.csv")
    setts = read(Path(a.data) / "settlements.csv")
    if not snaps:
        print("no snapshots yet")
        return

    sett = {(r["capture_id"], r["market_ticker"]): r for r in setts}
    hours = {}
    for r in setts:
        hours.setdefault(r["capture_id"], r)

    print("=" * 72)
    print("KALSHI HOURLY BTC — CALIBRATION REPORT")
    print("=" * 72)
    print(f"\nsnapshot rows      : {len(snaps):,}")
    print(f"snapshot hours     : {len({r['capture_id'] for r in snaps}):,}")
    print(f"settled hours      : {len(hours):,}")

    # ---------------------------------------------------------------- health
    print("\n--- DATA HEALTH " + "-" * 56)
    ok = [r["reconstruction_ok"] for r in hours.values() if r["reconstruction_ok"] != ""]
    if ok:
        rate = sum(1 for v in ok if v == "1") / len(ok)
        flag = "OK" if rate > 0.95 else "*** BELOW 95% — reference feed suspect ***"
        print(f"reconstruction_ok  : {rate:.1%} of {len(ok)} hours   {flag}")

    bas = [f(r["basis"]) for r in hours.values() if f(r["basis"]) is not None]
    if bas:
        m = statistics.mean(bas)
        s = statistics.pstdev(bas) if len(bas) > 1 else 0.0
        print(f"basis vs BRTI      : mean ${m:+.2f}  sd ${s:.2f}  (n={len(bas)})")
        print(f"                     a CONSTANT basis does not affect sigma; the sd above")
        print(f"                     is the part that does.")

    bid_sums = [f(r["ladder_bid_sum"]) for r in snaps if f(r["ladder_bid_sum"]) is not None]
    ask_live = [f(r["ladder_ask_sum_live"]) for r in snaps
                if f(r["ladder_ask_sum_live"]) is not None]
    if bid_sums:
        bad = sum(1 for v in bid_sums if not (0.70 <= v <= 1.00))
        print(f"ladder_bid_sum     : median {statistics.median(bid_sums):.3f}  "
              f"outside [0.70,1.00]: {bad/len(bid_sums):.1%}")
    if ask_live:
        bad = sum(1 for v in ask_live if not (1.00 <= v <= 1.20))
        print(f"ladder_ask_sum_live: median {statistics.median(ask_live):.3f}  "
              f"outside [1.00,1.20]: {bad/len(ask_live):.1%}   "
              f"{'*** WRONG-EVENT TRIPWIRE ***' if ask_live and bad/len(ask_live) > 0.1 else ''}")

    # ---------------------------------------------------------------- sigma
    print("\n--- SIGMA OF `move` " + "-" * 52)
    print("(estimated from the continuous variable, not win rates: ~200-300 hours")
    print(" for a usable sigma vs ~780 to separate a 53% from a 58% win rate)\n")

    moves, by_hour = [], defaultdict(list)
    hour_of = {r["capture_id"]: r["close_et_hour"] for r in snaps}
    for cid, r in hours.items():
        mv = f(r["move"])
        if mv is None:
            continue
        moves.append(mv)
        by_hour[hour_of.get(cid, "?")].append(mv)

    if len(moves) < 2:
        print("  not enough settled hours yet")
    else:
        s = statistics.stdev(moves)
        print(f"  overall      n={len(moves):4d}   sigma=${s:8.2f}   mean=${statistics.mean(moves):+8.2f}")
        se = s / math.sqrt(2 * (len(moves) - 1))
        print(f"               naive 95% CI on sigma: ${s-1.96*se:.2f} - ${s+1.96*se:.2f}")
        print("               (naive: vol clusters, so block-bootstrap by day for real")
        print("                error bars — effective n is below row count)\n")
        print("  by ET hour:")
        print("    hour     n      sigma       mean")
        for h in sorted(by_hour, key=lambda x: int(x) if str(x).isdigit() else 99):
            v = by_hour[h]
            if len(v) < 2:
                print(f"    {str(h):>4}  {len(v):4d}          -          -")
            else:
                print(f"    {str(h):>4}  {len(v):4d}   ${statistics.stdev(v):8.2f}   ${statistics.mean(v):+8.2f}")

    # ---------------------------------------------------------- calibration
    print("\n--- CALIBRATION: ASK BAND vs REALIZED YES RATE " + "-" * 25)
    print("(priced off the ASK — what you would actually pay. Never the mid.)\n")

    bands = defaultdict(lambda: {"n": 0, "ask": 0.0, "yes": 0})
    for r in snaps:
        if a.live_only and r.get("is_live") != "1":
            continue
        s = sett.get((r["capture_id"], r["market_ticker"]))
        if not s or s["result"] == "":
            continue
        ask = f(r["yes_ask"])
        if ask is None or ask <= 0:
            continue
        b = min(int(ask * 10), 9)
        bands[b]["n"] += 1
        bands[b]["ask"] += ask
        bands[b]["yes"] += 1 if s["result"] == "1" else 0

    if not bands:
        print("  no settled bucket-level data yet")
    else:
        print("    band        n    mean_ask   realized    edge")
        print("    " + "-" * 46)
        for b in sorted(bands):
            d = bands[b]
            ma = d["ask"] / d["n"]
            rz = d["yes"] / d["n"]
            print(f"    {b*10:2d}-{b*10+10:3d}%  {d['n']:6d}    {ma:7.4f}   {rz:8.4f}  {rz-ma:+7.4f}")
        print("\n    edge = realized - mean_ask. Positive means the ask underprices the")
        print("    bucket. Block-bootstrap by day before believing any of it.")
    print()


if __name__ == "__main__":
    main()
