"""
fetch_expected_moves.py
=======================
Fetches the ATM straddle-based Expected Move (±%) for every optionable
equity in your calendar via yfinance, then writes results to a dedicated
"📈 Expected Moves" tab in your Google Sheet.

Ticker source:
    "🗂️ Entity Library" tab — filtered to Public Company + ETF rows only.
    This is the single source of truth. Every other sheet (Earnings,
    Corporate Events, Sector Conferences, etc.) links back to entities
    already in this library, so pulling from here covers all event types:
    earnings, product launches, developer conferences, sector events, etc.

Formula:
    Expected Move $ = (ATM Call Ask + ATM Put Ask) × 0.85
    Expected Move % = Expected Move $ / Current Price × 100

Options contract used:
    Nearest available expiry only (expirations[0] from yfinance).
    This is the most liquid contract and the strongest signal for
    near-term event-driven price moves.

Data source: yfinance — Yahoo Finance options chains (free, unofficial).
Schedule: Weekdays at 5 PM ET via GitHub Actions (post market close).
"""

import os
import json
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

OUTPUT_SHEET_NAME       = "📈 Expected Moves"
ENTITY_SHEET_NAME       = "🗂️ Entity Library"

# Only these entity types have optionable equities worth fetching
OPTIONABLE_ENTITY_TYPES = {"Public Company", "ETF"}

# ATM straddle multiplier (industry standard: straddle × 0.85 ≈ 1σ move)
ATM_MULTIPLIER = 0.85

# Polite delay between yfinance requests (seconds)
RATE_LIMIT_DELAY = 1.2

# Retry attempts per ticker before marking as failed
MAX_RETRIES = 2


# ── Google Sheets auth ────────────────────────────────────────────────────────

def get_sheets_client() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise EnvironmentError(
            "GOOGLE_CREDENTIALS_JSON env var not set. See README.md."
        )
    creds = Credentials.from_service_account_info(
        json.loads(creds_json), scopes=SCOPES
    )
    return gspread.authorize(creds)


def get_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    sheet_id = os.environ.get("SPREADSHEET_ID")
    if not sheet_id:
        raise EnvironmentError(
            "SPREADSHEET_ID env var not set. See README.md."
        )
    return client.open_by_key(sheet_id)


def get_or_create_output_sheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(OUTPUT_SHEET_NAME)
    except gspread.WorksheetNotFound:
        log.info("Creating output tab: %s", OUTPUT_SHEET_NAME)
        return spreadsheet.add_worksheet(title=OUTPUT_SHEET_NAME, rows=600, cols=14)


# ── Ticker extraction from Entity Library ─────────────────────────────────────

def extract_equity_tickers(spreadsheet: gspread.Spreadsheet) -> list[dict]:
    """
    Read the Entity Library and return all optionable equity rows.

    Column layout (0-indexed):
        0  Entity Name
        1  Ticker / Symbol
        2  Entity Type
        3  Sector / Category
        4  Sub-Sector
        5  Country / Region
        6  Exchange / Venue

    Returns list of dicts: {ticker, name, entity_type, sector, country, exchange}
    Only includes rows where Entity Type is in OPTIONABLE_ENTITY_TYPES and
    the ticker is a real symbol (not "N/A", blank, or a footer note).
    """
    ws = spreadsheet.worksheet(ENTITY_SHEET_NAME)
    rows = ws.get_all_values()

    results = []
    seen    = set()
    header_found = False

    for row in rows:
        # Find the real header row (contains "Entity Name")
        if not header_found:
            if row and row[0].strip() == "Entity Name":
                header_found = True
            continue

        if len(row) < 3:
            continue

        name        = row[0].strip()
        ticker      = row[1].strip().upper()
        entity_type = row[2].strip()
        sector      = row[3].strip() if len(row) > 3 else ""
        country     = row[5].strip() if len(row) > 5 else ""
        exchange    = row[6].strip() if len(row) > 6 else ""

        # Skip non-equity types (Central Bank, Gov't Agency, Commodity, etc.)
        if entity_type not in OPTIONABLE_ENTITY_TYPES:
            continue

        # Skip placeholder / invalid tickers
        if (not ticker
                or ticker in ("N/A", "TICKER", "—")
                or ticker.startswith("[")
                or len(ticker) > 10):   # sanity guard against footer text
            continue

        if ticker not in seen:
            seen.add(ticker)
            results.append({
                "ticker":      ticker,
                "name":        name,
                "entity_type": entity_type,
                "sector":      sector,
                "country":     country,
                "exchange":    exchange,
            })

    log.info(
        "Entity Library: %d optionable tickers found (%s)",
        len(results),
        ", ".join(r["ticker"] for r in results),
    )
    return results


# ── Core expected move calculation ────────────────────────────────────────────

def get_expected_move(ticker_str: str) -> dict:
    """
    Use yfinance to fetch the nearest-expiry ATM straddle for a ticker
    and calculate the expected move.

    Only the nearest expiry (expirations[0]) is used — this is the
    most liquid contract and best reflects near-term event pricing.

    Returns a result dict. On failure, status='error' with a reason string.
    """
    result = {
        "ticker":            ticker_str,
        "current_price":     None,
        "atm_strike":        None,
        "atm_call_ask":      None,
        "atm_put_ask":       None,
        "straddle_price":    None,
        "expected_move_usd": None,
        "expected_move_pct": None,
        "nearest_expiry":    None,
        "avg_iv_pct":        None,
        "status":            "ok",
        "error":             "",
    }

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            stock = yf.Ticker(ticker_str)

            # ── 1. Get current price ───────────────────────────────────────
            current_price = getattr(stock.fast_info, "last_price", None)
            if not current_price:
                hist = stock.history(period="2d")
                if hist.empty:
                    result.update(status="error", error="No price data from yfinance")
                    return result
                current_price = float(hist["Close"].iloc[-1])

            result["current_price"] = round(float(current_price), 4)

            # ── 2. Get options expirations — nearest only ──────────────────
            expirations = stock.options
            if not expirations:
                result.update(
                    status="error",
                    error="No options chain (non-optionable, delisted, or international)"
                )
                return result

            nearest_expiry = expirations[0]   # ← nearest expiry, always
            result["nearest_expiry"] = nearest_expiry

            # ── 3. Pull the chain for that expiry ─────────────────────────
            chain = stock.option_chain(nearest_expiry)
            calls = chain.calls
            puts  = chain.puts

            if calls.empty or puts.empty:
                result.update(
                    status="error",
                    error=f"Empty options chain for nearest expiry {nearest_expiry}"
                )
                return result

            # ── 4. Find ATM strike ────────────────────────────────────────
            strikes      = calls["strike"].values
            atm_strike   = float(strikes[abs(strikes - current_price).argmin()])
            result["atm_strike"] = atm_strike

            call_row = calls[calls["strike"] == atm_strike]
            put_row  = puts[puts["strike"]  == atm_strike]

            if call_row.empty or put_row.empty:
                result.update(
                    status="error",
                    error=f"ATM strike ${atm_strike} not found on both call and put sides"
                )
                return result

            # ── 5. Get ask prices; fall back to mid if ask is zero ────────
            call_ask = float(call_row["ask"].iloc[0])
            put_ask  = float(put_row["ask"].iloc[0])

            if call_ask == 0:
                call_ask = (float(call_row["bid"].iloc[0]) + float(call_row["ask"].iloc[0])) / 2
            if put_ask == 0:
                put_ask  = (float(put_row["bid"].iloc[0]) + float(put_row["ask"].iloc[0])) / 2

            result["atm_call_ask"] = round(call_ask, 4)
            result["atm_put_ask"]  = round(put_ask,  4)

            # ── 6. Average IV (informational, not used in EM calc) ────────
            try:
                call_iv = float(call_row["impliedVolatility"].iloc[0])
                put_iv  = float(put_row["impliedVolatility"].iloc[0])
                result["avg_iv_pct"] = round((call_iv + put_iv) / 2 * 100, 2)
            except Exception:
                pass  # IV is bonus data — don't fail the whole calc over it

            # ── 7. Expected Move ──────────────────────────────────────────
            straddle          = call_ask + put_ask
            em_usd            = straddle * ATM_MULTIPLIER
            em_pct            = (em_usd / current_price) * 100

            result["straddle_price"]    = round(straddle, 4)
            result["expected_move_usd"] = round(em_usd,   4)
            result["expected_move_pct"] = round(em_pct,   4)

            return result   # ← success path

        except Exception as exc:
            if attempt <= MAX_RETRIES:
                wait = attempt * 2
                log.warning(
                    "%s — attempt %d/%d failed (%s). Retrying in %ds...",
                    ticker_str, attempt, MAX_RETRIES + 1, exc, wait
                )
                time.sleep(wait)
            else:
                result.update(status="error", error=str(exc))
                return result

    return result


# ── Sheet writer ──────────────────────────────────────────────────────────────

def write_results(
    ws: gspread.Worksheet,
    results: list[dict],
    meta: dict[str, dict],
) -> None:
    """Write all results to the output sheet, wiping the previous run."""

    now_et = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    ok_count  = sum(1 for r in results if r["status"] == "ok")
    err_count = len(results) - ok_count

    header_block = [
        ["EXPECTED MOVES — AUTO-CALCULATED VIA YFINANCE"],
        [
            f"Source: Yahoo Finance options chains via yfinance  |  "
            f"Formula: (ATM Call Ask + ATM Put Ask) × 0.85  |  "
            f"Expiry: nearest available  |  "
            f"Last run: {now_et}  |  "
            f"✓ {ok_count} succeeded   ⚠ {err_count} failed"
        ],
        [""],  # spacer
        [
            "Ticker",
            "Entity Name",
            "Entity Type",
            "Sector",
            "Country",
            "Exchange",
            "Current Price",
            "ATM Strike",
            "ATM Call Ask",
            "ATM Put Ask",
            "Straddle Price",
            "Expected Move $",
            "Expected Move ±%",
            "Avg IV %",
            "Nearest Expiry",
            "Status",
        ],
    ]

    data_rows = []
    for r in results:
        m = meta.get(r["ticker"], {})
        status_cell = "✓" if r["status"] == "ok" else f"⚠ {r['error']}"

        def fmt(val):
            return val if val is not None else "—"

        data_rows.append([
            r["ticker"],
            m.get("name",        ""),
            m.get("entity_type", ""),
            m.get("sector",      ""),
            m.get("country",     ""),
            m.get("exchange",    ""),
            fmt(r["current_price"]),
            fmt(r["atm_strike"]),
            fmt(r["atm_call_ask"]),
            fmt(r["atm_put_ask"]),
            fmt(r["straddle_price"]),
            fmt(r["expected_move_usd"]),
            f"{r['expected_move_pct']}%" if r["expected_move_pct"] is not None else "—",
            fmt(r["avg_iv_pct"]),
            fmt(r["nearest_expiry"]),
            status_cell,
        ])

    ws.clear()
    ws.update(header_block + data_rows, value_input_option="USER_ENTERED")
    log.info("Sheet written: %d rows  (%d OK, %d errors)", len(data_rows), ok_count, err_count)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("═" * 60)
    log.info("Expected Move Pipeline — starting")
    log.info("═" * 60)

    # 1. Auth + connect
    client      = get_sheets_client()
    spreadsheet = get_spreadsheet(client)
    log.info("Connected: %s", spreadsheet.title)

    # 2. Pull optionable tickers from Entity Library
    entities = extract_equity_tickers(spreadsheet)
    if not entities:
        log.warning("No optionable tickers found in Entity Library. Exiting.")
        return

    meta = {e["ticker"]: e for e in entities}

    # 3. Fetch expected move for each ticker
    results = []
    total   = len(entities)

    for i, entity in enumerate(entities, 1):
        ticker = entity["ticker"]
        log.info("[%d/%d]  %s — %s", i, total, ticker, entity["name"])

        result = get_expected_move(ticker)

        if result["status"] == "ok":
            log.info(
                "         ✓  $%.2f  |  ATM $%.2f  |  Straddle $%.2f  |  EM ±$%.2f (±%.2f%%)",
                result["current_price"],
                result["atm_strike"],
                result["straddle_price"],
                result["expected_move_usd"],
                result["expected_move_pct"],
            )
        else:
            log.warning("         ⚠  %s", result["error"])

        results.append(result)

        if i < total:
            time.sleep(RATE_LIMIT_DELAY)

    # 4. Write to Google Sheet
    output_ws = get_or_create_output_sheet(spreadsheet)
    write_results(output_ws, results, meta)

    # 5. Final summary
    ok  = sum(1 for r in results if r["status"] == "ok")
    err = len(results) - ok
    log.info("═" * 60)
    log.info("Done — %d succeeded, %d failed out of %d tickers", ok, err, total)
    log.info("═" * 60)

    if ok == 0:
        raise RuntimeError("All tickers failed — check yfinance / Yahoo Finance status.")


if __name__ == "__main__":
    main()
