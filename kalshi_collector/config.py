"""Central configuration. Verified against the live API 2026-09-04 (see STEP0-FINDINGS.md)."""
import os
from pathlib import Path
try:
    from zoneinfo import ZoneInfo          # Python 3.9+
except ImportError:                        # Ubuntu 20.04 ships Python 3.8
    from backports.zoneinfo import ZoneInfo

# --- Kalshi -----------------------------------------------------------------
# Both hosts return byte-identical results; the second is a failover.
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_BASE_FALLBACK = "https://external-api.kalshi.com/trade-api/v2"

# VERIFIED: "Bitcoin range", hourly, settles on CF Benchmarks BRTI.
# DO NOT change to KXBTCD ("Bitcoin price Above/below") -- also hourly, also BTC,
# but a different product. Using it yields a valid-looking dataset answering the
# wrong question. See STEP0-FINDINGS.md section 1.
SERIES_TICKER = "KXBTC"

# --- Timing (UTC wall-clock marks) ------------------------------------------
SNAPSHOT_MINUTE = 56    # perishable: the ladder exists for 4 minutes then is gone
BACKFILL_MINUTE = 20    # durable: best effort, retries implicitly next hour

ET = ZoneInfo("America/New_York")   # never a hardcoded UTC-5

# --- Paths ------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("KALSHI_DATA_DIR", "data"))
SNAPSHOTS_CSV = DATA_DIR / "snapshots.csv"
SETTLEMENTS_CSV = DATA_DIR / "settlements.csv"
RUNS_LOG = DATA_DIR / "runs.log"

# --- Reference price composite ----------------------------------------------
# Three BRTI constituent venues. A single venue carries a persistent basis
# against the index, not zero-mean noise.
VENUES = ("coinbase", "kraken", "bitstamp")

# Bitstamp's public transactions endpoint only exposes the trailing hour with no
# windowing, so basis reconstruction degrades gracefully past this age.
BITSTAMP_MAX_LOOKBACK_S = 55 * 60

# --- Optional integrations --------------------------------------------------
SHEETS_CREDENTIALS = os.environ.get("KALSHI_SHEETS_CREDENTIALS", "service-account.json")
SHEETS_SPREADSHEET_ID = os.environ.get("KALSHI_SHEETS_ID", "")
SHEETS_WORKSHEET = os.environ.get("KALSHI_SHEETS_TAB", "Data log")
HEALTHCHECK_URL = os.environ.get("KALSHI_HEALTHCHECK_URL", "")

HTTP_TIMEOUT = 20
MAX_RETRIES = 5
