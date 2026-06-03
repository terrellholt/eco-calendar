"""
fetch_expected_moves.py
=======================
Fetches the ATM straddle-based Expected Move (±%) for every optionable
event row in Supabase Master Calendar that has a ticker and a date.

For each event, the script finds the options expiration CLOSEST to that
event's specific date, so each row gets its own accurate expected move
rather than a shared nearest-expiry value.

Formula:
    Expected Move $ = (ATM Call Ask + ATM Put Ask) × 0.85
    Expected Move % = Expected Move $ / Current Price × 100

Schedule: Weekdays at 5 PM ET via GitHub Actions (post market close).
"""

import os
import json
import time
import logging
import urllib.request
import urllib.error
import urllib.parse
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
OUTPUT_SHEET_NAME = "📈 Expected Moves"
RATE_LIMIT_DELAY  = 2   # seconds between yfinance calls
CALENDAR_YEAR     = 2027

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pkzjgjtzljjjohiybnud.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


# ── Parse date string from Master Calendar ─────────────────────────────────
def parse_event_date(raw: str) -> date | None:
    """
    Handles:
      - 'Jan 4'  → date(2027, 1, 4)
      - '2027-01-01' or full ISO timestamp → parsed directly
    """
    if not raw:
        return None
    raw = raw.strip()
    # Full ISO date or timestamp
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
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


# ── Fetch event rows from Supabase ─────────────────────────────────────────
def fetch_calendar_events() -> list[dict]:
    """Pull all Master Calendar rows that have both a ticker and a date."""
    url = (
        f"{SUPABASE_URL}/rest/v1/Master%20Calendar"
        f"?select=id,date,ticker,event"
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

    # Filter out any rows without a usable date
    valid = []
    for r in rows:
        d = parse_event_date(r.get("date", ""))
        if d:
            r["_event_date"] = d
            valid.append(r)
        else:
            log.warning("Skipping row id=%s — unparseable date: %r", r.get("id"), r.get("date"))

    log.info("Fetched %d event rows with ticker + date from Supabase.", len(valid))
    return valid


# ── Find closest expiration to event date ─────────────────────────────────
def closest_expiry(expirations: list[str], event_date: date) -> str:
    """Return the expiration string whose date is closest to event_date."""
    def delta(exp_str):
        exp = datetime.strptime(exp_str, "%Y-%m-%d").date()
        return abs((exp - event_date).days)
    return min(expirations, key=delta)


# ── Calculate expected move for a specific expiration ─────────────────────
def get_expected_move(ticker: str, event_date: date) -> dict:
    base = {"ticker": ticker, "status": "error"}
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            hist  = stock.history(period="1d")
            price = float(hist["Close"].iloc[-1]) if not hist.empty else None
        if not price:
            return {**base, "error": "No price data"}

        expirations = stock.options
        if not expirations:
            return {**base, "error": "No options chain available"}

        expiry = closest_expiry(expirations, event_date)
        chain  = stock.option_chain(expiry)
        calls  = chain.calls
        puts   = chain.puts

        atm_strike = min(calls["strike"].tolist(), key=lambda x: abs(x - price))
        call_row   = calls[calls["strike"] == atm_strike]
        put_row    = puts[puts["strike"]  == atm_strike]

        if call_row.empty or put_row.empty:
            return {**base, "current_price": price, "nearest_expiry": expiry,
                    "error": f"ATM strike ${atm_strike} missing on one side"}

        call_ask = float(call_row["ask"].iloc[0])
        put_ask  = float(put_row["ask"].iloc[0])
        straddle = call_ask + put_ask
        em_usd   = round(straddle * 0.85, 4)
        em_pct   = round(em_usd / price * 100, 2)

        return {
            **base,
            "status":          "ok",
            "current_price":   round(price, 2),
            "atm_strike":      atm_strike,
            "atm_call_ask":    call_ask,
            "atm_put_ask":     put_ask,
            "straddle_price":  round(straddle, 4),
            "expected_move_usd": em_usd,
            "expected_move_pct": em_pct,
            "nearest_expiry":  expiry,
        }
    except Exception as exc:
        return {**base, "error": str(exc)}


# ── Push individual row back to Supabase ──────────────────────────────────
def patch_supabase_row(row_id: int, em_pct: float):
    url = f"{SUPABASE_URL}/rest/v1/Master%20Calendar?id=eq.{row_id}"
    payload = json.dumps({
        "price_move_val":  str(em_pct),
        "price_move_type": "%"
    }).encode("utf-8")
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
        ws = spreadsheet.add_worksheet(title=OUTPUT_SHEET_NAME, rows=500, cols=12)

    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    ok_count  = sum(1 for r in results if r["status"] == "ok")
    err_count = len(results) - ok_count

    header_rows = [
        [f"EXPECTED MOVES — per-event expiry matching  |  Last run: {now_str}"
         f"  |  ✓ {ok_count} succeeded   ⚠ {err_count} failed"],
        [],
        ["Row ID", "Event", "Ticker", "Event Date", "Matched Expiry",
         "Current Price", "ATM Strike", "Expected Move ±%", "Status"],
    ]

    data_rows = []
    for r in results:
        if r["status"] == "ok":
            data_rows.append([
                r["row_id"], r["event"], r["ticker"],
                str(r["event_date"]), r["nearest_expiry"],
                r["current_price"], r["atm_strike"],
                r["expected_move_pct"], "✓"
            ])
        else:
            data_rows.append([
                r["row_id"], r["event"], r["ticker"],
                str(r["event_date"]), "—", "—", "—", "—",
                f"⚠ {r.get('error','unknown')}"
            ])

    ws.clear()
    ws.update(range_name="A1", values=header_rows + data_rows)
    log.info("Google Sheet updated — %d rows written.", len(data_rows))


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    if not SUPABASE_KEY:
        raise EnvironmentError("SUPABASE_KEY env var not set.")

    log.info("Fetching event rows from Supabase...")
    events = fetch_calendar_events()
    if not events:
        log.warning("No eligible event rows found.")
        return

    # Deduplicate ticker fetches — cache options chains per ticker
    options_cache = {}
    results = []
    total   = len(events)

    for i, ev in enumerate(events, 1):
        ticker     = ev["ticker"]
        event_date = ev["_event_date"]
        row_id     = ev["id"]
        event_name = ev.get("event", "")

        log.info("[%d/%d]  %s  |  %s  |  %s", i, total, ticker, event_name, event_date)

        result = get_expected_move(ticker, event_date)
        result["row_id"]     = row_id
        result["event"]      = event_name
        result["event_date"] = event_date

        if result["status"] == "ok":
            log.info("  ✓  ±%.2f%%  (expiry: %s)", result["expected_move_pct"], result["nearest_expiry"])
            try:
                patched = patch_supabase_row(row_id, result["expected_move_pct"])
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
    log.info("Done.  %d succeeded  |  %d failed", ok_count, err_count)
    log.info("═" * 60)

    if ok_count == 0:
        raise RuntimeError("All rows failed — check yfinance / Yahoo Finance status.")

if __name__ == "__main__":
    main()
