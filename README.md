# DSE live data bridge (Python + bdshare)

DSE keeps changing its website (the old scraper URL now returns HTTP 410), so this uses the open-source
[`bdshare`](https://pypi.org/project/bdshare/) library, which tries DSE's current JSON API first and falls back to
`old.dsebd.org`. Needs bdshare 1.2.7 or newer.

## Run
```bash
pip install -r requirements.txt
python -c "from dse_data import fetch_market as f; d=f(); print(d['asOf'], d['count'], d['stocks'][0])"   # test
python server.py        # or: python -m uvicorn server:app --port 3000
```
Open http://localhost:3000 for the simulator. It detects the API and offers "Start live portfolio".
If the test fails, send me the error: bdshare's column names or DSE's site may have changed.

Note: `dse.com.bd`'s new API currently returns HTTP 403 to scripts, so `dse_data.py` sets `BDSHARE_SOURCE=legacy` and reads `old.dsebd.org` directly. Set `BDSHARE_SOURCE=new` to change that.

## API
- `GET /api/dse-live` (optional `?symbols=GP,BATBC`): `{asOf, stocks:[{symbol, ltp, ycp, high, low, change, changePercent, category, close, trades, valueMn, volume, traded}]}`.
  Cached 60 s while the market is open (Sun-Thu 10:00-14:30 Dhaka), 15 min otherwise; serves the last good copy with `stale: true` if DSE is down.
- `GET /api/dse-history/{symbol}`: last 60 daily closes plus the real 52-week high/low (cached 6 h).
- `asOf` is the trading date. If a day passes with no volume change in any stock, it is treated as a holiday and the date doesn't advance.

## Categories (A/B/N/Z)
The price feed has no category. Put overrides in `categories.json`, e.g. `{"GP":"A","SOMEZSTOCK":"Z"}`.
Unlisted symbols default to `A`. Look up any Z-category stock you trade on DSE's company page, because Z changes settlement to T+3.

## Daily snapshot (optional)
`python snapshot.py` saves `eod/<date>.json`; schedule it Sun-Thu around 15:00 Dhaka as an end-of-day backup.

`legacy-node/` holds the earlier Node scraper, no longer needed.

## Database (simulator.db)
Created automatically next to server.py. Tables: `users`, `states` (each user's full portfolio), `transactions` (every trade with its journal note),
`ticks` (whole-market prices every 5 min while DSE is open), `daily` (end-of-day prices), `companies` (name, sector, category, P/E and every field from the DSE company page).
- Company details are fetched once in the background (about 0.7 s per company, so roughly 5 minutes the first time). Please check DSE's terms before running it.
- Keep the server running 10:00-14:30 Dhaka (Sun-Thu) to build intraday history. Back up by copying `simulator.db`.
- Inspect it with any SQLite viewer, e.g. https://sqlitebrowser.org

## Accounts and "which source has today's prices?"
- `default` is a shared test account with no password. Create your own account in Settings: usernames are unique (case-insensitive), passwords are stored salted and hashed, and each account's data can only be read with its own sign-in token. Run the server on localhost only.
- Prices refresh when the page loads and when you press Refresh (no background polling in the browser).
- `python diagnose.py` shows the date DSE's site says its prices are from, whether the new JSON API is reachable, and which source the server uses. If the app shows an amber "These prices are from ..." banner, DSE's feed itself has not published newer prices.

## Vercel
Vercel's disk is read-only and temporary, so accounts and portfolios do not persist there (the app warns you). If `/` returns the JSON "API is running" message,
serve the page statically and send only the API to Python, e.g. `vercel.json`:
```json
{ "rewrites": [ { "source": "/api/(.*)", "destination": "/api/server" } ] }
```
with the page at `public/index.html` in the repo root. For real persistence use your own computer, a host with a disk, or move the database to Supabase.
