"""Three-venue BTC composite.

Two jobs:

1. ``live_spot()`` -- ref_spot at snapshot time. This is now the ONLY
   reconstructed quantity in the dataset and therefore the sole source of error
   in ``move``; Kalshi supplies the settlement leg exactly.
2. ``reconstruct_close()`` -- the composite over the final 60 seconds, rebuilt
   from public trade history. Purely a BASIS DIAGNOSTIC. Kalshi's
   expiration_value is authoritative for settlement, so this exists to measure
   ``basis = expiration_value - composite_close`` every hour, which turns
   reconstruction_ok into a real check rather than a self-consistency test.

A single venue carries a persistent basis against BRTI, not zero-mean noise, so
a persistent basis would shift every ``move`` in the same direction. Averaging
venues largely cancels it.
"""
import logging
import re
import statistics
from datetime import datetime, timezone

from .config import BITSTAMP_MAX_LOOKBACK_S
from .http import get_json

log = logging.getLogger(__name__)

CB_TICKER = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
CB_TRADES = "https://api.exchange.coinbase.com/products/BTC-USD/trades"
KR_TICKER = "https://api.kraken.com/0/public/Ticker"
KR_TRADES = "https://api.kraken.com/0/public/Trades"
BS_TICKER = "https://www.bitstamp.net/api/v2/ticker/btcusd/"
BS_TRADES = "https://www.bitstamp.net/api/v2/transactions/btcusd/"


_ISO = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?")


def _iso_to_epoch(s):
    """Coinbase timestamps are UTC with variable-length fractional seconds
    ('...T23:43:45.935000Z'). Python 3.8's fromisoformat rejects several of the
    shapes seen in the wild, so parse explicitly rather than patching strings."""
    m = _ISO.match(s.strip())
    if not m:
        raise ValueError(f"unparseable timestamp: {s!r}")
    y, mo, d, h, mi, sec = (int(m.group(i)) for i in range(1, 7))
    micro = int(((m.group(7) or "") + "000000")[:6])
    return datetime(y, mo, d, h, mi, sec, micro, tzinfo=timezone.utc).timestamp()


# --------------------------------------------------------------------------
# Live spot (mid of best bid/ask, which is quieter than last-trade)
# --------------------------------------------------------------------------
def _mid(bid, ask, last):
    try:
        b, a = float(bid), float(ask)
        if b > 0 and a > 0 and a >= b:
            return (a + b) / 2.0
    except (TypeError, ValueError):
        pass
    try:
        return float(last)
    except (TypeError, ValueError):
        return None


def _live_coinbase():
    d = get_json(CB_TICKER, retries=3)
    return _mid(d.get("bid"), d.get("ask"), d.get("price"))


def _live_kraken():
    d = get_json(KR_TICKER, params={"pair": "XBTUSD"}, retries=3)
    res = (d.get("result") or {})
    if not res:
        return None
    k = next(iter(res))
    t = res[k]
    return _mid(t["b"][0], t["a"][0], t["c"][0])


def _live_bitstamp():
    d = get_json(BS_TICKER, retries=3)
    return _mid(d.get("bid"), d.get("ask"), d.get("last"))


def live_spot():
    """Equal-weighted mid across whichever venues respond.

    Returns (composite, ref_source, per_venue). ref_source records the venues
    that actually contributed, so an hour with a degraded venue set can be
    identified -- and excluded or corrected -- in analysis.
    """
    fns = {"coinbase": _live_coinbase, "kraken": _live_kraken, "bitstamp": _live_bitstamp}
    per = {}
    for name, fn in fns.items():
        try:
            v = fn()
            if v and v > 0:
                per[name] = round(v, 2)
        except Exception as exc:
            log.warning("live spot %s failed: %s", name, exc)
    if not per:
        return None, "none", {}
    composite = round(sum(per.values()) / len(per), 4)
    return composite, "+".join(sorted(per)), per


# --------------------------------------------------------------------------
# Historical trades for the final-60s composite (basis diagnostic)
# --------------------------------------------------------------------------
def _trades_coinbase(start_ts, end_ts, max_pages=40):
    """Coinbase pages newest-first; walk back until we cover the window."""
    out, after = [], None
    for _ in range(max_pages):
        params = {"limit": 1000}
        if after:
            params["after"] = after
        page = get_json(CB_TRADES, params=params, retries=3)
        if not page:
            break
        oldest = None
        for t in page:
            ts = _iso_to_epoch(t["time"])
            oldest = ts if oldest is None else min(oldest, ts)
            if start_ts <= ts <= end_ts:
                out.append((ts, float(t["price"])))
        ids = [t.get("trade_id") for t in page if t.get("trade_id") is not None]
        if not ids:
            break
        after = min(ids)
        if oldest is not None and oldest < start_ts:
            break
    return out


def _trades_kraken(start_ts, end_ts):
    """Kraken's `since` seeks forward from a timestamp -- one call lands on the
    window directly."""
    d = get_json(KR_TRADES, params={"pair": "XBTUSD", "since": int(start_ts * 1_000_000_000)},
                 retries=3)
    res = d.get("result") or {}
    key = next((k for k in res if k != "last"), None)
    if not key:
        return []
    return [(float(r[2]), float(r[0])) for r in res[key] if start_ts <= float(r[2]) <= end_ts]


def _trades_bitstamp(start_ts, end_ts):
    """Bitstamp exposes only the trailing hour with no windowing, so this
    degrades to nothing if the sweep runs late. Recorded, not fatal."""
    d = get_json(BS_TRADES, params={"time": "hour"}, retries=3)
    return [(float(t["date"]), float(t["price"])) for t in d
            if start_ts <= float(t["date"]) <= end_ts]


def _grid(trades, start_ts, seconds=60):
    """Last trade at or before each second, forward-filled. Mirrors the index's
    own once-per-second sampling more closely than a trade-weighted mean."""
    if not trades:
        return {}
    trades = sorted(trades)
    grid, i, last = {}, 0, None
    for s in range(seconds):
        t = start_ts + s
        while i < len(trades) and trades[i][0] <= t:
            last = trades[i][1]
            i += 1
        if last is not None:
            grid[s] = last
    return grid


def reconstruct_close(close_utc):
    """Composite over [close-60s, close). Returns a dict of diagnostic fields."""
    end_ts = close_utc.timestamp()
    start_ts = end_ts - 60
    age = datetime.now(timezone.utc).timestamp() - end_ts

    fetchers = {"coinbase": _trades_coinbase, "kraken": _trades_kraken,
                "bitstamp": _trades_bitstamp}
    grids = {}
    for name, fn in fetchers.items():
        if name == "bitstamp" and age is not None and age > BITSTAMP_MAX_LOOKBACK_S:
            log.info("skipping bitstamp: window is %.0fs old, beyond its 1h retention", age)
            continue
        try:
            tr = fn(start_ts, end_ts) if name != "coinbase" else fn(start_ts, end_ts)
            g = _grid(tr, start_ts)
            if g:
                grids[name] = g
        except Exception as exc:
            log.warning("trade fetch %s failed: %s", name, exc)

    if not grids:
        return {"composite_close": None, "basis_venues": "none",
                "n_samples": 0, "sample_span_seconds": None, "sample_stdev": None}

    points = []
    for s in range(60):
        vals = [g[s] for g in grids.values() if s in g]
        if vals:
            points.append((s, sum(vals) / len(vals)))
    if not points:
        return {"composite_close": None, "basis_venues": "+".join(sorted(grids)),
                "n_samples": 0, "sample_span_seconds": None, "sample_stdev": None}

    prices = [p for _, p in points]
    return {
        "composite_close": round(sum(prices) / len(prices), 4),
        "basis_venues": "+".join(sorted(grids)),
        "n_samples": len(points),
        "sample_span_seconds": points[-1][0] - points[0][0] + 1,
        "sample_stdev": round(statistics.pstdev(prices), 4) if len(prices) > 1 else 0.0,
    }
