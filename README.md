# FiscalPulse

**OECD Sovereign Debt Intelligence Dashboard**

Live, open-source dashboard tracking macrofiscal risk and investment signals across 20 OECD economies. Data sourced directly from the IMF World Economic Outlook (WEO) API and auto-refreshed via GitHub Actions.

## Live demo

→ [fiscalpulse on GitHub Pages](https://YOUR-USERNAME.github.io/fiscalpulse/)

## Features

- **Investments view (DZA)** — composite scoring, debt trajectory, r−g snowball effect, fiscal balance decomposition
- **Risk view (DR)** — stress testing (3 scenarios), structural balance, interest burden, debt-at-risk simulation
- **20 OECD countries** selected via flag buttons
- **Light / dark mode**
- **Auto-updated** monthly via GitHub Actions (April & October WEO releases captured automatically)

## Data source

IMF World Economic Outlook — SDMX 3.0 API (`api.imf.org`).
Historical data from 2015, IMF projections through 2031.

## Local setup

```bash
pip install -r requirements.txt
python fetch_data.py          # fetch fresh data from IMF API
python fetch_data.py --from-cache   # use existing local cache

# then open index.html in a browser (via a local server):
python -m http.server 8000
# open http://localhost:8000
```

## Deploy to GitHub Pages

1. Push this repo to GitHub
2. Go to **Settings → Pages → Source**: set to `main` branch, root `/`
3. GitHub Actions will auto-update `data/fiscal_data.json` on the 1st of each month

## Methodology

| Component | Weight | Score 1 → 5 |
|-----------|--------|-------------|
| Gross Debt % GDP | 25% | <40% → 1 ... >120% → 5 |
| 3Y Debt Trajectory | 20% | falling >5pp → 1 ... rising >7pp → 5 |
| Structural Balance | 20% | >−1% → 1 ... <−5% → 5 |
| r−g Snowball Spread | 20% | <−2pp → 1 ... >3pp → 5 |
| Interest / Revenue | 15% | <5% → 1 ... >20% → 5 |

**Stress scenarios** shift growth (Δg), effective interest rate (Δr), and primary balance (ΔPB) simultaneously for 3 years using the standard debt dynamics equation:

```
Debt(t+1) = Debt(t) × (1 + r_shocked) / (1 + g_shocked) − PB_shocked
```

## License

MIT
