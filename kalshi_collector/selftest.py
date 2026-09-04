"""Deployment self-test. Run FIRST on the VM: `python -m kalshi_collector.main selftest`.

The exchange endpoints could not be verified from the build machine (egress to
Coinbase/Kraken/Bitstamp was blocked there), so this is the gate that proves the
composite path actually works where it will run.
"""
import logging
from datetime import datetime, timedelta, timezone

from . import kalshi, refprice, sheets
from .config import SERIES_TICKER

log = logging.getLogger("selftest")
PASS, FAIL = "  PASS", "  FAIL"


def _check(name, fn):
    try:
        ok, detail = fn()
    except Exception as exc:
        print(f"{FAIL}  {name}: {type(exc).__name__}: {exc}")
        return False
    print(f"{PASS if ok else FAIL}  {name}: {detail}")
    return ok


def run():
    print(f"\nKalshi collector self-test  ({datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ})\n")
    results = []

    def kalshi_series():
        d = kalshi.fetch_markets(series_ticker=SERIES_TICKER, status="open")
        return len(d) > 0, f"{len(d)} open {SERIES_TICKER} markets (pagination followed)"
    results.append(_check("kalshi reachable + pagination", kalshi_series))

    def ladder():
        nxt = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0)
        rows = kalshi.get_ladder(nxt)
        s = kalshi.ladder_stats(rows)
        ok = s["n_buckets"] > 0 and s["n_buckets_live"] > 0
        return ok, (f"n={s['n_buckets']} live={s['n_buckets_live']} "
                    f"bid_sum={s['ladder_bid_sum']} ask_sum={s['ladder_ask_sum']} "
                    f"ask_sum_live={s['ladder_ask_sum_live']}")
    results.append(_check("next-hour ladder resolves", ladder))

    def settled():
        close = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) \
            - timedelta(hours=2)
        et = kalshi.event_ticker_for(close)
        rows = kalshi.get_settled_event(et)
        if not rows:
            return False, f"{et} not settled yet"
        exp = next((r["expiration_value"] for r in rows if r["expiration_value"]), None)
        win = next((r for r in rows if str(r["result"]).lower() == "yes"), None)
        inside = win and (win["floor_strike"] is None or exp >= win["floor_strike"]) \
            and (win["cap_strike"] is None or exp <= win["cap_strike"])
        return bool(exp and inside), f"{et} expiration_value={exp} winner={win and win['floor_strike']} inside={inside}"
    results.append(_check("settled event + expiration_value", settled))

    def spot():
        c, src, per = refprice.live_spot()
        spread = (max(per.values()) - min(per.values())) if len(per) > 1 else 0.0
        return (c is not None and len(per) >= 2), \
            f"composite={c} venues={src} max_venue_spread=${spread:.2f}"
    results.append(_check("live composite (>=2 venues)", spot))

    def recon():
        close = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) \
            - timedelta(hours=1)
        d = refprice.reconstruct_close(close)
        return d["n_samples"] >= 30, \
            (f"close={close:%H:%M}Z composite={d['composite_close']} "
             f"venues={d['basis_venues']} n_samples={d['n_samples']} "
             f"stdev={d['sample_stdev']}")
    results.append(_check("historical trade reconstruction", recon))

    def basis():
        close = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) \
            - timedelta(hours=1)
        rows = kalshi.get_settled_event(kalshi.event_ticker_for(close))
        if not rows:
            return False, "prior hour not settled yet"
        exp = next((r["expiration_value"] for r in rows if r["expiration_value"]), None)
        d = refprice.reconstruct_close(close)
        if exp is None or d["composite_close"] is None:
            return False, "missing one leg"
        b = exp - d["composite_close"]
        # A composite of BRTI constituents should track the index closely.
        return abs(b) < 100, f"BRTI={exp} composite={d['composite_close']} basis=${b:+.2f}"
    results.append(_check("measured basis vs BRTI", basis))

    def sheet():
        if not sheets.enabled():
            return True, "not configured (skipped)"
        n = sheets.append([], [])
        return True, "credentials load OK"
    results.append(_check("google sheets", sheet))

    ok = all(results)
    print(f"\n{'ALL CHECKS PASSED' if ok else 'FAILURES PRESENT — do not start the service'}\n")
    return ok
