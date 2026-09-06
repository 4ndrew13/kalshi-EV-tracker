"""Google Sheets append -- live view and offsite copy.

LIVE BUCKETS ONLY. The full 188-bucket ladder would write 4,512 rows/day
(~126k cells) against Sheets' hard 10,000,000-cell ceiling, exhausting it in
about 79 days. Live buckets are ~14/hour, which stays under the cap for years --
and they are the only buckets that could ever be transacted anyway.

Never on the critical path: every failure here is swallowed after the CSV write
has already succeeded.
"""
import logging

from .config import SHEETS_CREDENTIALS, SHEETS_SPREADSHEET_ID, SHEETS_WORKSHEET

log = logging.getLogger(__name__)


def enabled():
    return bool(SHEETS_SPREADSHEET_ID)


_WS = None


def _worksheet(refresh=False):
    """Cached. Each open_by_key + worksheet() pair costs two reads against a
    60-reads-per-minute quota, so re-opening per call is what exhausted it."""
    global _WS
    if _WS is not None and not refresh:
        return _WS
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        SHEETS_CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(SHEETS_SPREADSHEET_ID)
    _WS = sh.worksheet(SHEETS_WORKSHEET)
    return _WS


def _col_letter(idx):
    """0-based column index -> A1 letter."""
    s, idx = "", idx + 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def update_settlements_bulk(by_capture, cols, fields):
    """Fill settlement columns for many captures in ONE read and ONE write.

    The Sheet is a view and settlements.csv is the system of record, so editing
    cells in place is safe here in a way it is not for the CSVs. Doing it per
    capture cost four reads each and blew the 60/min read quota; this reads the
    two key columns once and issues a single batch_update.
    """
    if not enabled() or not by_capture:
        return 0
    try:
        ws = _worksheet()
        ids = ws.col_values(cols.index("capture_id") + 1)
        tks = ws.col_values(cols.index("market_ticker") + 1)
        first, last = cols.index(fields[0]), cols.index(fields[-1])
        updates = []
        for i, cid in enumerate(ids, start=1):
            by_ticker = by_capture.get(cid)
            if not by_ticker:
                continue
            row = by_ticker.get(tks[i - 1] if i - 1 < len(tks) else "")
            if row:
                updates.append({
                    "range": f"{_col_letter(first)}{i}:{_col_letter(last)}{i}",
                    "values": [[row.get(f, "") for f in fields]],
                })
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        log.info("sheets: settlement-filled %d rows", len(updates))
        return len(updates)
    except Exception as exc:
        log.warning("sheets settlement update failed (non-fatal, CSV has it): %s: %s",
                    type(exc).__name__, exc)
        return 0


def update_settlements(capture_id, by_ticker, cols, fields):
    """Single-capture wrapper used by the hourly backfill sweep."""
    return update_settlements_bulk({capture_id: by_ticker}, cols, fields)


def append(rows, cols):
    """Write rows at an explicitly computed range.

    Deliberately NOT gspread's append_rows: that delegates to Google's
    "find the end of the table" heuristic, which mis-detects on a sheet with
    title rows above the header and silently writes nowhere -- returning success
    while dropping the data. Computing the target row from column A is
    deterministic and verifiable.
    """
    if not enabled() or not rows:
        return 0
    try:
        ws = _worksheet()
        first = len(ws.col_values(1)) + 1
        last = first + len(rows) - 1
        if last > ws.row_count:
            ws.add_rows(last - ws.row_count + 500)
        rng = f"A{first}:{_col_letter(len(cols) - 1)}{last}"
        vals = [[("" if r.get(c) is None else r.get(c)) for c in cols] for r in rows]
        ws.batch_update([{"range": rng, "values": vals}], value_input_option="RAW")
        # Log success too: silence must not be ambiguous between "worked" and
        # "silently did nothing".
        log.info("sheets: wrote %d rows to %s", len(rows), rng)
        return len(rows)
    except Exception as exc:
        # Non-fatal: the CSV write already succeeded.
        log.warning("sheets append FAILED (non-fatal, CSV has it): %s: %s",
                    type(exc).__name__, exc)
        return 0


def existing_rows(cols):
    """(capture_id, market_ticker) pairs already in the Sheet.

    Keyed on the pair, not capture_id alone: a partially-written capture would
    otherwise look present and never be repaired.
    """
    if not enabled():
        return set()
    try:
        ws = _worksheet()
        ids = ws.col_values(cols.index("capture_id") + 1)
        tks = ws.col_values(cols.index("market_ticker") + 1)
        return {(c, tks[i] if i < len(tks) else "")
                for i, c in enumerate(ids) if c.startswith("20")}
    except Exception as exc:
        log.warning("sheets read failed: %s", exc)
        return set()
