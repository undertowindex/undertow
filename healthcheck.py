#!/usr/bin/env python3
"""
Undertow/Ark FEED HEALTH MONITOR — a watchdog for the whole pipeline.

Runs independently of Undertow and Ark. It does NOT re-fetch market data or
recompute anything — it inspects the daily handoff Gist that Undertow writes,
plus the GitHub token's own expiry, and emails a short health summary. The
point is to catch SILENT failure: a crash-warning system that quietly stops
working is worse than none, because it gives false comfort.

Checks:
  1. Handoff freshness — is ark_inputs.json's generated_at from today (UTC)?
     A stale timestamp means Undertow didn't run / didn't publish — the
     loudest alarm, because everything downstream is then blind.
  2. GitHub token expiry — warns if the token GITHUB_GIST_TOKEN expires within
     30 days (the known November lapse that would blind Ark's data feed).
  3. Signal sanity — composite score present and within 0..max; note if Glint
     candidates are flowing.

Sends an email ONLY containing the health summary. Never places or changes
anything. Designed to run on its own Railway cron (e.g. weekly), reading the
same GITHUB_GIST_TOKEN / ARK_HANDOFF_GIST_ID / RESEND_API_KEY the others use.

Exit code 0 = all healthy; 1 = at least one WARN/FAIL (visible in Railway logs).
"""

import os
import sys
import json
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("requests not available", flush=True)
    sys.exit(2)

GITHUB_GIST_TOKEN = os.environ.get("GITHUB_GIST_TOKEN", "").strip()
ARK_HANDOFF_GIST_ID = os.environ.get("ARK_HANDOFF_GIST_ID", "").strip()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "micahbrown4@me.com").split(",")[0].strip()

TOKEN_WARN_DAYS = 30


def check_handoff_freshness():
    """Return (status, message) — reads the handoff Gist and checks its age."""
    if not (GITHUB_GIST_TOKEN and ARK_HANDOFF_GIST_ID):
        return "FAIL", "GITHUB_GIST_TOKEN or ARK_HANDOFF_GIST_ID not set — can't check the handoff."
    try:
        r = requests.get(
            f"https://api.github.com/gists/{ARK_HANDOFF_GIST_ID}",
            headers={"Authorization": f"token {GITHUB_GIST_TOKEN}",
                     "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        return "FAIL", f"Couldn't reach the handoff Gist: {e}"

    files = r.json().get("files", {})
    if "ark_inputs.json" not in files:
        return "FAIL", "Handoff Gist has no ark_inputs.json — Undertow may never have published."
    try:
        payload = json.loads(files["ark_inputs.json"]["content"])
    except (KeyError, json.JSONDecodeError) as e:
        return "FAIL", f"Handoff ark_inputs.json is unreadable: {e}"

    ts = payload.get("generated_at", "")
    try:
        gen = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return "WARN", f"Handoff present but generated_at is unparseable ('{ts}')."

    age_h = (datetime.now(timezone.utc) - gen).total_seconds() / 3600
    # Undertow runs ~07:00 UTC daily. Anything older than ~28h means a run was missed.
    if age_h > 28:
        return "FAIL", f"Handoff is STALE — last updated {age_h:.0f}h ago ({ts}). Undertow may not be running."
    return "OK", f"Handoff fresh — updated {age_h:.0f}h ago ({ts}).", payload


def check_token_expiry():
    """Warn if the GitHub token expires within TOKEN_WARN_DAYS. GitHub returns
    the fine-grained/classic token expiry in the 'github-authentication-token-
    expiration' response header on authenticated calls."""
    if not GITHUB_GIST_TOKEN:
        return "FAIL", "No GITHUB_GIST_TOKEN to check."
    try:
        r = requests.get("https://api.github.com/user",
                         headers={"Authorization": f"token {GITHUB_GIST_TOKEN}"},
                         timeout=15)
    except requests.exceptions.RequestException as e:
        return "WARN", f"Couldn't check token expiry: {e}"

    exp = r.headers.get("github-authentication-token-expiration", "").strip()
    if not exp:
        return "OK", "Token has no expiry set (or GitHub didn't report one) — no imminent lapse."
    # header looks like '2026-11-05 12:00:00 UTC' or ISO; handle both
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S UTC"):
        try:
            exp_dt = datetime.strptime(exp, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            exp_dt = None
    if exp_dt is None:
        return "WARN", f"Token expiry present but unparseable ('{exp}') — check it manually."

    days = (exp_dt - datetime.now(timezone.utc)).days
    if days < 0:
        return "FAIL", f"Token EXPIRED on {exp}. Ark's data feed is blind until renewed."
    if days <= TOKEN_WARN_DAYS:
        return "WARN", f"Token expires in {days} days ({exp}) — renew soon to avoid a feed blackout."
    return "OK", f"Token valid — expires in {days} days ({exp})."


def check_signal_sanity(payload):
    if not payload:
        return "WARN", "No handoff payload to sanity-check."
    comp = payload.get("composite", {})
    score, mx, sig = comp.get("score"), comp.get("max"), comp.get("signal")
    if score is None or mx is None:
        return "WARN", "Composite score/max missing from handoff."
    if not (0 <= score <= mx):
        return "FAIL", f"Composite score {score} is out of range (max {mx}) — scoring bug likely."
    n_glint = len(payload.get("glint_candidates", []))
    return "OK", f"Signal sane — {score}/{mx} {sig}; Glint candidates flowing: {n_glint}."


def main():
    lines = []
    worst = "OK"
    rank = {"OK": 0, "WARN": 1, "FAIL": 2}

    fresh = check_handoff_freshness()
    payload = fresh[2] if len(fresh) > 2 else None
    for status, msg in [fresh[:2], check_token_expiry(), check_signal_sanity(payload)]:
        icon = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌"}[status]
        lines.append(f"{icon} {status}: {msg}")
        if rank[status] > rank[worst]:
            worst = status

    header = {
        "OK":   "🟢 Undertow/Ark health: ALL SYSTEMS GO",
        "WARN": "🟡 Undertow/Ark health: ATTENTION NEEDED",
        "FAIL": "🔴 Undertow/Ark health: ACTION REQUIRED",
    }[worst]

    body = header + "\n" + "=" * 56 + "\n\n" + "\n".join(lines) + "\n\n" + \
        "This is the weekly feed-health watchdog. It reads the handoff Gist and\n" + \
        "token status only — it never trades or changes anything.\n"

    print(body, flush=True)

    if RESEND_API_KEY:
        try:
            resp = requests.post(
                "https://api.resend.com/emails",
                json={"from": "Undertow Health <onboarding@resend.dev>",
                      "to": [ALERT_EMAIL],
                      "subject": f"[Health] {header}",
                      "text": body},
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                timeout=30,
            )
            print(f"Health email sent (status {resp.status_code}).", flush=True)
        except Exception as e:
            print(f"Health email FAILED: {e}", flush=True)
    else:
        print("RESEND_API_KEY not set — printed only, no email.", flush=True)

    sys.exit(0 if worst == "OK" else 1)


if __name__ == "__main__":
    main()
