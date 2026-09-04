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


def append(rows, cols):
    """Batch every bucket into one values.append call. Returns rows written."""
    if not enabled() or not rows:
        return 0
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(
            SHEETS_CREDENTIALS,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        sh = gspread.authorize(creds).open_by_key(SHEETS_SPREADSHEET_ID)
        try:
            ws = sh.worksheet(SHEETS_WORKSHEET)
        except Exception:
            ws = sh.add_worksheet(title=SHEETS_WORKSHEET, rows=1000, cols=len(cols))
            ws.append_row(cols, value_input_option="RAW")
        if not ws.get_values("A1:A1"):
            ws.append_row(cols, value_input_option="RAW")
        ws.append_rows([[("" if r.get(c) is None else r.get(c)) for c in cols] for r in rows],
                       value_input_option="RAW")
        return len(rows)
    except Exception as exc:
        # Deliberately non-fatal. The CSV already has the data.
        log.warning("sheets append failed (non-fatal, CSV already written): %s", exc)
        return 0
