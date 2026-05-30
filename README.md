# Expected Move Pipeline

Automatically fetches the ATM straddle-based **Expected Move (±%)** for every
equity in your `📊 Earnings` sheet and writes results to a `📈 Expected Moves`
tab — every weekday at 5 PM ET, via GitHub Actions. Free. No server needed.

---

## How It Works

```
GitHub Actions (free, scheduled)
    └── runs fetch_expected_moves.py
            ├── reads tickers from "📊 Earnings" tab in your Google Sheet
            ├── calls Yahoo Finance via yfinance for each ticker
            │       → finds nearest-expiry options chain
            │       → locates ATM strike (closest to current price)
            │       → grabs ATM Call Ask + ATM Put Ask
            │       → Expected Move $ = (Call Ask + Put Ask) × 0.85
            │       → Expected Move % = Expected Move $ ÷ Current Price
            └── writes all results to "📈 Expected Moves" tab
```

---

## One-Time Setup (~20 minutes)

### Step 1 — Fork / clone this repo to your GitHub account

If you don't have a repo yet:
1. Go to github.com → **New repository**
2. Name it anything (e.g. `economic-calendar-pipeline`)
3. Clone it locally, drop these files in, push

---

### Step 2 — Create a Google Cloud Service Account

This is the "bot user" that will read/write your Google Sheet.

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable these two APIs (search in the API Library):
   - **Google Sheets API**
   - **Google Drive API**
4. Go to **IAM & Admin → Service Accounts → Create Service Account**
   - Name: `expected-move-bot` (or anything)
   - Role: **Editor** (or just "Basic → Editor")
5. Click the service account → **Keys tab → Add Key → Create new key → JSON**
6. Download the `.json` file — you'll need its contents in Step 4

---

### Step 3 — Share your Google Sheet with the service account

1. Open your Google Sheet
2. Click **Share**
3. Paste the service account email (looks like `expected-move-bot@your-project.iam.gserviceaccount.com`)
4. Give it **Editor** access
5. Uncheck "Notify people" → **Share**

---

### Step 4 — Add GitHub Secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

Add these two secrets:

#### `GOOGLE_CREDENTIALS_JSON`
Open the `.json` file you downloaded in Step 2.
Copy the **entire contents** (the whole JSON object) and paste it as the secret value.

It looks like:
```json
{
  "type": "service_account",
  "project_id": "your-project",
  "private_key_id": "...",
  "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...",
  "client_email": "expected-move-bot@your-project.iam.gserviceaccount.com",
  ...
}
```

#### `SPREADSHEET_ID`
Copy the ID from your Google Sheet URL:
```
https://docs.google.com/spreadsheets/d/  >>>THIS_PART<<<  /edit
```
Paste just the ID string (no slashes) as the secret value.

---

### Step 5 — Push the files to your repo

Your repo structure should look like:
```
your-repo/
├── .github/
│   └── workflows/
│       └── expected_moves.yml
├── fetch_expected_moves.py
├── requirements.txt
└── README.md
```

Push to `main`. GitHub Actions will pick up the schedule automatically.

---

### Step 6 — Run it manually to verify

1. Go to your repo on GitHub
2. Click **Actions** tab
3. Click **Fetch Expected Moves** in the left sidebar
4. Click **Run workflow → Run workflow**
5. Watch the logs — you should see each ticker being fetched
6. Open your Google Sheet — a new `📈 Expected Moves` tab will appear

---

## Schedule

Runs **Monday through Friday at ~5:00 PM ET** (after market close).

To change the time, edit the `cron` line in `.github/workflows/expected_moves.yml`:
```yaml
- cron: "0 21 * * 1-5"   # 21:00 UTC = 5 PM EDT
```
Use [crontab.guru](https://crontab.guru) to build a different schedule.

---

## Output Format

The `📈 Expected Moves` tab will contain:

| Ticker | Company | Report Date | Sector | Current Price | ATM Strike | ATM Call Ask | ATM Put Ask | Straddle Price | Expected Move $ | Expected Move ±% | Avg IV % | Expiration | Status |
|--------|---------|-------------|--------|---------------|------------|--------------|-------------|----------------|-----------------|-------------------|----------|------------|--------|
| AAPL | Apple Inc. | Jan 28 | Tech | 214.11 | 215.00 | 1.42 | 1.38 | 2.80 | 2.38 | 1.11% | 26.4% | 2027-01-29 | ✓ |
| MSFT | Microsoft | Jan 28 | Tech | 415.30 | 415.00 | 3.10 | 3.05 | 6.15 | 5.23 | 1.26% | 22.1% | 2027-01-29 | ✓ |

---

## Troubleshooting

### "No options chain available"
- The ticker is not optionable (some small-caps, ETFs, international tickers)
- The ticker symbol is wrong — double-check vs Yahoo Finance
- International tickers need Yahoo's format: `HSBA.L` (London), `7203.T` (Tokyo)

### "No price data returned"
- Ticker may be delisted or suspended
- Could be a temporary Yahoo Finance outage — re-run manually tomorrow

### The whole run fails
- Yahoo Finance sometimes blocks bulk requests temporarily
- The yfinance library may need updating: bump the version in `requirements.txt`
- Check [github.com/ranaroussi/yfinance](https://github.com/ranaroussi/yfinance) for recent issues

### GitHub Actions not running on schedule
- GitHub pauses scheduled workflows on repos with **no commits in 60 days**
- Fix: make any small commit (e.g. update README) to wake the repo back up

---

## Keeping It Updated

When Yahoo Finance breaks yfinance (happens a few times a year):
1. Check the [yfinance releases page](https://pypi.org/project/yfinance/#history)
2. Update the version in `requirements.txt`
3. Push — next run will use the new version

---

## International Tickers (Future)

Yahoo Finance uses exchange suffixes for non-US tickers:

| Exchange | Suffix | Example |
|----------|--------|---------|
| London Stock Exchange | `.L` | `HSBA.L` |
| Tokyo Stock Exchange | `.T` | `7203.T` |
| Frankfurt | `.DE` | `SAP.DE` |
| Paris | `.PA` | `MC.PA` |
| Toronto | `.TO` | `RY.TO` |

Options data coverage varies significantly outside the US. Expect more
"No options chain available" errors for international names — this is normal.
The script handles these gracefully with a ⚠ status rather than crashing.
