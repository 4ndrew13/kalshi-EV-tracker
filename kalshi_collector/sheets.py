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


def _worksheet():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        SHEETS_CREDENTIALS, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(SHEETS_SPREADSHEET_ID)
    return sh.worksheet(SHEETS_WORKSHEET)


def _col_letter(idx):
    """0-based column index -> A1 letter."""
    s, idx = "", idx + 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def update_settlements(capture_id, by_ticker, cols, fields):
    """Backfill the settlement columns of rows this capture already appended.

    The Sheet is a VIEW, not the system of record, so updating cells in place is
    safe here in a way it deliberately is not for the CSVs -- and without it the
    Calibration tab can never join result to the ask it was paid at.

    Non-fatal by design: settlements.csv already holds the truth.
    """
    if not enabled() or not by_ticker:
        return 0
    try:
        ws = _worksheet()
        first, last = cols.index(fields[0]), cols.index(fields[-1])
        rng = lambda r: f"{_col_letter(first)}{r}:{_col_letter(last)}{r}"
        ids = ws.col_values(cols.index("capture_id") + 1)
        tks = ws.col_values(cols.index("market_ticker") + 1)
        updates = []
        for i, cid in enumerate(ids, start=1):
            if cid != capture_id:
                continue
            row = by_ticker.get(tks[i - 1] if i - 1 < len(tks) else "")
            if row:
                updates.append({"range": rng(i), "values": [[row.get(f, "") for f in fields]]})
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        return len(updates)
    except Exception as exc:
        log.warning("sheets settlement update failed (non-fatal, CSV has it): %s", exc)
        return 0


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


def existing_capture_ids():
    """capture_ids already present in the Sheet, for idempotent re-sync."""
    if not enabled():
        return []
    try:
        return [v for v in _worksheet().col_values(1) if v.startswith("20")]
    except Exception as exc:
        log.warning("sheets read failed: %s", exc)
        return []
