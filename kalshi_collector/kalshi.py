"""Kalshi public market data. No credentials, no order placement, ever."""
import logging
from datetime import datetime, timezone

from .config import KALSHI_BASE, KALSHI_BASE_FALLBACK, SERIES_TICKER, ET
from .http import get_json, FetchError

log = logging.getLogger(__name__)


class NoMarketFound(Exception):
    """Distinct from a network error. If the series is renamed this must not
    look like a timeout."""


def _paged_markets(base, **params):
    """Cursor pagination is mandatory: limit=200 silently truncates a 238-market
    ladder, corrupting n_buckets, both ladder sums, and possibly dropping the
    winning bucket."""
    out, cursor = [], None
    while True:
        p = dict(params, limit=1000)
        if cursor:
            p["cursor"] = cursor
        d = get_json(f"{base}/markets", params=p)
        out.extend(d.get("markets") or [])
        cursor = d.get("cursor") or ""
        if not cursor:
            return out


def fetch_markets(**params):
    try:
        return _paged_markets(KALSHI_BASE, **params)
    except FetchError:
        log.warning("primary host failed, trying fallback host")
        return _paged_markets(KALSHI_BASE_FALLBACK, **params)


def _f(v, default=None):
    """Prices arrive as decimal-dollar STRINGS ('0.0200'). They are already
    fractions -- do NOT divide by 100."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def event_ticker_for(close_utc):
    """Deterministic: KXBTC-YYMMMDDHH on the ET calendar. This is the backfill
    primitive -- it addresses any past hour directly, no history paging."""
    et = close_utc.astimezone(ET)
    return f"{SERIES_TICKER}-{et:%y%b%d%H}".upper().replace(SERIES_TICKER.upper() + "-", SERIES_TICKER + "-", 1)


def normalize(m):
    """One market -> flat dict. Absent fields are OMITTED, not null: cap_strike
    is missing on strike_type 'greater', floor_strike on 'less'."""
    floor = m.get("floor_strike")
    cap = m.get("cap_strike")
    width = round(cap - floor, 2) if (floor is not None and cap is not None) else None
    bid = _f(m.get("yes_bid_dollars"), 0.0)
    ask = _f(m.get("yes_ask_dollars"), 0.0)
    return {
        "market_ticker": m.get("ticker"),
        "event_ticker": m.get("event_ticker"),
        "strike_type": m.get("strike_type"),
        "floor_strike": floor,
        "cap_strike": cap,
        "bucket_width": width,
        "yes_bid": bid,
        "yes_ask": ask,
        "last_price": _f(m.get("last_price_dollars")),
        "volume": _f(m.get("volume_fp"), 0.0),
        "open_interest": _f(m.get("open_interest_fp"), 0.0),
        # A bucket nobody bids on cannot be transacted; it only carries the
        # $0.01 minimum ask. This flag is what keeps the Sheet and the
        # overround metric meaningful.
        "is_live": 1 if bid > 0 else 0,
        "close_time": m.get("close_time"),
        "status": m.get("status"),
        "result": m.get("result") or "",
        "expiration_value": _f(m.get("expiration_value")),
    }


def get_ladder(close_utc):
    """Full open ladder for the event closing at close_utc."""
    want = close_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets = fetch_markets(series_ticker=SERIES_TICKER, status="open")
    rows = [normalize(m) for m in markets]
    rows = [r for r in rows if r["close_time"] == want]
    if not rows:
        raise NoMarketFound(f"no open {SERIES_TICKER} markets closing {want}")
    return rows


def get_settled_event(event_ticker):
    """Settled ladder addressed directly by event ticker.

    Kalshi publishes expiration_value -- the exact BRTI settlement value, to the
    cent, on every market in the event. This is why no trade reconstruction is
    needed for the settlement leg.
    """
    markets = fetch_markets(event_ticker=event_ticker)
    if not markets:
        raise NoMarketFound(f"no markets for event {event_ticker}")
    rows = [normalize(m) for m in markets]
    settled = [r for r in rows if r["status"] in ("settled", "finalized")]
    if not settled:
        return None
    return rows


def ladder_stats(rows):
    """ask_sum scales with n_buckets because ~174 of 188 buckets carry the $0.01
    minimum ask, so it can never sit in 1.00-1.20. The bid side has no tick
    floor, giving a true no-arbitrage bracket: bid_sum <= 1.0 <= ask_sum."""
    live = [r for r in rows if r["is_live"]]
    return {
        "ladder_bid_sum": round(sum(r["yes_bid"] for r in rows), 4),
        "ladder_ask_sum": round(sum(r["yes_ask"] for r in rows), 4),
        "ladder_ask_sum_live": round(sum(r["yes_ask"] for r in live), 4),
        "n_buckets": len(rows),
        "n_buckets_live": len(live),
    }
