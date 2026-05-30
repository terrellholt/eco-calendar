"""
fetch_expected_moves.py
=======================
Fetches the ATM straddle-based Expected Move (±%) for every optionable
equity in your calendar via yfinance, then writes results to a dedicated
"📈 Expected Moves" tab in your Google Sheet AND pushes the values
directly into Supabase Master Calendar (price_move + price_move_type columns).

Ticker source: "🗂️ Entity Library" tab — filtered to Public Company + ETF rows only.

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
from datetime import datetime

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
SCOPES             = ["https://www.googleapis.com/auth/spreadsheets",
                      "https://www.googleapis.com/auth/drive.readonly"]
ENTITY_SHEET_NAME  = "🗂️ Entity Library"
OUTPUT_SHEET_NAME  = "📈 Expected Moves"
ENTITY_HEADER_ROW  = 4
RATE_LIMIT_DELAY   = 2   # seconds between yfinance calls

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pkzjgjtzljjjohiybnud.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

ELIGIBLE_TYPES = {"Public Company", "ETF"}

# ── Google Sheets helpers ───────────────────────────────────────────────────
def get_sheets_client():
    raw = os.environ["GOOGLE_CREDENTIALS_JSON"]
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)

def get_spreadsheet(client):
    return client.open_by_key(os.environ["SPREADSHEET_ID"])

def extract_tickers_from_entity_library(spreadsheet):
    ws = spreadsheet.worksheet(ENTITY_SHEET_NAME)
    rows = ws.get_all_values()
    header = rows[ENTITY_HEADER_ROW - 1]
    try:
        ticker_col  = header.index("Ticker / Symbol")
        name_col    = header.index("Entity Name")
        type_col    = header.index("Entity Type")
        sector_col  = header.index("Sector / Category")
        country_col = header.index("Country / Region")
        exchange_col= header.index("Exchange / Venue")
    except ValueError as e:
        raise RuntimeError(f"Missing expected column in Entity Library: {e}")

    entries = []
    for row in rows[ENTITY_HEADER_ROW:]:
        if len(row) <= max(ticker_col, type_col):
            continue
        ticker      = row[ticker_col].strip()
        entity_type = row[type_col].strip()
        if not ticker or entity_type not in ELIGIBLE_TYPES:
            continue
        entries.append({
            "ticker":   ticker,
            "company":  row[name_col].strip()  if len(row) > name_col  else "",
            "type":     entity_type,
            "sector":   row[sector_col].strip()   if len(row) > sector_col   else "",
            "country":  row[country_col].strip()  if len(row) > country_col  else "",
            "exchange": row[exchange_col].strip()  if len(row) > exchange_col  else "",
        })
    return entries

# ── Expected move calculation ───────────────────────────────────────────────
def get_expected_move(ticker: str) -> dict:
    base = {"ticker": ticker, "status": "error"}
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        if not price:
            hist  = stock.history(period="1d")
            price = float(hist["Close"].iloc[-1]) if not hist.empty else None
        if not price:
            return {**base, "error": "No price data returned"}

        expirations = stock.options
        if not expirations:
            return {**base, "error": "No options chain available"}

        expiry = expirations[0]
        chain  = stock.option_chain(expiry)
        calls  = chain.calls
        puts   = chain.puts

        atm_strike = min(calls["strike"].tolist(), key=lambda x: abs(x - price))

        call_row = calls[calls["strike"] == atm_strike]
        put_row  = puts[puts["strike"]  == atm_strike]

        if call_row.empty or put_row.empty:
            return {**base,
                    "current_price": price,
                    "atm_strike":    atm_strike,
                    "nearest_expiry": expiry,
                    "error": f"ATM strike ${atm_strike} not found on both call and put sides"}

        call_ask = float(call_row["ask"].iloc[0])
        put_ask  = float(put_row["ask"].iloc[0])
        iv_call  = float(call_row["impliedVolatility"].iloc[0]) if "impliedVolatility" in call_row else None
        iv_put   = float(put_row["impliedVolatility"].iloc[0])  if "impliedVolatility" in put_row  else None
        avg_iv   = round((iv_call + iv_put) / 2 * 100, 2) if iv_call and iv_put else None

        straddle        = call_ask + put_ask
        em_usd          = round(straddle * 0.85, 4)
        em_pct          = round(em_usd / price, 6)
        em_pct_display  = round(em_pct * 100, 2)

        return {
            **base,
            "status":             "ok",
            "current_price":      round(price, 2),
            "atm_strike":         atm_strike,
            "atm_call_ask":       call_ask,
            "atm_put_ask":        put_ask,
            "straddle_price":     round(straddle, 4),
            "expected_move_usd":  em_usd,
            "expected_move_pct":  em_pct,
            "expected_move_pct_display": em_pct_display,
            "avg_iv_pct":         avg_iv,
            "nearest_expiry":     expiry,
        }
    except Exception as exc:
        return {**base, "error": str(exc)}

# ── Write results to Google Sheet ──────────────────────────────────────────
def get_or_create_output_sheet(spreadsheet):
    try:
        return spreadsheet.worksheet(OUTPUT_SHEET_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=OUTPUT_SHEET_NAME, rows=200, cols=20)

def write_results_to_sheet(ws, results, ticker_meta):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    ok_count  = sum(1 for r in results if r["status"] == "ok")
    err_count = len(results) - ok_count

    header_rows = [
        [f"EXPECTED MOVES — AUTO-CALCULATED VIA YFINANCE"
         f"          Source: Yahoo Finance options chains via yfinance"
         f"  |  Formula: (ATM Call Ask + ATM Put Ask) × 0.85"
         f"  |  Expiry: nearest available"
         f"  |  Last run: {now_str}"
         f"  |  ✓ {ok_count} succeeded   ⚠ {err_count} failed"],
        [],
        ["Ticker","Entity Name","Entity Type","Sector","Country","Exchange",
         "Current Price","ATM Strike","ATM Call Ask","ATM Put Ask",
         "Straddle Price","Expected Move $","Expected Move ±%",
         "Avg IV %","Nearest Expiry","Status"],
    ]

    data_rows = []
    for r in results:
        meta = ticker_meta.get(r["ticker"], {})
        if r["status"] == "ok":
            row = [
                r["ticker"],
                meta.get("company",""),
                meta.get("type",""),
                meta.get("sector",""),
                meta.get("country",""),
                meta.get("exchange",""),
                r["current_price"],
                r["atm_strike"],
                r["atm_call_ask"],
                r["atm_put_ask"],
                r["straddle_price"],
                r["expected_move_usd"],
                r["expected_move_pct"],
                r.get("avg_iv_pct",""),
                r["nearest_expiry"],
                "✓",
            ]
        else:
            row = [
                r["ticker"],
                meta.get("company",""),
                meta.get("type",""),
                meta.get("sector",""),
                meta.get("country",""),
                meta.get("exchange",""),
                r.get("current_price","—"),
                r.get("atm_strike","—"),
                "—","—","—","—","—","—",
                r.get("nearest_expiry","—"),
                f"⚠ {r.get('error','unknown error')}",
            ]
        data_rows.append(row)

    ws.clear()
    all_rows = header_rows + data_rows
    ws.update(range_name="A1", values=all_rows)
    log.info("Google Sheet updated — %d rows written.", len(data_rows))

# ── Push to Supabase ────────────────────────────────────────────────────────
def push_to_supabase(results):
    if not SUPABASE_KEY:
        log.warning("SUPABASE_KEY not set — skipping Supabase update.")
        return

    ok_results = [r for r in results if r["status"] == "ok"]
    log.info("Pushing %d tickers to Supabase...", len(ok_results))

    updated = 0
    skipped = 0

    for r in ok_results:
        ticker   = r["ticker"]
        em_value = str(r["expected_move_pct_display"])  # e.g. "1.03"

        # Build the PATCH request — update all rows where ticker matches
        url = (
            f"{SUPABASE_URL}/rest/v1/Master%20Calendar"
            f"?ticker=eq.{urllib.parse.quote(ticker)}"
        )
        payload = json.dumps({
            "price_move":      em_value,
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
        try:
            with urllib.request.urlopen(req) as resp:
                if resp.status in (200, 204):
                    log.info("  ✓  %s → ±%s%%", ticker, em_value)
                    updated += 1
                else:
                    log.warning("  ⚠  %s → HTTP %s", ticker, resp.status)
                    skipped += 1
        except urllib.error.HTTPError as e:
            log.warning("  ⚠  %s → HTTP error %s: %s", ticker, e.code, e.reason)
            skipped += 1
        except Exception as e:
            log.warning("  ⚠  %s → %s", ticker, e)
            skipped += 1

    log.info("Supabase update complete — %d updated, %d skipped.", updated, skipped)

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    import urllib.parse  # needed inside push_to_supabase

    log.info("Connecting to Google Sheets...")
    client      = get_sheets_client()
    spreadsheet = get_spreadsheet(client)
    log.info("Connected: %s", spreadsheet.title)

    ticker_entries = extract_tickers_from_entity_library(spreadsheet)
    if not ticker_entries:
        log.warning("No tickers found — nothing to do.")
        return

    ticker_meta = {e["ticker"]: e for e in ticker_entries}

    results = []
    total   = len(ticker_entries)

    for i, entry in enumerate(ticker_entries, 1):
        ticker = entry["ticker"]
        log.info("[%d/%d]  Fetching: %s (%s)", i, total, ticker, entry["company"])
        result = get_expected_move(ticker)
        if result["status"] == "ok":
            log.info(
                "  ✓  Price: $%.2f  |  ATM: $%.2f  |  EM: ±$%.2f (±%.2f%%)",
                result["current_price"],
                result["atm_strike"],
                result["expected_move_usd"],
                result["expected_move_pct_display"],
            )
        else:
            log.warning("  ⚠  %s: %s", ticker, result["error"])
        results.append(result)
        if i < total:
            time.sleep(RATE_LIMIT_DELAY)

    log.info("Writing results to Google Sheet...")
    output_ws = get_or_create_output_sheet(spreadsheet)
    write_results_to_sheet(output_ws, results, ticker_meta)

    log.info("Pushing results to Supabase...")
    push_to_supabase(results)

    ok_count  = sum(1 for r in results if r["status"] == "ok")
    err_count = len(results) - ok_count
    log.info("═" * 60)
    log.info("Done.  %d succeeded  |  %d failed", ok_count, err_count)
    log.info("═" * 60)

    if ok_count == 0:
        raise RuntimeError("All tickers failed — check yfinance / Yahoo Finance status.")

if __name__ == "__main__":
    import urllib.parse
    main()
