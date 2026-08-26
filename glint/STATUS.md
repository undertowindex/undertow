# Glint — Project Status

Read this first if you're a Claude session (or anyone else) picking this project up cold, on any device. Last updated 2026-08-26.

## What this repo is

A value screener that looks for stocks that are cheap relative to their own fundamentals — the "fallen out of love but still worth a look" kind, not just anything with a low price. Screens a combined universe of ~97 FTSE 100 (UK) tickers and ~100 US large-caps (200 total).

**How the scoring works**: pulls 5 fundamental metrics per stock via free Yahoo Finance data (P/E, P/B, dividend yield, ROE, debt/equity), ranks every stock against the rest of the universe on each metric (percentile 0–1), averages into one composite score, and calls it BUY (≥0.65), SELL (≤0.35), or HOLD in between. Investment trusts and anything with too little fundamental data are excluded from ranking entirely (their P/E-only data is a misleading signal). Negative P/E or P/B (financial distress, not cheapness) is also excluded from ranking rather than treated as "extra cheap."

## Where it actually runs

Live on **Railway** — account `undertowindex`, project `respectful-simplicity`, service **`glint`**: daily cron, 07:30 UTC, auto-deploys from this repo's `main` branch, start command `python -m glint.main --email`.

Also runnable manually from this Mac via the Desktop icon `GLINT.command` — a **symlink** to `Run Glint.command` in this repo. Only edit the real file in this repo, never the Desktop symlink, or its custom icon gets stripped. The manual run does **not** send email (no `--email` flag) — it just opens the report locally in your browser.

## Required environment variables

- `RESEND_API_KEY` — for the automated Railway run's email. Same key as Undertow/Ark use, same Resend account.
- `ALERT_EMAIL` — defaults to `micahbrown4@me.com` if unset (see `glint/email_sender.py`)
- No API key needed for the actual stock data — it's all free Yahoo Finance (yfinance library)

## Repo layout

- `glint/tickers.py` — the FTSE_100 and US_LARGE_CAP hardcoded ticker lists (hardcoded because free-tier data doesn't offer a live "list every ticker on this exchange" endpoint)
- `glint/fetch.py` — pulls fundamentals via yfinance
- `glint/score.py` — the ranking/scoring logic, including the negative-ratio exclusion fix
- `glint/report.py` — builds the plain-text and color-coded HTML report
- `glint/email_sender.py` — sends via Resend (added 2026-08-26, for the Railway cron only)
- `glint/main.py` — CLI entry point, `--email` flag controls whether it actually sends

## Things that look related but aren't

- **The `glint/` folder inside the separate `undertow` repo** (`~/undertow/glint/`) is an older, vendored copy that feeds Undertow's own email section and Ark's "quality survivor" picks. It is not this repo and doesn't share code with it — changes here don't propagate there and vice versa.
- **"Market Vital Signs"** — a separate, more polished system built by **ChatGPT** (not Claude), doing something similar (screening for ideas) but deliberately built independently, on Railway project `charismatic-balance`, GitHub repo `undertowindex/market-vital-signs`. This exists specifically so the user can compare Claude's approach (this repo) against ChatGPT's (that one) — don't try to merge scope or match its feature set.

## History

Built from scratch 2026-08-24, initially UK-only (FTSE 100), using free yfinance data specifically so the user could validate the screening logic before committing to any paid data subscription. US large-caps added 2026-08-25. Moved from local-only to GitHub + Railway (private repo) 2026-08-25/26, at the user's request, once the UK+US logic was validated as sound and email-sending was added.
