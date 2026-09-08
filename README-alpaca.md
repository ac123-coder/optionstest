# Alpaca 1-minute option bars backfill

Upload these files to the repo root (keep the folder structure):

- `alpaca_bars.py`
- `.github/workflows/alpaca-bars.yml`
- `requests/bars_request.json`

Then: Settings → Secrets and variables → Actions → add `ALPACA_KEY` and `ALPACA_SECRET` (paper keys are fine).
Then: Actions tab → "alpaca-bars" → "Run workflow". It fetches for ~45 minutes, commits `data/intraday/*.json.gz`, and re-runs itself hourly until all 5,201 contracts are done (about 3-4 runs).
