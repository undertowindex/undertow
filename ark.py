import urllib.request
#!/usr/bin/env python3
"""
Ark — advisory-only crash-insurance and quality-survivor signal generator.

Ark runs LOCALLY (not on Railway). Each run it:
  1. Pulls today's Undertow + Glint signal from the private "Ark handoff" Gist
     that Railway publishes after every Undertow run.
  2. Compares today's composite score against yesterday's (kept in a small
     local state file) to detect the "early tremor" window: the score is
     climbing while the signal is still GREEN, or has just crossed into
     AMBER for the first time. That is the window Ark cares about — implied
     volatility is still cheap, but stress is visibly building.
  3. If that window is open, it drafts advisory trade ideas:
       - Shorts: broad index/ETF puts (SPY, QQQ) for speed
       - Longs: Glint's highest-scoring "quality survivor" candidates
       - Insurance: long-dated (12-18mo) SPX/SPY puts + VIX calls, bought
         while premium is still cheap
  4. It also re-checks any advisory positions it suggested on a previous run:
     if they have been open a long time without the crash materializing, it
     flags theta decay so Micah knows to reassess, not just let it ride.

Ark NEVER places, modifies, or cancels any real or paper order. It only
prints/saves a report. Every trade Micah takes based on this report is
entered by Micah, by hand, in IBKR.
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration — reads the same two env vars Railway uses for the handoff.
# ---------------------------------------------------------------------------
GITHUB_GIST_TOKEN = os.environ.get("GITHUB_GIST_TOKEN", "")
ARK_HANDOFF_GIST_ID = os.environ.get("ARK_HANDOFF_GIST_ID", "")
GIST_FILENAME = "ark_inputs.json"

# Where Ark keeps its state (history + open advisory positions).
# If ARK_STATE_GIST_ID is set (as on Railway, where the container's disk is
# wiped between runs), state lives in a secret Gist. If unset (running
# locally on the Mac), state stays in the local file exactly as before.
ARK_STATE_GIST_ID = os.environ.get("ARK_STATE_GIST_ID", "")
STATE_GIST_FILENAME = "ark_state.json"

# Where Ark keeps its own memory of past runs and open advisory positions.
# Override with ARK_STATE_PATH if you ever want it somewhere else.
STATE_PATH = Path(os.environ.get("ARK_STATE_PATH", str(Path.home() / "undertow" / "ark_state.json")))
REPORTS_DIR = Path(os.environ.get("ARK_REPORTS_DIR", str(Path.home() / "undertow" / "ark_reports")))

# How many calendar days an open advisory hedge can sit before Ark starts
# flagging theta decay if the crash hasn't shown up yet.
THETA_WARNING_DAYS = 45

# Glint value_score is 0-3 (P/E, P/B, FCF-yield pass count). Score 3 means a
# candidate passed every quality/value screen Glint runs — that's Ark's
# "quality survivor" bar for the long book.
QUALITY_SURVIVOR_MIN_SCORE = 3

SIGNAL_ORDER = {"GREEN": 0, "AMBER": 1, "RED": 2}


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------
def fetch_ark_inputs():
    """Pull today's ark_inputs.json from the private handoff Gist."""
    if not GITHUB_GIST_TOKEN or not ARK_HANDOFF_GIST_ID:
        print("⚠️  GITHUB_GIST_TOKEN or ARK_HANDOFF_GIST_ID not set locally.")
        print("    Ark needs both — same values you put in Railway — set in this")
        print("    machine's environment before it can pull today's signal.")
        sys.exit(1)

    url = f"https://api.github.com/gists/{ARK_HANDOFF_GIST_ID}"
    headers = {
        "Authorization": f"token {GITHUB_GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"❌ Could not reach the Ark handoff Gist: {e}")
        sys.exit(1)

    gist = r.json()
    files = gist.get("files", {})
    if GIST_FILENAME not in files:
        print(f"❌ Gist found, but it has no '{GIST_FILENAME}' file in it.")
        sys.exit(1)

    try:
        return json.loads(files[GIST_FILENAME]["content"])
    except (KeyError, json.JSONDecodeError) as e:
        print(f"❌ Could not parse today's signal: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Local state (Ark's own memory — separate from anything on Railway)
# ---------------------------------------------------------------------------
def _state_gist_request(method, payload=None):
    url = f"https://api.github.com/gists/{ARK_STATE_GIST_ID}"
    headers = {
        "Authorization": f"token {GITHUB_GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    r = requests.request(method, url, headers=headers, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def _load_state_from_gist():
    try:
        gist = _state_gist_request("GET")
    except requests.exceptions.RequestException as e:
        # Deliberately fatal: if we can't READ existing state, starting
        # "fresh" would wipe open positions the moment we saved. Better to
        # skip a day than lose the position book.
        print(f"❌ Could not reach the Ark state Gist: {e}")
        sys.exit(1)
    files = gist.get("files", {})
    if STATE_GIST_FILENAME not in files:
        print("⚠️  State Gist has no ark_state.json yet — starting with fresh state.")
        return {"history": [], "open_positions": []}
    try:
        return json.loads(files[STATE_GIST_FILENAME]["content"])
    except (KeyError, json.JSONDecodeError):
        print("⚠️  State Gist content was unreadable — starting with fresh state.")
        return {"history": [], "open_positions": []}


def _save_state_to_gist(state):
    payload = {"files": {STATE_GIST_FILENAME: {"content": json.dumps(state, indent=2)}}}
    try:
        _state_gist_request("PATCH", payload)
    except requests.exceptions.RequestException as e:
        print(f"❌ Could not save state to the Ark state Gist: {e}")
        sys.exit(1)


def load_state():
    if ARK_STATE_GIST_ID:
        return _load_state_from_gist()
    if not STATE_PATH.exists():
        return {"history": [], "open_positions": []}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        print(f"⚠️  {STATE_PATH} was unreadable — starting a fresh state file.")
        return {"history": [], "open_positions": []}


def save_state(state):
    if ARK_STATE_GIST_ID:
        _save_state_to_gist(state)
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Core decision logic
# ---------------------------------------------------------------------------
def classify_window(today, history):
    """
    Decide which window we're in:
      - "too_late"   : signal is already RED — premium has almost certainly
                        already repriced; buying insurance now is expensive,
                        not cheap.
      - "act"        : signal has JUST crossed from GREEN into AMBER (first
                        AMBER reading after a GREEN one) — the clearest
                        version of the early-tremor buy window.
      - "watch"      : still GREEN, but the composite score rose versus the
                        last recorded run — stress building under the
                        surface, worth flagging even though it's not yet
                        actionable.
      - "calm"       : GREEN and flat/falling — nothing to do.
    """
    signal = today["composite"]["signal"]
    score = today["composite"]["score"]

    if signal == "RED":
        return "too_late", None

    prev = history[-1] if history else None
    if prev is None:
        # First run ever — no trend to compare against yet.
        return ("watch", None) if signal == "AMBER" else ("calm", None)

    prev_signal = prev["signal"]
    prev_score = prev["score"]

    if signal == "AMBER" and prev_signal == "GREEN":
        return "act", prev_score

    if signal == "GREEN" and score > prev_score:
        return "watch", prev_score

    return "calm", prev_score


def pick_quality_survivors(glint_candidates):
    survivors = [c for c in glint_candidates if c.get("value_score", 0) >= QUALITY_SURVIVOR_MIN_SCORE]
    survivors.sort(key=lambda c: c.get("value_score", 0), reverse=True)
    return survivors


def build_trade_ideas(today, quality_survivors):
    score = today["composite"]["score"]
    max_score = today["composite"]["max"]

    ideas = {
        "shorts": [
            {
                "instrument": "SPY long-dated put (12-18mo)",
                "rationale": (
                    "Broad-market hedge, bought for speed of execution while "
                    "IV is still cheap — this is the core insurance leg."
                ),
            },
            {
                "instrument": "QQQ long-dated put (12-18mo)",
                "rationale": "Tech-weighted hedge alongside SPY, since concentration risk usually shows up there first.",
            },
            {
                "instrument": "IWM long-dated put (12-18mo)",
                "rationale": (
                    "Small-cap hedge. Small caps are higher-beta and tend to "
                    "fall hardest in a broad selloff, so an IWM put adds "
                    "downside breadth beyond the large-cap SPY/QQQ names."
                ),
            },
            {
                "instrument": "HYG put or credit hedge (12-18mo)",
                "rationale": (
                    "Credit hedge. High-yield bonds (HYG) crack early when "
                    "stress builds — a put here captures the credit leg of a "
                    "downturn that pure equity puts miss. Ties directly to "
                    "Undertow's own credit-spread layer flashing."
                ),
            },
            {
                "instrument": "VIX call spread (12-18mo)",
                "rationale": (
                    "Convexity play — VIX calls are cheapest exactly when "
                    "nobody wants them, which is now. Structured as a call "
                    "SPREAD (buy lower strike, sell higher) to cap the premium "
                    "paid, since outright VIX calls bleed hard if the spike "
                    "doesn't come."
                ),
            },
        ],
        "shorts_note": (
            "By design, Ark's short/insurance side is INDEX-ONLY — broad hedges "
            "(equity indices, small caps, credit) plus VIX convexity. Individual "
            "single-stock shorts are deliberately excluded: unbounded loss, borrow "
            "cost, and squeeze risk make them a different risk class, and a weak "
            "name can stay overvalued for years. Speed and defined risk over size."
        ),
        "longs": [],
    }

    for c in quality_survivors:
        ideas["longs"].append(
            {
                "ticker": c["ticker"],
                "price": c["price"],
                "value_score": c["value_score"],
                "rationale": (
                    f"Passed all of Glint's value + quality checks (score "
                    f"{c['value_score']}/3) — this is the 'quality survivor' "
                    f"pool to rotate into if broad markets do crack."
                ),
            }
        )

    if not ideas["longs"]:
        ideas["longs_note"] = (
            "No candidate cleared the quality-survivor bar (value_score == 3) "
            "in today's Glint run — nothing to add to the long book yet."
        )

    ideas["context"] = (
        f"Undertow composite {score}/{max_score}, signal {today['composite']['signal']}."
    )
    return ideas


def refresh_open_positions(state, today_date_str, window):
    """Age existing advisory positions and flag theta decay; mark any that
    should be considered resolved because the crash actually showed up."""
    signal = None
    updates = []
    for pos in state["open_positions"]:
        opened = datetime.fromisoformat(pos["opened"])
        now = datetime.fromisoformat(today_date_str)
        age_days = (now - opened).days
        pos["age_days"] = age_days

        if window == "too_late":
            pos["status"] = "crash_materialized"
            pos["note"] = (
                "Signal has moved to RED since this position was suggested — "
                "the scenario it was insurance against may be playing out. "
                "Worth actively reviewing this hedge now, not just monitoring."
            )
        elif age_days >= THETA_WARNING_DAYS and pos["status"] == "open":
            pos["status"] = "theta_warning"
            pos["note"] = (
                f"Open {age_days} days with no crash yet — long-dated options "
                f"still have time value, but at {age_days} days it is worth "
                f"checking whether the thesis still holds or the position "
                f"should be trimmed/rolled."
            )
        updates.append(pos)
    state["open_positions"] = updates
    return state


def record_new_advisory(state, today_date_str, ideas):
    entry = {
        "opened": today_date_str,
        "age_days": 0,
        "status": "open",
        "ideas": ideas,
    }
    state["open_positions"].append(entry)
    return state


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def render_report(today, window, prev_score, ideas, state, dry_run):
    score = today["composite"]["score"]
    max_score = today["composite"]["max"]

    # On CALM days Ark has nothing to act on, so keep the email to a single
    # line — the subject already carries "CALM" and the date. Ark should only
    # be verbose on days it actually has something to say (WATCH/ACT/TOO_LATE).
    # The one exception: if there are open advisory positions being tracked,
    # fall through to the full report so those never get hidden on a calm day.
    open_positions = [p for p in state["open_positions"] if p["status"] != "closed"]
    if window == "calm" and not open_positions:
        line = f"🟢 CALM — nothing to do. Undertow {score}/{max_score}, GREEN and flat/falling."
        if dry_run:
            line += "  (dry run — no state written)"
        return line

    lines = []
    ts = today.get("generated_at", "unknown")
    lines.append("=" * 64)
    lines.append("ARK — advisory report (local, manual-execution only)")
    lines.append(f"Undertow signal generated: {ts}")
    lines.append("=" * 64)
    lines.append("")

    signal = today["composite"]["signal"]
    lines.append(f"Composite score: {score}/{max_score}  |  Signal: {signal}")
    if prev_score is not None:
        lines.append(f"Previous recorded score: {prev_score}")
    lines.append("")

    if window == "calm":
        lines.append("🟢 CALM — no early-tremor signal today. No new advisory positions.")
    elif window == "watch":
        lines.append(
            "🟡 WATCH — score is rising while still GREEN. Stress is building "
            "under the surface but this is not yet the buy window. No new "
            "positions suggested — just keep an eye on tomorrow's run."
        )
    elif window == "act":
        lines.append(
            "🟠 ACT — signal has just crossed from GREEN to AMBER. This is "
            "the early-tremor buy window: implied volatility is likely still "
            "cheap. Draft advisory ideas below — nothing has been placed, "
            "review and execute manually in IBKR if you agree."
        )
    elif window == "too_late":
        lines.append(
            "🔴 TOO LATE FOR CHEAP PREMIUM — signal is RED. If you don't "
            "already hold insurance, options here are pricing in fear, not "
            "calm. Focus below shifts to reviewing any open hedges rather "
            "than opening fresh ones."
        )
    lines.append("")

    if window == "act":
        lines.append("--- Draft trade ideas (not executed) ---")
        lines.append("")
        lines.append("Shorts / insurance:")
        for s in ideas["shorts"]:
            lines.append(f"  • {s['instrument']}")
            lines.append(f"      {s['rationale']}")
        lines.append("")
        lines.append(f"  Note: {ideas['shorts_note']}")
        lines.append("")
        lines.append("Longs (quality survivors from Glint):")
        if ideas["longs"]:
            for l in ideas["longs"]:
                lines.append(f"  • {l['ticker']}  (price {l['price']}, Glint score {l['value_score']}/3)")
                lines.append(f"      {l['rationale']}")
        else:
            lines.append(f"  {ideas.get('longs_note', 'None today.')}")
        lines.append("")
        lines.append(
            "⚠️  These are sketches, not orders. Verify actual strikes, "
            "expirations, and liquidity in IBKR before placing anything — "
            "same caveat as Undertow's own trade ideas."
        )
        lines.append("")

    if open_positions:
        lines.append("--- Open advisory positions Ark is tracking ---")
        for i, p in enumerate(open_positions, 1):
            lines.append(f"  {i}. Opened {p['opened']}  ({p['age_days']} days ago)  status: {p['status']}")
            if p.get("note"):
                lines.append(f"     {p['note']}")
        lines.append("")

    if dry_run:
        lines.append("(dry run — no state file was written)")

    lines.append("=" * 64)
    return "\n".join(lines)


def save_report(report_text, today_date_str):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"ark_report_{today_date_str}.txt"
    path.write_text(report_text)
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def send_ark_email(report, window, date_str):
    """Email the daily report via Resend. Never crashes the run if sending fails."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        print("RESEND_API_KEY not set - skipping email.")
        return
    subject = "Ark - " + str(window).upper() + " - " + date_str
    payload = {
        "from": "Ark <onboarding@resend.dev>",
        "to": ["micahbrown4@me.com"],
        "subject": subject,
        "text": report,
    }
    try:
        import requests
        resp = requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": "Bearer " + api_key},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            print("Email sent to micahbrown4@me.com (status " + str(resp.status_code) + ").")
        else:
            print("Email send FAILED (status " + str(resp.status_code) + "): " + resp.text[:200])
    except Exception as e:
        print("Email send FAILED: " + str(e))


def main():
    parser = argparse.ArgumentParser(description="Ark — advisory-only crash-insurance signal generator")
    parser.add_argument("--dry-run", action="store_true", help="Run without saving state (safe to test with)")
    args = parser.parse_args()

    today_data = fetch_ark_inputs()
    today_date_str = today_data.get("generated_at", datetime.now(timezone.utc).isoformat())[:10]

    state = load_state()
    window, prev_score = classify_window(today_data, state["history"])

    quality_survivors = pick_quality_survivors(today_data.get("glint_candidates", []))
    ideas = build_trade_ideas(today_data, quality_survivors) if window == "act" else {}

    state = refresh_open_positions(state, today_date_str, window)

    if window == "act":
        state = record_new_advisory(state, today_date_str, ideas)

    new_entry = {
        "date": today_date_str,
        "score": today_data["composite"]["score"],
        "signal": today_data["composite"]["signal"],
    }
    if state["history"] and state["history"][-1]["date"] == today_date_str:
        # Already ran today — update rather than duplicate.
        state["history"][-1] = new_entry
    else:
        state["history"].append(new_entry)
    # Keep history from growing forever — 120 days is plenty for trend checks.
    state["history"] = state["history"][-120:]

    report = render_report(today_data, window, prev_score, ideas, state, args.dry_run)
    print(report)

    if not args.dry_run:
        save_state(state)
        report_path = save_report(report, today_date_str)
        print(f"\nSaved report to {report_path}")
        state_dest = f"Gist {ARK_STATE_GIST_ID}" if ARK_STATE_GIST_ID else str(STATE_PATH)
        print(f"Saved state to {state_dest}")
        send_ark_email(report, window, today_date_str)


if __name__ == "__main__":
    main()
