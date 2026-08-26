# Undertow / Ark — Project Status

Read this first if you're a Claude session (or anyone else) picking this project up cold, on any device. Last updated 2026-08-26.

## What this repo is

**Undertow** is an early-warning system for market stress — it watches for "seismic tremors" before a crash, not after. It scores 7 layers of market data (equity breadth, credit spreads/yield curve, dollar/yen/copper-gold, COT positioning & repo stress, options put/call ratio, SKEW tail-risk pricing, dealer gamma exposure) into a composite GREEN/AMBER/RED signal, then runs that signal past a "Boardroom" of LLM-simulated investor personas (Buffett, Burry, Dalio, etc., living and historical) for a second opinion. `undertow.py` is the entry point.

**Ark** (`ark.py`, same repo) is a separate, advisory-only "crash insurance" watcher. It reads Undertow's last published signal (via a private GitHub Gist handoff) and, specifically at the moment risk shifts from calm to elevated, proposes index-only hedges plus "quality survivor" long ideas sourced from Glint. It never places real or paper trades — advisory only.

**Glint** also lives partly in this repo (`glint/` folder) as an older, vendored value-screener that feeds Undertow's email + Ark's picks. The current, actively developed Glint is a **separate standalone repo**: `undertowindex/glint`. Don't confuse the two.

## Where it actually runs

Both `undertow` and `ark` run live on **Railway** — account `undertowindex`, project **`respectful-simplicity`**:
- **`undertow` service**: daily cron, 07:00 UTC, auto-deploys from this repo's `main` branch
- **`ark` service**: daily cron, 07:15 UTC, same repo/branch, different start command (`python ark.py` vs `undertow`'s `python undertow.py`)

Both are also runnable manually from this Mac via Desktop icons — `UNDERTOW.command` and `ARK.command` — which are **symlinks** to `Run Undertow.command` / `Run Ark.command` in this repo. Only edit the real files in this repo, never the Desktop symlinks directly, or their custom icons get stripped.

## Required environment variables

Set on Railway (and in `~/undertow/.env.local`, gitignored, for local manual runs):
- `FRED_API_KEY` — Federal Reserve data, free signup at fred.stlouisfed.org
- `ANTHROPIC_API_KEY` — powers the Boardroom LLM calls
- `RESEND_API_KEY` — email sending (see caveat below)
- `GITHUB_GIST_TOKEN` + `ARK_HANDOFF_GIST_ID` — the Undertow→Ark signal handoff (Ark only)
- `IBKR_TOKEN` / `IBKR_QUERY_ID` — optional, not currently set; without them Layer 8 (your live IBKR portfolio) just reports "unavailable," doesn't break anything

## Known issues fixed 2026-08-24 to 2026-08-26

- **Layer 3c (put/call ratio) was dead since 2026-07-30** — silently returning 0/0 because its CBOE data source was a frozen historical archive, not live. Fixed: now derived from the live SPY options chain (same data Layer 3e/GEX already uses), shared in one fetch.
- **Sanity checks couldn't see silent layer failures** — a layer that completely failed to fetch data scored 0 (looked calm) with the real cause buried in a flags list nobody read. Fixed: any layer producing no data, or any degraded-fetch flag, now surfaces as a loud warning in the actual email.
- **A failed email send was invisible** — `send_email()` used to return nothing regardless of success/failure, so a broken send looked identical to a successful run. Fixed: now returns real success/failure, and the whole run exits non-zero if the email didn't go out.
- **Two accidental duplicate Railway services existed** (`alluring-light`, `acceptable-growth`) — same repo/branch as `undertow`, running on overlapping schedules, silently doubling emails and Boardroom LLM costs every day. Deleted 2026-08-25.
- **Resend email restricted to account owner only, as of 2026-08-26.** Resend's shared `onboarding@resend.dev` sender can only email the account's own verified address (`micahbrown4@me.com`). It was configured to also email 3 other people, which Resend blocks entirely without a verified custom domain. `ALERT_EMAIL` default was changed to just the account owner until a domain is verified at resend.com/domains — add the others back there once that's done.

## Things that look related but aren't

- **"Undertow 2.0"** — a separate, half-built rebuild prototyped via a *different* Claude session (likely Claude.ai directly, not this Code setup), running in a Claude cloud container (`/home/claude/undertow/...`), sent as manual test emails from `micah.brown7@gmail.com` between 2026-08-21 and 08-25. Broken (couldn't persist state between runs, its web-fetching was blocked). Confirmed inactive as of 2026-08-26 — no scheduled tasks exist for it. Nothing to do with this repo.
- **"Market Vital Signs"** — a real, live, separate system built by **ChatGPT** (not Claude), also on Railway under the same `undertowindex` account but a different project (`charismatic-balance`), GitHub repo `undertowindex/market-vital-signs`. Deliberately built as a parallel/independent tool to Glint so the user can compare model outputs — don't try to merge scope or align it with this repo.
