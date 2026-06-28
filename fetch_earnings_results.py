"""
fetch_earnings_results.py
=========================
Backward-looking sibling of fetch_expected_moves.py.

For every PAST earnings event in Supabase Master Calendar that has a ticker
and does not already have an actual result, this script pulls the company's
REPORTED EPS for that quarter from yfinance and writes it into the
`actual_value` column (e.g. "$4.91 EPS").

Design rules (kept deliberately isolated from the forward-looking pipeline):
  • Only earnings-category rows are processed.
  • Only events whose date is strictly in the past are processed.
  • Rows that already have an actual_value are skipped (freeze rule —
    a result, once set, is never overwritten).
  • Matching is by date: the reported earnings within 7 days of the event
    date is used, so a slightly-off scheduled date still matches the print.

Schedule: Weekdays after market close via GitHub Actions, as its own job
(NOT bolted onto fetch_expected_moves.py).
"""

import os
import json
import time
import math
import logging
import urllib.request
import urllib.error
import urllib.parse
import re
from datetime import datetime, date

import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────
SCOPES            = ["https://www.googleapis.com/auth/spreadsheets",
                     "https://www.googleapis.com/auth/drive.readonly"]
OUTPUT_SHEET_NAME = "📊 Earnings Results"
RATE_LIMIT_DELAY  = 2   # seconds between yfinance calls
CALENDAR_YEAR     = 2027
MATCH_WINDOW_DAYS = 7   # how close a reported earnings date must be to the event

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pkzjgjtzljjjohiybnud.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


# ── Parse date string from Master Calendar ─────────────────────────────────
def parse_event_date(raw: str) -> date | None:
    """
    Handles:
      - 'Jan 4'  → date(CALENDAR_YEAR, 1, 4)
      - '2027-01-01' or full ISO timestamp → parsed directly
      - 'Jan 28, 2027' → parsed with explicit year
    """
    if not raw:
        return None
    raw = raw.strip()
    # Strip trailing " (est.)" or " (est)" suffix
    raw = re.sub(r'\s*\(est\.?\)\s*$', '', raw, flags=re.IGNORECASE).strip()
    # Full ISO date or timestamp
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    # Full text date with year e.g. "Jan 28, 2027"
    try:
        return datetime.strptime(raw, "%b %d, %Y").date()
    except ValueError:
        pass
    # Abbreviated "Mon DD" format — assume CALENDAR_YEAR
    for fmt in ("%b %d", "%b  %d"):
        try:
            d = datetime.strptime(raw, fmt)
            return date(CALENDAR_YEAR, d.month, d.day)
        except ValueError:
            pass
    log.warning("Could not parse date: %r", raw)
    return None


# ── Fetch past earnings rows from Supabase ─────────────────────────────────
def fetch_earnings_events() -> list[dict]:
    """
    Pull Master Calendar rows that are earnings events with a ticker, are in
    the past, and do not already have an actual_value.
    """
    url = (
        f"{SUPABASE_URL}/rest/v1/Master%20Calendar"
        f"?select=id,date,ticker,event,category,actual_value"
        f"&ticker=not.is.null"
        f"&ticker=neq."
        f"&limit=2000"
    )
    req = urllib.request.Request(
        url,
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept":        "application/json",
        }
    )
    with urllib.request.urlopen(req) as resp:
        rows = json.loads(resp.read())

    today = date.today()
    valid = []
    for r in rows:
        category = (r.get("category") or "").lower()
        if "earnings" not in category:
            continue
        if (r.get("actual_value") or "").strip():
            continue  # freeze rule — already has a result
        d = parse_event_date(r.get("date", ""))
        if not d:
            log.warning("Skipping row id=%s — unparseable date: %r", r.get("id"), r.get("date"))
            continue
        if d >= today:
            continue  # not in the past yet
        r["_event_date"] = d
        valid.append(r)

    log.info("Fetched %d past earnings rows needing a result from Supabase.", len(valid))
    return valid


# ── Pull reported EPS for a specific event date ────────────────────────────
def get_earnings_dates_df(stock):
    """yfinance has shifted APIs across versions; try both."""
    try:
        df = stock.get_earnings_dates(limit=24)
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    try:
        return getattr(stock, "earnings_dates", None)
    except Exception:
        return None


def get_earnings_actual(ticker: str, event_date: date) -> dict:
    base = {"ticker": ticker, "status": "error"}
    try:
        stock = yf.Ticker(ticker)
        df = get_earnings_dates_df(stock)
        if df is None or df.empty:
            return {**base, "error": "No earnings dates available"}
        if "Reported EPS" not in df.columns:
            return {**base, "error": "No Reported EPS column"}

        best = None          # (reported_date, eps)
        best_gap = None
        for idx, row in df.iterrows():
            try:
                reported_date = idx.date()
            except Exception:
                continue
            reported = row.get("Reported EPS")
            if reported is None or (isinstance(reported, float) and math.isnan(reported)):
                continue
            gap = abs((reported_date - event_date).days)
            if gap <= MATCH_WINDOW_DAYS and (best_gap is None or gap < best_gap):
                best = (reported_date, float(reported))
                best_gap = gap

        if best is None:
            return {**base, "error": f"No reported EPS within {MATCH_WINDOW_DAYS} days of event date"}

        reported_date, reported = best
        if reported < 0:
            value_str = f"-${abs(reported):.2f} EPS"
        else:
            value_str = f"${reported:.2f} EPS"

        return {
            **base,
            "status":        "ok",
            "reported_eps":  reported,
            "matched_date":  str(reported_date),
            "actual_value":  value_str,
        }
    except Exception as exc:
        return {**base, "error": str(exc)}


# ── Push result back to Supabase ──────────────────────────────────────────
def patch_actual_value(row_id: int, value_str: str) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/Master%20Calendar?id=eq.{row_id}"
    payload = json.dumps({"actual_value": value_str}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="PATCH",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        }
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status in (200, 204)


# ── Write summary to Google Sheet ─────────────────────────────────────────
def get_sheets_client():
    raw   = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return gspread.authorize(creds)


def write_summary_to_sheet(spreadsheet, results):
    try:
        ws = spreadsheet.worksheet(OUTPUT_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=OUTPUT_SHEET_NAME, rows=500, cols=10)

    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    ok_count  = sum(1 for r in results if r["status"] == "ok")
    err_count = len(results) - ok_count

    header_rows = [
        [f"EARNINGS RESULTS — reported EPS backfill  |  Last run: {now_str}"
         f"  |  ✓ {ok_count} written   ⚠ {err_count} failed"],
        [],
        ["Row ID", "Event", "Ticker", "Event Date",
         "Matched Earnings Date", "Reported EPS", "Written Value", "Status"],
    ]

    data_rows = []
    for r in results:
        if r["status"] == "ok":
            data_rows.append([
                r["row_id"], r["event"], r["ticker"],
                str(r["event_date"]), r["matched_date"],
                r["reported_eps"], r["actual_value"], "✓"
            ])
        else:
            data_rows.append([
                r["row_id"], r["event"], r["ticker"],
                str(r["event_date"]), "—", "—", "—",
                f"⚠ {r.get('error','unknown')}"
            ])

    ws.clear()
    ws.update(range_name="A1", values=header_rows + data_rows)
    log.info("Google Sheet updated — %d rows written.", len(data_rows))


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    if not SUPABASE_KEY:
        raise EnvironmentError("SUPABASE_KEY env var not set.")

    log.info("Fetching past earnings rows from Supabase...")
    events = fetch_earnings_events()
    if not events:
        log.warning("No eligible past earnings rows found — nothing to backfill.")
        return

    results = []
    total   = len(events)

    for i, ev in enumerate(events, 1):
        ticker     = ev["ticker"]
        event_date = ev["_event_date"]
        row_id     = ev["id"]
        event_name = ev.get("event", "")

        log.info("[%d/%d]  %s  |  %s  |  %s", i, total, ticker, event_name, event_date)

        result = get_earnings_actual(ticker, event_date)
        result["row_id"]     = row_id
        result["event"]      = event_name
        result["event_date"] = event_date

        if result["status"] == "ok":
            log.info("  ✓  %s  (reported %s)", result["actual_value"], result["matched_date"])
            try:
                patched = patch_actual_value(row_id, result["actual_value"])
                if not patched:
                    log.warning("  ⚠  Supabase patch returned unexpected status for id=%s", row_id)
            except Exception as e:
                log.warning("  ⚠  Supabase patch failed for id=%s: %s", row_id, e)
        else:
            log.warning("  ⚠  %s", result["error"])

        results.append(result)

        if i < total:
            time.sleep(RATE_LIMIT_DELAY)

    log.info("Writing summary to Google Sheet...")
    client      = get_sheets_client()
    spreadsheet = client.open_by_key(os.environ["SPREADSHEET_ID"])
    write_summary_to_sheet(spreadsheet, results)

    ok_count  = sum(1 for r in results if r["status"] == "ok")
    err_count = len(results) - ok_count
    log.info("═" * 60)
    log.info("Done.  %d written  |  %d failed", ok_count, err_count)
    log.info("═" * 60)

    if ok_count == 0 and total > 0:
        raise RuntimeError("All rows failed — check yfinance / Yahoo Finance status.")


if __name__ == "__main__":
    main()
