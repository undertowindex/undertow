import os
import sys
import json
import math
import time
import datetime
import requests
import yfinance as yf

from glint_bridge import run_glint, build_glint_section
from boardroom_research import (
    should_run_full_research, run_member_research, run_full_boardroom,
    run_glint_review, log_run,
)

try:
    from undertow_new_indicators import (
        fetch_breadth_indicator, fetch_vix_term_structure,
        fetch_qqq_put_call_ratio, calculate_shadow_score
    )
    SHADOW_INDICATORS_AVAILABLE = True
except ImportError:
    SHADOW_INDICATORS_AVAILABLE = False
    print("⚠️  Shadow indicators module not available", flush=True)

# ─────────────────────────────────────────────
def yf_download_with_retry(tickers, retries=3, backoff_seconds=3, **kwargs):
    """yf.download wrapper with retries. Without this, a transient fetch
    error (rate limit, cache lock, network blip) makes a layer return its
    all-clear default score instead of erroring loudly - the most
    dangerous failure mode for a risk-alert system, since it looks
    identical to genuinely calm markets."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return yf.download(tickers, progress=False, **kwargs)
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
    raise last_error

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
RESEND_API_KEY = os.environ.get("SENDGRID_API_KEY", "").strip()
# Comma-separated. Temporarily just the account owner - Resend's shared
# "onboarding@resend.dev" testing address can only send to the account's
# own verified email (confirmed 2026-08-26, was silently/loudly failing
# every run since the other 3 addresses were added). Add the others back
# once a real domain is verified at resend.com/domains.
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "micahbrown4@me.com").strip()
ALERT_EMAILS = [e.strip() for e in ALERT_EMAIL.split(",") if e.strip()]
# Ark handoff: lets Ark (running locally, not on Railway) pull today's real
# signal automatically instead of Micah retyping it from the email. Micah
# explicitly approved publishing this to a secret (unlisted) GitHub Gist as
# the cross-machine bridge - see publish_ark_handoff() below.
GITHUB_GIST_TOKEN = os.environ.get("GITHUB_GIST_TOKEN", "").strip()
ARK_HANDOFF_GIST_ID = os.environ.get("ARK_HANDOFF_GIST_ID", "")

# LAYER 8 SOFR THRESHOLDS (dynamic, calibrated to current Fed target)
# Set FED_TARGET_RATE in environment to current Fed funds target midpoint (e.g., "4.25")
# Thresholds calculated as: Fed_Target + spread
# CAUTION: Fed_Target + 1.20pp (e.g., 5.45% if Fed target is 4.25%)
# WARNING: Fed_Target + 1.75pp (e.g., 6.00% if Fed target is 4.25%)
try:
    FED_TARGET_RATE = float(os.environ.get("FED_TARGET_RATE", "4.25").strip())
except ValueError:
    FED_TARGET_RATE = 4.25  # Fallback to current approximate target
    print("⚠️  FED_TARGET_RATE invalid, defaulting to 4.25%", flush=True)

SOFR_CAUTION_THRESHOLD = FED_TARGET_RATE + 1.20
SOFR_WARNING_THRESHOLD = FED_TARGET_RATE + 1.75

# ─────────────────────────────────────────────
def publish_ark_handoff(ark_inputs, gist_id, token):
    """Publishes ark_inputs to a secret GitHub Gist so Ark, running locally
    rather than on Railway, can fetch today's real signal automatically
    instead of it being typed in by hand. Gists are 'secret' (unlisted,
    readable by anyone with the exact ID) rather than access-controlled -
    acceptable here since the payload is a market signal plus already-public
    tickers, not account data. Returns the gist ID (existing or newly
    created) on success, or None on failure - callers must treat a failed
    publish as "handoff did not happen," never assume it silently worked."""
    if not token:
        print("  ⚠️  GITHUB_GIST_TOKEN not set - skipping Ark handoff publish.", flush=True)
        return None

    payload = {**ark_inputs, "generated_at": datetime.datetime.utcnow().isoformat() + "Z"}
    body = {
        "description": "Undertow -> Ark signal handoff (auto-updated daily, do not edit by hand)",
        "public": False,
        "files": {"ark_inputs.json": {"content": json.dumps(payload, indent=2)}},
    }
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

    try:
        if gist_id:
            r = requests.patch(f"https://api.github.com/gists/{gist_id}", json=body, headers=headers, timeout=15)
        else:
            r = requests.post("https://api.github.com/gists", json=body, headers=headers, timeout=15)
        r.raise_for_status()
        new_id = r.json()["id"]
        if not gist_id:
            print(f"  🔑 Created new Ark handoff gist: {new_id}", flush=True)
            print(f"  🔑 Set ARK_HANDOFF_GIST_ID={new_id} in Railway (and in Ark's local env) "
                  f"so future runs update this same gist and Ark can find it.", flush=True)
        return new_id
    except Exception as e:
        print(f"  ⚠️  Failed to publish Ark handoff: {e}", flush=True)
        return None

# ─────────────────────────────────────────────
def fred_get(series_id, retries=4, backoff_seconds=3):
    """Shared FRED API fetch with retry logic. Skips missing ('.') values
    and returns the most recent real observation as (value, date_str).
    Returns (None, None) if the series has no valid data or every attempt
    fails. Callers should check the date against today's date - FRED will
    happily return a real, valid-looking number that is nonetheless old if
    a series has stopped being updated upstream."""
    import time
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series_id, "api_key": FRED_API_KEY,
              "file_type": "json", "sort_order": "desc", "limit": 5}

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            for o in r.json()["observations"]:
                if o["value"] != ".":
                    return float(o["value"]), o["date"]
            return None, None
        except Exception as e:
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
            else:
                print(f"  ⚠️  FRED fetch failed for {series_id} after {retries} attempts: {e}", flush=True)
                return None, None

# ─────────────────────────────────────────────
# LAYER 1: EQUITY PULSE
# ─────────────────────────────────────────────
def get_layer1():
    try:
        tickers = yf_download_with_retry("SPY RSP QQQ ^VIX ^VVIX NVDA", period="5d", interval="1d")
        close = tickers["Close"]

        spy = float(close["SPY"].dropna().iloc[-1])
        spy_prev = float(close["SPY"].dropna().iloc[-2])
        rsp = float(close["RSP"].dropna().iloc[-1])
        rsp_prev = float(close["RSP"].dropna().iloc[-2])
        nvda = float(close["NVDA"].dropna().iloc[-1])
        nvda_prev = float(close["NVDA"].dropna().iloc[-2])
        vix = float(close["^VIX"].dropna().iloc[-1])
        vvix = float(close["^VVIX"].dropna().iloc[-1])
        last_bar_date = close["SPY"].dropna().index[-1].strftime("%Y-%m-%d")

        spy_chg = (spy - spy_prev) / spy_prev * 100
        rsp_chg = (rsp - rsp_prev) / rsp_prev * 100
        nvda_chg = (nvda - nvda_prev) / nvda_prev * 100

        rsp_spy_ratio = rsp / spy
        rsp_spy_ratio_prev = rsp_prev / spy_prev
        ratio_chg = (rsp_spy_ratio - rsp_spy_ratio_prev) / rsp_spy_ratio_prev * 100

        score = 0
        flags = []

        if vix > 25:
            score += 2
            flags.append(f"VIX elevated at {vix:.1f}")
        elif vix > 18:
            score += 1
            flags.append(f"VIX creeping at {vix:.1f}")

        if vvix > 120:
            score += 2
            flags.append(f"VVIX extreme at {vvix:.1f} — fear is chaotic/disorderly")
        elif vvix > 100:
            score += 1
            flags.append(f"VVIX elevated at {vvix:.1f} — instability building")
        elif vvix < 70:
            score += 1
            flags.append(f"VVIX very low at {vvix:.1f} — dangerous complacency")

        if ratio_chg < -0.5:
            score += 2
            flags.append(f"RSP/SPY ratio falling {ratio_chg:.2f}% — rally narrowing (bad sign)")
        elif ratio_chg < 0:
            score += 1
            flags.append(f"RSP/SPY ratio slightly down {ratio_chg:.2f}%")

        if nvda_chg < -3:
            score += 1
            flags.append(f"NVDA down {nvda_chg:.1f}% — AI sentiment weakening")

        return {
            "score": score,
            "max": 7,
            "flags": flags,
            "data": {
                "SPY": round(spy, 2), "SPY_chg": round(spy_chg, 2),
                "RSP": round(rsp, 2), "RSP_chg": round(rsp_chg, 2),
                "NVDA": round(nvda, 2), "NVDA_chg": round(nvda_chg, 2),
                "VIX": round(vix, 2), "VVIX": round(vvix, 2),
                "RSP_SPY_ratio": round(rsp_spy_ratio, 4),
                "RSP_SPY_ratio_chg": round(ratio_chg, 2),
                "last_bar_date": last_bar_date
            }
        }
    except Exception as e:
        return {"score": 0, "max": 5, "flags": [f"Layer 1 error: {e}"], "data": {}}

# ─────────────────────────────────────────────
# LAYER 2: CREDIT & YIELD CURVE
# ─────────────────────────────────────────────
def get_layer2():
    score = 0
    flags = []
    data = {}

    try:
        t10, t10_date = fred_get("DGS10")
        t2, t2_date = fred_get("DGS2")
        t30, t30_date = fred_get("DGS30")
        spread = t10 - t2
        data["yield_curve_spread"] = round(spread, 3)
        data["yield_curve_date"] = min(t10_date, t2_date)
        data["t10_yield"] = round(t10, 3) if t10 else None
        data["t30_yield"] = round(t30, 3) if t30 else None

        if spread < 0:
            score += 2
            flags.append(f"Yield curve inverted: 10Y-2Y = {spread:.3f}% (recession signal)")
        elif spread < 0.3:
            score += 1
            flags.append(f"Yield curve flat: 10Y-2Y = {spread:.3f}%")

        # Monitor absolute yield levels - elevated yields signal bond selling/rate risk
        if t10 is not None:
            if t10 > 5.0:
                score += 2
                flags.append(f"10Y Treasury yield elevated at {t10:.2f}% — bond selling pressure")
            elif t10 > 4.5:
                score += 1
                flags.append(f"10Y Treasury yield rising ({t10:.2f}%)")

        if t30 is not None:
            if t30 > 5.2:
                score += 2
                flags.append(f"30Y Treasury yield critically high at {t30:.2f}% — long-end stress")
            elif t30 > 4.8:
                score += 1
                flags.append(f"30Y Treasury yield elevated ({t30:.2f}%)")

        hy, hy_date = fred_get("BAMLH0A0HYM2")
        data["hy_spread"] = round(hy, 3)
        data["hy_spread_date"] = hy_date

        if hy > 5.0:
            score += 2
            flags.append(f"HY credit spreads wide at {hy:.2f}% — stress building")
        elif hy > 3.5:
            score += 1
            flags.append(f"HY spreads elevated at {hy:.2f}%")

        # MOVE Index: Bond market volatility (parallel to VIX for bonds)
        # CRITICAL for understanding bond market stress given elevated Treasury yields
        try:
            move_data = yf_download_with_retry("^MOVE", period="1d", interval="1d")
            if not move_data.empty:
                move = float(move_data["Close"].iloc[-1])
                data["move_index"] = round(move, 2)
                if move > 150:
                    score += 2
                    flags.append(f"MOVE Index extreme at {move:.1f} — bond market panic")
                elif move > 120:
                    score += 2
                    flags.append(f"MOVE Index elevated at {move:.1f} — bond volatility spike")
                elif move > 100:
                    score += 1
                    flags.append(f"MOVE Index rising at {move:.1f} — bond market stress")
            else:
                data["move_index"] = "UNAVAILABLE"
        except Exception as e:
            # Log the fetch failure but don't fail Layer 2
            data["move_index"] = f"FETCH_ERROR: {type(e).__name__}"
            print(f"  ⚠️  MOVE Index fetch failed: {e}", flush=True)

        # Cross-asset divergence: Bonds selling into low VIX = hidden stress
        try:
            vix_data = yf_download_with_retry("^VIX", period="1d", interval="1d")
            if not vix_data.empty:
                vix = float(vix_data["Close"].iloc[-1])
                data["vix_level"] = round(vix, 2)
                # Flag when bonds are selling (10Y > 4.5%) but equity vol is complacent (VIX < 15)
                if t10 is not None and t10 > 4.5 and vix < 15:
                    score += 2
                    flags.append(f"⚡ DIVERGENCE: Bonds selling ({t10:.2f}%) into low VIX ({vix:.1f}) — hidden stress")
        except Exception as e:
            pass  # VIX divergence check is optional, don't fail Layer 2 on it

    except Exception as e:
        flags.append(f"Layer 2 error: {e}")

    return {"score": score, "max": 8, "flags": flags, "data": data}

# ─────────────────────────────────────────────
# LAYER 3: MACRO TREMORS
# ─────────────────────────────────────────────
def get_layer3():
    score = 0
    flags = []
    data = {}

    try:
        tickers = yf_download_with_retry("JPY=X GC=F HG=F UUP", period="4mo", interval="1d")
        close = tickers["Close"]

        yen = float(close["JPY=X"].dropna().iloc[-1])
        yen_prev = float(close["JPY=X"].dropna().iloc[-2])
        yen_chg = (yen - yen_prev) / yen_prev * 100

        gold = float(close["GC=F"].dropna().iloc[-1])
        copper = float(close["HG=F"].dropna().iloc[-1])
        copper_gold = copper / gold
        last_bar_date = close["JPY=X"].dropna().index[-1].strftime("%Y-%m-%d")
        data["yen"] = round(yen, 4)
        data["last_bar_date"] = last_bar_date
        data["yen_chg"] = round(yen_chg, 3)
        data["copper_gold_ratio"] = round(copper_gold, 6)

        if yen_chg > 0.5:
            score += 2
            flags.append(f"Yen surging {yen_chg:.2f}% — carry trade unwinding risk")
        elif yen_chg > 0.2:
            score += 1
            flags.append(f"Yen strengthening {yen_chg:.2f}%")

        cu_gold_threshold = 0.00018
        if copper_gold < cu_gold_threshold * 0.95:
            score += 2
            flags.append(f"Copper/gold ratio low at {copper_gold:.6f} — growth fears")
        elif copper_gold < cu_gold_threshold:
            score += 1
            flags.append(f"Copper/gold ratio softening at {copper_gold:.6f}")

        dollar = float(close["UUP"].dropna().iloc[-1])
        dollar_ma50 = float(close["UUP"].dropna().tail(50).mean())
        dollar_pct_above_ma = (dollar - dollar_ma50) / dollar_ma50 * 100
        data["dollar_proxy"] = round(dollar, 3)
        data["dollar_ma50"] = round(dollar_ma50, 3)
        data["dollar_pct_above_ma"] = round(dollar_pct_above_ma, 2)

        if dollar_pct_above_ma > 3:
            score += 2
            flags.append(f"Dollar strongly above its 50-day average ({dollar_pct_above_ma:.1f}%) - possible flight-to-safety demand")
        elif dollar_pct_above_ma > 1.5:
            score += 1
            flags.append(f"Dollar above its 50-day average ({dollar_pct_above_ma:.1f}%)")

        # Institutional gold positioning (flight-to-safety indicator)
        try:
            cot_gold_url = "https://yw9f-hn96.json.ckan.io/api/3/action/datastore_search_sql?sql=SELECT%20*%20FROM%20%22yw9f-hn96%22%20WHERE%20%22commodity%22%3D%27Gold%27%20ORDER%20BY%20%22report_date_as_yyyy_mm_dd%22%20DESC%20LIMIT%202"
            resp = requests.get(cot_gold_url, timeout=10)
            cot_gold_data = resp.json().get("result", {}).get("records", [])

            if cot_gold_data:
                report_date_str = cot_gold_data[0].get("report_date_as_yyyy_mm_dd", "")[:10]
                try:
                    report_date = datetime.datetime.strptime(report_date_str, "%Y-%m-%d")
                    days_old = (datetime.datetime.now() - report_date).days

                    if days_old <= 8:
                        # Data is fresh — use it
                        long_pos = float(cot_gold_data[0].get("lev_money_positions_long", 0))
                        short_pos = float(cot_gold_data[0].get("lev_money_positions_short", 0))
                        net_pos = long_pos - short_pos
                        data["gold_cot_long"] = long_pos
                        data["gold_cot_short"] = short_pos
                        data["gold_cot_net"] = net_pos
                        data["gold_cot_report_date"] = report_date_str

                        # Compare to prior week if available
                        prior_net = 0
                        if len(cot_gold_data) > 1:
                            prior_long = float(cot_gold_data[1].get("lev_money_positions_long", 0))
                            prior_short = float(cot_gold_data[1].get("lev_money_positions_short", 0))
                            prior_net = prior_long - prior_short

                        net_change = net_pos - prior_net

                        # Heavy institutional gold accumulation = flight-to-safety signal
                        if net_pos > 200000:  # Threshold for "heavy" positioning
                            score += 2
                            flags.append(f"🏆 Heavy institutional gold longs ({net_pos:,.0f}) — flight-to-safety accumulation [week of {report_date_str}]")
                        elif net_change > 50000:  # Large weekly increase
                            score += 1
                            flags.append(f"Institutions adding gold longs (+{net_change:,.0f} this week) — caution building [week of {report_date_str}]")
                except (ValueError, TypeError):
                    pass  # Date parsing failed, skip gold positioning
        except Exception as e:
            pass  # Gold COT fetch is optional, don't fail Layer 3 on it

    except Exception as e:
        flags.append(f"Layer 3 error: {e}")

    return {"score": score, "max": 6, "flags": flags, "data": data}

# ─────────────────────────────────────────────
# LAYER 4: COMPOSITE SCORE
# ─────────────────────────────────────────────
# ──────────────────────────────────────────────
# LAYER 3b: COT POSITIONING & REPO STRESS
# ──────────────────────────────────────────────
def get_layer3b():
    score = 0
    flags = []
    data = {}

    try:
        # yw9f-hn96 is CFTC's "Traders in Financial Futures" report, which
        # actually has lev_money_positions_long/short. The old resource id
        # (jun7-fc8e) was the Legacy report - it lacks those fields
        # entirely, so .get(..., 0) silently defaulted to 0 every day. The
        # filter is now an exact match so it can't also match "MICRO
        # E-MINI S&P 500", a different, much smaller retail contract.
        cot_url = "https://publicreporting.cftc.gov/resource/yw9f-hn96.json"
        cot_params = {
            "$where": "contract_market_name = 'E-MINI S&P 500'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 1
        }
        cot_resp = requests.get(cot_url, params=cot_params, timeout=10)
        cot_data = cot_resp.json()

        if cot_data:
            report_date_str = cot_data[0].get("report_date_as_yyyy_mm_dd", "")[:10]
            # FRESHNESS GATE: Only use COT data if it's current week (≤10 days old)
            # COT publishes every Friday 15:30 EST for positions as of Tuesday. Allow up to 10 days to account for
            # Socrata API indexing lag. Ideally we'd fetch directly from CFTC or CME, but this covers the current feed.
            try:
                report_date = datetime.datetime.strptime(report_date_str, "%Y-%m-%d")
                days_old = (datetime.datetime.now() - report_date).days
                if days_old > 10:
                    # REJECT stale data — do not use it in scoring
                    flags.append(f"⚠️  COT: STALE DATA ({days_old} days old) — rejecting, awaiting fresh weekly report")
                    data["cot_report_date"] = report_date_str
                    data["cot_stale"] = True
                else:
                    # Data is fresh — use it
                    long_pos = float(cot_data[0].get("lev_money_positions_long", 0))
                    short_pos = float(cot_data[0].get("lev_money_positions_short", 0))
                    net_pos = long_pos - short_pos
                    data["cot_long"] = long_pos
                    data["cot_short"] = short_pos
                    data["cot_net"] = net_pos
                    data["cot_report_date"] = report_date_str
                    data["cot_stale"] = False
                    if net_pos < 0:
                        score += 1
                        flags.append(f"COT: leveraged funds net SHORT E-mini S&P ({net_pos:,.0f} contracts) [week of {report_date_str}]")
            except Exception as e:
                flags.append(f"COT: date error — {e}")
        else:
            flags.append("COT: no data available")
    except Exception as e:
        flags.append(f"Layer 3b COT error: {e}")

    try:
        sofr, sofr_date = fred_get("SOFR")
        dff, dff_date = fred_get("DFF")
        rrp, rrp_date = fred_get("RRPONTSYD")
        dgs3mo, dgs3mo_date = fred_get("DGS3MO")

        if sofr is not None and dff is not None:
            spread = sofr - dff
            data["sofr_dff_spread"] = round(spread, 3)
            data["sofr_dff_date"] = min(sofr_date, dff_date)
            if spread > 0.10:
                score += 2
                flags.append(f"SOFR-Fed Funds spread widening ({spread:.2f}pp) - repo stress")
            elif spread > 0.05:
                score += 1
                flags.append(f"SOFR-Fed Funds spread elevated ({spread:.2f}pp)")

        if rrp is not None:
            data["reverse_repo_bn"] = round(rrp, 1)
            data["reverse_repo_date"] = rrp_date
            if rrp > 100:
                score += 1
                flags.append(f"Reverse repo usage spiking (${rrp:.0f}B)")

        if sofr is not None and dgs3mo is not None:
            ted = dgs3mo - sofr
            data["ted_spread_equiv"] = round(ted, 3)
            data["ted_spread_date"] = min(sofr_date, dgs3mo_date)
            if ted < -0.15:
                score += 1
                flags.append(f"TED-equivalent spread inverted ({ted:.2f}pp) - funding stress")
    except Exception as e:
        flags.append(f"Layer 3b repo/TED error: {e}")

    return {"score": score, "max": 4, "flags": flags, "data": data}

def _check_freshness(warnings, label, date_str, max_age_days=5):
    """A stale-but-numerically-sane reading (e.g. a frozen archive quietly
    returning an old-but-plausible value) passes every range check and is
    still wrong. This checks the actual date behind each number, not just
    whether the number itself looks reasonable."""
    if not date_str:
        return
    try:
        date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        age_days = (datetime.datetime.now() - date).days
        if age_days > max_age_days:
            warnings.append(f"{label} STALE DATA: dated {date_str} is {age_days} days old (expected within {max_age_days}) - feed may have stopped updating")
    except ValueError:
        warnings.append(f"{label} sanity: date {date_str!r} not parseable")


def run_sanity_checks(l1, l2, l3, l3b, l3c, l3d, l3e, l8, l9=None):
    warnings = []

    vix = l1["data"].get("vix")
    if vix is not None and not (5 <= vix <= 100):
        warnings.append(f"Layer 1 sanity: VIX {vix} outside plausible range (5-100)")
    for name in ["spy", "rsp", "nvda"]:
        v = l1["data"].get(name)
        if v is not None and v <= 0:
            warnings.append(f"Layer 1 sanity: {name} price {v} is non-positive")

    yc = l2["data"].get("yield_curve_spread")
    if yc is not None and not (-5 <= yc <= 5):
        warnings.append(f"Layer 2 sanity: yield curve spread {yc} outside plausible range (-5pp to 5pp)")
    t10 = l2["data"].get("t10_yield")
    if t10 is not None and not (0 <= t10 <= 10):
        warnings.append(f"Layer 2 sanity: 10Y yield {t10} outside plausible range (0-10%)")
    t30 = l2["data"].get("t30_yield")
    if t30 is not None and not (0 <= t30 <= 10):
        warnings.append(f"Layer 2 sanity: 30Y yield {t30} outside plausible range (0-10%)")
    hy = l2["data"].get("hy_spread")
    if hy is not None and not (0 <= hy <= 20):
        warnings.append(f"Layer 2 sanity: HY spread {hy} outside plausible range (0-20%)")

    yen = l3["data"].get("yen")
    if yen is not None and not (50 <= yen <= 300):
        warnings.append(f"Layer 3 sanity: yen {yen} outside plausible range (50-300)")
    cg = l3["data"].get("copper_gold_ratio")
    if cg is not None and not (0 < cg < 1):
        warnings.append(f"Layer 3 sanity: copper/gold ratio {cg} outside plausible range (0-1)")

    dpam = l3["data"].get("dollar_pct_above_ma")
    if dpam is not None and not (-15 <= dpam <= 15):
        warnings.append(f"Layer 3 sanity: dollar % above 50-day MA {dpam} outside plausible range (-15 to 15)")

    sofr_dff = l3b["data"].get("sofr_dff_spread")
    if sofr_dff is not None and not (-2 <= sofr_dff <= 2):
        warnings.append(f"Layer 3b sanity: SOFR-DFF spread {sofr_dff} outside plausible range (-2pp to 2pp)")
    rrp = l3b["data"].get("reverse_repo_bn")
    if rrp is not None and rrp < 0:
        warnings.append(f"Layer 3b sanity: reverse repo {rrp} is negative")
    ted = l3b["data"].get("ted_spread_equiv")
    if ted is not None and not (-2 <= ted <= 2):
        warnings.append(f"Layer 3b sanity: TED-equivalent spread {ted} outside plausible range (-2pp to 2pp)")

    # Freshness checks across every layer that carries a date - daily
    # market/FRED data gets 5 days' tolerance (covers weekends plus a
    # holiday), COT gets 10 (it only reports weekly with a 2-week indexing lag).
    _check_freshness(warnings, "Layer 1", l1["data"].get("last_bar_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 2 (yield curve)", l2["data"].get("yield_curve_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 2 (HY spread)", l2["data"].get("hy_spread_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 3", l3["data"].get("last_bar_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 3b (COT)", l3b["data"].get("cot_report_date"), max_age_days=10)
    _check_freshness(warnings, "Layer 3b (SOFR-DFF)", l3b["data"].get("sofr_dff_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 3b (reverse repo)", l3b["data"].get("reverse_repo_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 3b (TED-equiv)", l3b["data"].get("ted_spread_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 3d (SKEW)", l3d["data"].get("last_bar_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 8 (SOFR)", l8["data"].get("sofr_date"), max_age_days=5)

    pcr = l3c["data"].get("put_call_ratio")
    if pcr is not None and not (0.1 <= pcr <= 5):
        warnings.append(f"Layer 3c sanity: put/call ratio {pcr} outside plausible range (0.1-5)")

    skew_val = l3d["data"].get("skew")
    if skew_val is not None and not (80 <= skew_val <= 200):
        warnings.append(f"Layer 3d sanity: SKEW {skew_val} outside plausible range (80-200)")

    gex_bn = l3e["data"].get("net_gex_bn_per_1pct")
    if gex_bn is not None and abs(gex_bn) > 100:
        warnings.append(f"Layer 3e sanity: net GEX {gex_bn}bn per 1% outside plausible range (-100 to 100)")
    _check_freshness(warnings, "Layer 3e (GEX)", l3e["data"].get("last_bar_date"), max_age_days=5)

    sofr_pct = l8["data"].get("sofr_3m_pct")
    if sofr_pct is not None and not (3.0 <= sofr_pct <= 6.0):
        warnings.append(f"Layer 8 sanity: SOFR 3M {sofr_pct:.2f}% outside plausible range (3.0-6.0%)")

    volume_b = l8["data"].get("sofr_volume_b")
    if volume_b is not None and not (2500 <= volume_b <= 4000):
        warnings.append(f"Layer 8 sanity: SOFR volume {volume_b:.0f}B outside plausible range (2,500-4,000B)")

    nfci_val = None
    if l9:
        nfci_val = l9["data"].get("nfci_value")
        if nfci_val is not None and not (-2 <= nfci_val <= 2):
            warnings.append(f"Layer 9 sanity: NFCI {nfci_val:.2f} outside plausible range (-2 to 2)")
        anfci_val = l9["data"].get("anfci_value")
        if anfci_val is not None and not (-2 <= anfci_val <= 2):
            warnings.append(f"Layer 9 sanity: ANFCI {anfci_val:.2f} outside plausible range (-2 to 2)")
        _check_freshness(warnings, "Layer 9 (NFCI)", l9["data"].get("nfci_date"), max_age_days=7)

    layer_list = [("Layer 1", l1), ("Layer 2", l2), ("Layer 3", l3), ("Layer 3b", l3b), ("Layer 3c", l3c), ("Layer 3d", l3d), ("Layer 3e", l3e), ("Layer 8", l8)]
    if l9:
        layer_list.append(("Layer 9", l9))

    for label, layer in layer_list:
        if not (0 <= layer["score"] <= layer["max"]):
            warnings.append(f"{label} sanity: score {layer['score']} outside valid range 0-{layer['max']}")
        # A layer that produced zero data almost always means its fetch hit
        # an exception and fell back to score 0 (looks calm) with the real
        # cause buried in its flags list. Score 0 from a genuinely calm
        # reading and score 0 from "the feed never returned anything" are
        # indistinguishable in the composite total unless called out here -
        # this is the exact silent-failure mode the whole self-test exists
        # to catch.
        if not layer["data"] and layer["max"] > 0:
            flag_summary = "; ".join(layer["flags"]) if layer["flags"] else "no flags recorded"
            warnings.append(f"{label} produced NO DATA (scored 0/{layer['max']}, looks calm but may be a dead feed) - {flag_summary}")
        # A PARTIAL failure (e.g. one of several FRED calls in a layer
        # succeeds, a later one throws) leaves some real data in place, so
        # the "no data" check above won't catch it. Every degraded-data
        # flag in this file (exception handlers, "no data returned", GEX's
        # insufficient/lopsided-chain guards) uses one of these words -
        # confirmed by grepping every flags.append() call in this file, not
        # guessed. Surface those as loud warnings too, not just quiet
        # entries in a flags list a human has to go read.
        _DEGRADED_FLAG_WORDS = ("error", "no data", "insufficient", "lopsided", "unavailable", "failed", "disabled", "could not")
        for flag in layer["flags"]:
            if any(w in flag.lower() for w in _DEGRADED_FLAG_WORDS):
                warnings.append(f"{label} DEGRADED DATA: {flag}")

    return warnings

# ──────────────────────────────────────────────
# SHARED: SPY OPTIONS CHAIN FETCH (feeds Layer 3c and Layer 3e)
# ──────────────────────────────────────────────
# Both the put/call ratio (3c) and dealer gamma (3e) are derived from the
# same live SPY options chain (Yahoo Finance, free, no key), so they share
# one fetch instead of hitting Yahoo twice for the same data.
def _fetch_spy_option_chain():
    tk = yf.Ticker("SPY")
    hist = tk.history(period="5d")
    spot = float(hist["Close"].dropna().iloc[-1])
    last_bar_date = hist["Close"].dropna().index[-1].strftime("%Y-%m-%d")

    today = datetime.date.today()
    # SPY has daily expirations, so "first N in the window" would only
    # ever sample the front week. Instead pick the expiration closest
    # to each of ~1wk / 2wk / 1mo / 2mo out, so the measure spans the
    # window and includes the heavyweight monthly expirations.
    window = []
    for exp in tk.options:
        dte = (datetime.datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        if 5 <= dte <= 60:
            window.append((exp, dte))

    def _is_third_friday(exp_str):
        d = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
        return d.weekday() == 4 and 15 <= d.day <= 21

    # Force-include monthly OPEX expirations (third Fridays) - that is
    # where the heavyweight dealer books sit - then add a front-week
    # and a couple of spread-out picks around them.
    expirations = [x for x in window if _is_third_friday(x[0])]
    for target in (7, 14, 45):
        if not window:
            break
        pick = min(window, key=lambda x: abs(x[1] - target))
        if pick not in expirations:
            expirations.append(pick)
    expirations.sort(key=lambda x: x[1])
    expirations = expirations[:6]

    contracts = []
    for exp, dte in expirations:
        chain = tk.option_chain(exp)
        for df, sign in ((chain.calls, 1), (chain.puts, -1)):
            for strike, iv, oi, vol in zip(df["strike"], df["impliedVolatility"], df["openInterest"], df["volume"]):
                try:
                    strike, iv = float(strike), float(iv)
                    oi = float(oi) if oi == oi else 0.0  # NaN-safe
                    vol = float(vol) if vol == vol else 0.0
                except (TypeError, ValueError):
                    continue
                contracts.append({"sign": sign, "strike": strike, "iv": iv, "oi": oi, "volume": vol, "dte": dte})

    return {"spot": spot, "last_bar_date": last_bar_date, "expirations": expirations, "contracts": contracts}


# ──────────────────────────────────────────────
# LAYER 3c: OPTIONS SENTIMENT (PUT/CALL RATIO)
# ──────────────────────────────────────────────
# Was DISABLED 2026-07-30 to 2026-08-24: CBOE's public CDN archive for this
# data is a frozen historical dump, not a live feed - verified it stops at
# 2012 (and an alternate CBOE archive URL stops at 2019). Re-enabled by
# deriving the ratio from the same live SPY options chain already fetched
# for Layer 3e, instead of a paid CBOE feed. This is a SPY-specific proxy
# for options sentiment, not CBOE's broad total-market put/call ratio, so
# its day-to-day numbers won't match the old CBOE series 1:1 - but it is
# genuinely live. Thresholds are carried over from the original CBOE-based
# version of this layer as a starting point; worth revisiting once this
# proxy has a few weeks of its own history to calibrate against.
def get_layer3c(chain=None):
    score = 0
    flags = []
    data = {}

    try:
        if chain is None:
            chain = _fetch_spy_option_chain()

        put_volume = sum(c["volume"] for c in chain["contracts"] if c["sign"] == -1 and c["volume"] > 0)
        call_volume = sum(c["volume"] for c in chain["contracts"] if c["sign"] == 1 and c["volume"] > 0)
        put_oi = sum(c["oi"] for c in chain["contracts"] if c["sign"] == -1 and c["oi"] > 0)
        call_oi = sum(c["oi"] for c in chain["contracts"] if c["sign"] == 1 and c["oi"] > 0)

        if call_volume <= 0:
            flags.append("Layer 3c put/call ratio: no usable call volume in today's SPY chain - scoring 0")
            return {"score": 0, "max": 2, "flags": flags, "data": data}

        pc_ratio = put_volume / call_volume
        data["put_call_ratio"] = round(pc_ratio, 3)
        data["put_call_source"] = "SPY options chain volume (Yahoo Finance, live) - proxy for the old CBOE index put/call ratio"
        data["put_call_date"] = chain["last_bar_date"]
        if call_oi > 0:
            data["put_call_oi_ratio"] = round(put_oi / call_oi, 3)

        if pc_ratio > 1.2:
            score += 2
            flags.append(f"Put/call ratio elevated at {pc_ratio:.2f} - heavy put buying, fear positioning")
        elif pc_ratio > 1.0:
            score += 1
            flags.append(f"Put/call ratio above parity at {pc_ratio:.2f} - moderate hedging demand")
        elif pc_ratio < 0.5:
            score += 1
            flags.append(f"Put/call ratio very low at {pc_ratio:.2f} - complacency risk")
    except Exception as e:
        flags.append(f"Layer 3c put/call ratio error: {e}")

    return {"score": score, "max": 2, "flags": flags, "data": data}


# ──────────────────────────────────────────────
# LAYER 3d: SKEW INDEX (TAIL-RISK PRICING)
# ──────────────────────────────────────────────
def get_layer3d():
    score = 0
    flags = []
    data = {}

    try:
        tickers = yf_download_with_retry("^SKEW", period="5d", interval="1d")
        close = tickers["Close"]
        skew = float(close["^SKEW"].dropna().iloc[-1])
        data["skew"] = round(skew, 2)
        data["last_bar_date"] = close["^SKEW"].dropna().index[-1].strftime("%Y-%m-%d")

        if skew > 150:
            score += 2
            flags.append(f"SKEW elevated at {skew:.1f} - crash-tail protection pricing rising")
        elif skew > 135:
            score += 1
            flags.append(f"SKEW moderately elevated at {skew:.1f}")
    except Exception as e:
        flags.append(f"Layer 3d SKEW error: {e}")

    return {"score": score, "max": 2, "flags": flags, "data": data}


# ─────────────────────────────────────────────
# LAYER 3e: DEALER GAMMA EXPOSURE (GEX)
# ─────────────────────────────────────────────
# Computed from the SPY options chain (yfinance, free, no key) rather than
# a paid GEX feed. Standard dealer-positioning convention: dealers are
# long calls, short puts. Positive net GEX = dealer hedging suppresses
# volatility (shock absorber). Negative net GEX = the same hedging
# amplifies moves (petrol on the fire) - a structural-fragility tremor.
def _bs_gamma(spot, strike, iv, t_years, r=0.04):
    """Black-Scholes gamma (identical for calls and puts)."""
    if iv <= 0 or t_years <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return pdf / (spot * iv * math.sqrt(t_years))


def get_layer3e(chain=None):
    score = 0
    flags = []
    data = {}

    try:
        if chain is None:
            chain = _fetch_spy_option_chain()
        spot = chain["spot"]
        data["spy_spot"] = round(spot, 2)
        data["last_bar_date"] = chain["last_bar_date"]

        net_gex = 0.0   # $ change in dealer delta-hedge demand per 1% SPY move
        gross_gex = 0.0
        contracts_used = 0
        side_counts = {1: 0, -1: 0}  # calls / puts actually contributing
        for c in chain["contracts"]:
            oi, iv = c["oi"], c["iv"]
            if not (oi > 0 and 0.01 < iv < 5):
                continue
            t_years = c["dte"] / 365.0
            gamma = _bs_gamma(spot, c["strike"], iv, t_years)
            dollar_gamma = gamma * oi * 100 * spot * spot * 0.01
            net_gex += c["sign"] * dollar_gamma
            gross_gex += abs(dollar_gamma)
            contracts_used += 1
            side_counts[c["sign"]] += 1

        if contracts_used < 50 or gross_gex <= 0:
            flags.append(f"Layer 3e GEX: insufficient usable options data ({contracts_used} contracts) - scoring 0 today")
            return {"score": 0, "max": 2, "flags": flags, "data": data}

        normalized = net_gex / gross_gex  # -1 (all-put) .. +1 (all-call)
        data["net_gex_bn_per_1pct"] = round(net_gex / 1e9, 2)
        data["gex_normalized"] = round(normalized, 3)
        data["gex_contracts_used"] = contracts_used
        data["gex_calls_used"] = side_counts[1]
        data["gex_puts_used"] = side_counts[-1]
        data["gex_expirations"] = [e for e, _ in chain["expirations"]]

        # Data-quality guard: if one whole side of the market has gone
        # dark (bad Yahoo IV/OI fields), the net number is not a real
        # positioning measure - keep the data visible but score 0.
        lopsided = min(side_counts.values()) < 0.2 * max(side_counts.values())
        if lopsided:
            flags.append(f"Layer 3e GEX data quality: lopsided chain data (calls {side_counts[1]} / puts {side_counts[-1]} usable) - net GEX unreliable today, scoring 0")
            return {"score": 0, "max": 2, "flags": flags, "data": data}

        if net_gex <= 0 and normalized < -0.10:
            score += 2
            flags.append(f"Dealer gamma DEEPLY NEGATIVE ({net_gex/1e9:.1f}bn per 1% move) - dealer hedging is amplifying market moves")
        elif net_gex <= 0:
            score += 1
            flags.append(f"Dealer gamma negative ({net_gex/1e9:.1f}bn per 1% move) - volatility-amplifying regime")
    except Exception as e:
        flags.append(f"Layer 3e GEX error: {e}")

    return {"score": score, "max": 2, "flags": flags, "data": data}

def compute_score(l1, l2, l3, l3b, l3c, l3d, l3e, l8, l9=None):
    # 2026-09-27: Added Layer 8 SOFR funding stress (+5 max). Max = 36.
    # 2026-10-01: Added Layer 9 NFCI/ANFCI systemic risk (+5 max). Max = 41.
    # Scaled thresholds from old (max=31): 10/31*36 ≈ 12, 19/31*36 ≈ 22.
    # New thresholds with L9: 12*41/36 ≈ 14, 22*41/36 ≈ 25.
    l9_score = l9["score"] if l9 else 0
    total = l1["score"] + l2["score"] + l3["score"] + l3b["score"] + l3c["score"] + l3d["score"] + l3e["score"] + l8["score"] + l9_score

    max_score = 41 if l9 else 36

    if total <= 14:
        signal = "GREEN"
        emoji = "🟢"
        summary = "Markets calm. No significant stress signals detected."
    elif total <= 25:
        signal = "AMBER"
        emoji = "🟡"
        summary = "Elevated risk. Multiple stress signals present. Watch closely."
    else:
        signal = "RED"
        emoji = "🔴"
        summary = "High alert. Significant macro stress across multiple indicators."

    return {"score": total, "max": max_score, "signal": signal, "emoji": emoji, "summary": summary}

# ─────────────────────────────────────────────
# LAYER 5: THE BOARDROOM
# ─────────────────────────────────────────────
def call_anthropic_text(payload, timeout, label):
    """POST to the Anthropic messages API and return the joined text blocks.
    Raises on an error response or an empty body - both used to come back
    as an empty string, which rendered as a blank email section with no
    tally and no visible failure (the same silent-failure mode as a layer
    defaulting to calm). Retries once on transient statuses."""
    last_error = None
    for attempt in range(2):
        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json=payload,
                timeout=timeout,
            )
        except requests.exceptions.RequestException as e:
            last_error = f"request failed: {e}"
            time.sleep(5)
            continue
        if response.status_code in (429, 500, 502, 503, 529) and attempt == 0:
            last_error = f"HTTP {response.status_code}"
            time.sleep(10)
            continue
        data = response.json()
        if not response.ok or data.get("type") == "error":
            err = data.get("error", {})
            raise RuntimeError(
                f"API error (HTTP {response.status_code}): "
                f"{err.get('type', 'unknown')} - {err.get('message', json.dumps(data)[:200])}"
            )
        usage = data.get("usage", {})
        print(f"[{label}] Tokens used: input={usage.get('input_tokens', 0)}, output={usage.get('output_tokens', 0)}")
        text = "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
        if not text.strip():
            raise RuntimeError("API returned HTTP 200 but no text content")
        return text
    raise RuntimeError(f"API unreachable after retries ({last_error})")


def run_boardroom(score_data, l1, l2, l3, l8):
    if not ANTHROPIC_API_KEY:
        return "Boardroom unavailable — no API key."

    all_flags = l1["flags"] + l2["flags"] + l3["flags"] + l8["flags"]
    flags_text = "\n".join(all_flags) if all_flags else "No flags raised."
    raw_data = {**l1.get("data", {}), **l2.get("data", {}), **l3.get("data", {}), **l8.get("data", {})}
    data_text = json.dumps(raw_data, indent=2)

    board_today = datetime.date.today().strftime("%A %d %B %Y")

    prompt = f"""You are running The Boardroom — a council of the world's greatest investors and traders. Today's date is {board_today}. Your training data may end before this date - trust this date and, where relevant, your web search results for current events.

Current Undertow Index reading:
- Score: {score_data['score']}/{score_data['max']}
- Signal: {score_data['signal']}
- Summary: {score_data['summary']}

Live market data:
{data_text}

Active stress flags:
{flags_text}

The council members are:

LIVING MASTERS:
1. Warren Buffett — long-term value, fear/greed cycles
2. Michael Burry — contrarian, hidden systemic risk
3. Ray Dalio — macro cycles, debt dynamics
4. Stanley Druckenmiller — macro momentum, asymmetric bets
5. Howard Marks — risk assessment, market psychology
6. Paul Tudor Jones — technical macro, crisis anticipation
7. Jeffrey Gundlach — fixed income, macro flows
8. David Tepper — buying panics, aggressive risk-on at extremes
9. Nassim Taleb — tail risk, fragility, black swans
10. Peter Lynch — bottom-up stock picking, stay-invested optimism
11. George Soros — reflexivity, currency macro bets
12. Jim Simons — quantitative pattern detection

HISTORICAL GHOSTS:
13. Jesse Livermore — tape reading, market psychology
14. Benjamin Graham — margin of safety, intrinsic value
15. Sir John Templeton — contrarian global value
16. Charlie Munger — mental models, concentrated bets
17. André Kostolany — European macro, sentiment cycles

Each member should give:
- A 1-2 sentence view in their authentic voice
- A vote line formatted exactly like this example: "Vote: 🟢 CONFIRM". Choose the colored circle emoji based on the signal level that vote implies: 🟢 GREEN, 🟡 AMBER, 🔴 RED. CONFIRM implies the same color as the current signal ({score_data['signal']}); UPGRADE implies one level more severe (GREEN→AMBER→RED); DOWNGRADE implies one level less severe (RED→AMBER→GREEN). If the current signal is already at that extreme (e.g. UPGRADE from RED, or DOWNGRADE from GREEN), use the same color as the current signal.

Then give a BOARDROOM VERDICT:
- Final consensus signal, formatted as the matching colored emoji followed by the word: 🟢 GREEN, 🟡 AMBER, or 🔴 RED
- 2-3 sentence synthesis of why
- Confidence level (Low / Medium / High)

CRITICAL: There are exactly 17 members listed above. Each member must appear exactly once - do not repeat any member's name in the panel discussion or in the vote tally, and do not invent additional members. Before writing the BOARDROOM VERDICT vote tally, re-count the panel section you just wrote: the CONFIRM + UPGRADE + DOWNGRADE vote counts MUST sum to exactly 17. Recheck this arithmetic before outputting the table.

CRITICAL - ORDERING: Write the members in STRICT sequential order, 1 through 17, exactly as numbered in the list above. Fully complete each member's entire entry (their view AND their vote) before starting the next numbered member. Do NOT interleave, interrupt, or jump ahead to a later-numbered member mid-way through an earlier one. Do NOT go back to an earlier number after moving on. Before outputting your final answer, verify the member numbers appear in ascending order with no gaps, repeats, or interruptions.

CRITICAL - MACHINE-READABLE TALLY: After everything else, on its own final line with nothing else on it, output exactly this format with the real integer counts from the panel above (no extra words, no markdown formatting on this line):
TALLY: CONFIRM=<n> UPGRADE=<n> DOWNGRADE=<n>

Format clearly with each member's name bolded."""

    try:
        return call_anthropic_text(
            {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 6500,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=150,
            label="Boardroom",
        )
    except Exception as e:
        return f"Boardroom error: {e}"


def parse_boardroom_tally(boardroom_text):
    """Pulls the machine-readable 'TALLY: CONFIRM=n UPGRADE=n DOWNGRADE=n'
    line out of the Boardroom's free text. Returns None if it's missing or
    malformed, so callers can fall back to the composite score alone
    rather than trust a bad parse."""
    import re
    match = re.search(r"TALLY:\s*CONFIRM=(\d+)\s*UPGRADE=(\d+)\s*DOWNGRADE=(\d+)", boardroom_text)
    if not match:
        return None
    confirm, upgrade, downgrade = (int(g) for g in match.groups())
    if confirm + upgrade + downgrade != 17:
        return None
    return {"confirm": confirm, "upgrade": upgrade, "downgrade": downgrade}


SIGNAL_LEVELS = ["GREEN", "AMBER", "RED"]
SIGNAL_EMOJI = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}


def apply_boardroom_override(score_data, tally):
    """The composite score alone used to be the entire headline signal,
    with the Boardroom's vote sitting underneath as commentary that could
    never move it - even a 13-4 majority to escalate had zero effect on
    what the top of the email said. This makes a real majority (9+ of 17)
    actually move the headline one level, and always says plainly whether
    an override happened, rather than silently picking one number."""
    base_signal = score_data["signal"]
    result = {
        "signal": base_signal,
        "emoji": SIGNAL_EMOJI[base_signal],
        "overridden": False,
        "override_note": None,
    }

    if tally is None:
        result["override_note"] = "Boardroom tally unavailable — showing the composite score's signal only."
        return result

    idx = SIGNAL_LEVELS.index(base_signal)
    if tally["upgrade"] >= 9 and idx < len(SIGNAL_LEVELS) - 1:
        new_signal = SIGNAL_LEVELS[idx + 1]
        result["signal"] = new_signal
        result["emoji"] = SIGNAL_EMOJI[new_signal]
        result["overridden"] = True
        result["override_note"] = (
            f"Composite score alone says {base_signal}, but the Boardroom voted "
            f"{tally['upgrade']}-{tally['confirm']} (upgrade-confirm, {tally['downgrade']} downgrade) "
            f"to escalate — today's signal is {new_signal}."
        )
    elif tally["downgrade"] >= 9 and idx > 0:
        new_signal = SIGNAL_LEVELS[idx - 1]
        result["signal"] = new_signal
        result["emoji"] = SIGNAL_EMOJI[new_signal]
        result["overridden"] = True
        result["override_note"] = (
            f"Composite score alone says {base_signal}, but the Boardroom voted "
            f"{tally['downgrade']}-{tally['confirm']} (downgrade-confirm, {tally['upgrade']} upgrade) "
            f"to de-escalate — today's signal is {new_signal}."
        )
    return result

# ─────────────────────────────────────────────
# LAYER 6: TRADE IDEAS
# ─────────────────────────────────────────────
def get_trade_ideas(score_data, l1, l2, l3, l8, effective_signal=None):
    if not ANTHROPIC_API_KEY:
        return "Trade ideas unavailable — no API key."

    # Use the Boardroom-adjusted signal if one was computed, so trade
    # ideas match whatever signal actually appears in the email headline
    # rather than the pre-Boardroom composite signal alone.
    signal = effective_signal or score_data["signal"]
    score = score_data["score"]
    all_flags = l1["flags"] + l2["flags"] + l3["flags"] + l8["flags"]
    flags_text = "\n".join(all_flags) if all_flags else "No flags."

    today_str = datetime.date.today().strftime("%A %d %B %Y")

    prompt = f"""You are Michael Burry's trading desk AI. Today's date is {today_str}. Current Undertow signal: {signal} ({score}/{score_data['max']}).

Active flags:
{flags_text}

Generate 3-5 specific, actionable trade ideas appropriate for this risk level.

For each idea include:
- Instrument (specific ticker or product)
- Direction (long/short/put/call)
- Rationale (1 sentence, Burry-style blunt)
- Risk level (Low/Medium/High)
- Time horizon

Focus on asymmetric bets — cheap options, underpriced tail risk, or obvious contrarian plays.
For GREEN: opportunistic longs, vol selling.
For AMBER: hedges, defensive rotation, small put positions.
For RED: aggressive downside plays, safe haven longs, crisis positioning.

CRITICAL - DATES: Today is {today_str}. Your training data may end before this date - trust the date given here, not your memory. Every option expiry, target date, or time horizon you mention MUST be a real, tradeable date IN THE FUTURE relative to today (typically 30-180 days out). Never suggest an expiry that has already passed. For monthly options, expiries are the third Friday of the month.

Be specific. No waffle."""

    try:
        return call_anthropic_text(
            {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 2500,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=45,
            label="Trade Ideas",
        )
    except Exception as e:
        return f"Trade ideas error: {e}"

# ─────────────────────────────────────────────
# LAYER 7: EMAIL via RESEND
# ─────────────────────────────────────────────
def generate_dials_dashboard(score_data, l1, l2, l3, l3b, l3c, l3d, l3e, l8, l9=None):
    """Generate HTML dashboard showing 8-10 indicator dials (GREEN/AMBER/RED gauges).

    Each dial shows: layer name, score/max, and colored gauge.
    Stress threshold: ≤30% green, ≤60% amber, >60% red.
    Shows "!" indicator when score=0 due to missing data (vs. actual zero stress).
    """
    layers = [
        ("Equity Pulse", l1),
        ("Credit & Yield", l2),
        ("Macro Tremors", l3),
        ("COT & Repo", l3b),
        ("Put/Call Ratio", l3c),
        ("SKEW Index", l3d),
        ("Dealer Gamma", l3e),
        ("SOFR Funding", l8),
    ]

    # Add Layer 9 if available (Chicago Fed NFCI/ANFCI)
    if l9:
        layers.append(("NFCI Systemic Risk", l9))

    dials_html = f"""
<div style="background: #0d0d0d; padding: 20px; border-radius: 8px; margin: 20px 0;">
  <h3 style="color: #f0c040; margin-top: 0; text-align: center;">📊 INDICATOR DIALS — State of the Nation</h3>

  <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; margin-bottom: 20px;">
"""

    for layer_name, layer_data in layers:
        score = layer_data["score"]
        max_score = layer_data["max"]
        stress_pct = (score / max_score * 100) if max_score > 0 else 0

        # Check if data is missing (empty data dict means failed fetch)
        has_data = bool(layer_data.get("data"))

        # Display score with "!" indicator if no data
        score_display = f"{score}!" if (score == 0 and not has_data) else str(score)

        # Determine color based on stress level
        if stress_pct <= 30:
            color = "#2ecc71"  # GREEN
            status = "CALM"
        elif stress_pct <= 60:
            color = "#f39c12"  # AMBER
            status = "CAUTION"
        else:
            color = "#e74c3c"  # RED
            status = "STRESS"

        # SVG gauge
        gauge_svg = f"""
<svg width="120" height="120" viewBox="0 0 120 120" style="margin: auto; display: block;">
  <!-- Background circle -->
  <circle cx="60" cy="60" r="50" fill="none" stroke="#333" stroke-width="8"/>

  <!-- Colored arc (progress) -->
  <circle cx="60" cy="60" r="50" fill="none" stroke="{color}" stroke-width="8"
          stroke-dasharray="{stress_pct * 3.14}" stroke-dashoffset="0"
          stroke-linecap="round" transform="rotate(-90 60 60)"/>

  <!-- Center label -->
  <text x="60" y="55" text-anchor="middle" font-size="18" font-weight="bold" fill="{color}">
    {score_display}
  </text>
  <text x="60" y="72" text-anchor="middle" font-size="12" fill="#999">
    /{max_score}
  </text>
</svg>
"""

        dials_html += f"""
    <div style="text-align: center; background: #1a1a1a; padding: 12px; border-radius: 6px; border-left: 3px solid {color};">
      {gauge_svg}
      <p style="margin: 8px 0 0 0; font-size: 13px; color: #ddd;">
        <strong>{layer_name}</strong>
      </p>
      <p style="margin: 4px 0 0 0; font-size: 11px; color: {color}; font-weight: bold;">
        {status}
      </p>
    </div>
"""

    dials_html += """
  </div>

  <div style="text-align: center; font-size: 12px; color: #888;">
    Green = calm (≤30% of max score) | Amber = caution (31–60%) | Red = stress (>60%) | ! = no data available
  </div>
</div>
"""

    return dials_html


def build_data_quality_dashboard(l1, l2, l3, l3b, l3c, l3d, l3e, l8, l9=None):
    """
    Build a data quality dashboard showing which feeds are live and which are missing.
    Returns HTML block and a data quality score (0-100, higher is better).
    """
    feeds = {
        "Layer 1 (Equity)": bool(l1.get("data")),
        "Layer 2 (Credit/Yield)": bool(l2.get("data")),
        "Layer 3 (Macro)": bool(l3.get("data")),
        "Layer 3b (COT/Repo)": bool(l3b.get("data")),
        "Layer 3c (Put/Call)": bool(l3c.get("data")),
        "Layer 3d (SKEW)": bool(l3d.get("data")),
        "Layer 3e (Dealer Gamma)": bool(l3e.get("data")),
        "Layer 8 SOFR Rate": bool(l8.get("data", {}).get("sofr_3m_pct")),
        "Layer 8 SOFR Volume": bool(l8.get("data", {}).get("sofr_volume_b")),
    }

    # Add Layer 9 if available
    if l9:
        feeds["Layer 9 NFCI"] = bool(l9.get("data", {}).get("nfci_value"))
        feeds["Layer 9 ANFCI"] = bool(l9.get("data", {}).get("anfci_value"))

    live_feeds = sum(1 for v in feeds.values() if v)
    total_feeds = len(feeds)
    quality_pct = int((live_feeds / total_feeds) * 100)

    html_rows = []
    for feed_name, is_live in feeds.items():
        status = "✅ LIVE" if is_live else "❌ MISSING"
        color = "#2d7a2d" if is_live else "#a94442"
        html_rows.append(f'<tr><td>{feed_name}</td><td style="color: {color}; font-weight: bold;">{status}</td></tr>')

    quality_color = "#2d7a2d" if quality_pct >= 80 else "#ff9800" if quality_pct >= 60 else "#a94442"
    quality_emoji = "🟢" if quality_pct >= 80 else "🟡" if quality_pct >= 60 else "🔴"

    html = f"""
<div style="margin: 20px 0; padding: 15px; background: #f9f9f9; border-left: 4px solid {quality_color}; border-radius: 4px;">
  <h3 style="margin-top: 0; color: {quality_color};">{quality_emoji} DATA QUALITY DASHBOARD</h3>
  <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
    <tr style="background: #f0f0f0;">
      <th style="text-align: left; padding: 8px; border-bottom: 1px solid #ddd;">Feed</th>
      <th style="text-align: left; padding: 8px; border-bottom: 1px solid #ddd;">Status</th>
    </tr>
    {''.join(html_rows)}
  </table>
  <p style="margin: 10px 0 0 0; font-size: 12px; color: #666;">
    <strong>{live_feeds}/{total_feeds} feeds live</strong> — Signal is based on <strong>available data only</strong>.
    Missing feeds do <strong>NOT</strong> inflate or deflate the composite score.
  </p>
</div>
"""

    return html, quality_pct


def send_email(score_data, l1, l2, l3, boardroom, trade_ideas, layer8_html="", glint_html="", sanity_warnings=None, final_signal_data=None, glint_review="", extra_flags=None, shadow_output="", l3b=None, l3c=None, l3d=None, l3e=None, l8=None, l9=None, layer9_html="", quality_html=""):
    """Returns True only on a confirmed 200 from Resend. This is an
    early-warning system - a report that silently failed to send on the
    one day it mattered is worse than no report at all, so callers must
    check this and fail loudly (non-zero exit), not just log and move on."""
    if not RESEND_API_KEY:
        print("No Resend key — skipping email.")
        return False

    date_str = datetime.datetime.now().strftime("%A %d %B %Y, %H:%M UTC")
    score = score_data["score"]

    # The headline uses the Boardroom-adjusted signal when available (a
    # real majority vote can move it one level from the composite score's
    # signal); falls back to the composite signal alone if the Boardroom
    # never ran or its tally couldn't be parsed.
    final_signal_data = final_signal_data or {"signal": score_data["signal"], "emoji": score_data["emoji"], "overridden": False, "override_note": None}
    signal = final_signal_data["signal"]
    emoji = final_signal_data["emoji"]

    all_flags = l1["flags"] + l2["flags"] + l3["flags"] + (extra_flags or [])
    flags_html = "".join(f"<li>{f}</li>" for f in all_flags) if all_flags else "<li>No flags</li>"
    signal_color = {"GREEN": "#2ecc71", "AMBER": "#f39c12", "RED": "#e74c3c"}.get(signal, "#999")

    # Extract VVIX and MOVE for explicit display
    # MOVE is critical given elevated Treasury yields — always show it
    vvix_val = l1.get("data", {}).get("VVIX", "N/A")
    move_val = l2.get("data", {}).get("move_index", "N/A")
    indicator_display = ""
    if vvix_val != "N/A" or move_val != "N/A":
        indicator_lines = []
        if vvix_val != "N/A":
            indicator_lines.append(f"VIX disorderliness (VVIX): {vvix_val}")
        # Always show MOVE (even fetch errors indicate data issues worth flagging)
        move_display = move_val if move_val != "N/A" else "Unable to fetch"
        indicator_lines.append(f"Bond volatility (MOVE Index): {move_display}")
        indicator_html = "".join(f"<li>{line}</li>" for line in indicator_lines)
        indicator_display = f"""<h3 style="color: #f0c040;">📊 Key Indicator Snapshot</h3>
<ul style="background: #1a1a1a; padding: 15px 15px 15px 30px; border-radius: 4px;">
{indicator_html}
</ul>"""

    sanity_warnings = sanity_warnings or []
    sanity_html = ""
    if sanity_warnings:
        warnings_html = "".join(f"<li>{w}</li>" for w in sanity_warnings)
        sanity_html = f"""
<div style="background: #2a1a1a; border-left: 4px solid #e74c3c; padding: 15px; margin: 20px 0; border-radius: 4px;">
  <h3 style="margin: 0 0 8px 0; color: #e74c3c;">🚨 Self-Test Warnings — treat the score above with caution</h3>
  <ul style="margin: 6px 0; padding-left: 20px;">{warnings_html}</ul>
</div>
"""

    override_html = ""
    if final_signal_data.get("override_note"):
        override_color = "#f39c12" if final_signal_data["overridden"] else "#666"
        override_label = "🏛️ Boardroom Override" if final_signal_data["overridden"] else "🏛️ Boardroom Note"
        override_html = f"""
<div style="background: #1a1a1a; border-left: 4px solid {override_color}; padding: 15px; margin: 20px 0; border-radius: 4px;">
  <h3 style="margin: 0 0 8px 0; color: {override_color};">{override_label}</h3>
  <p style="margin: 0;">{final_signal_data['override_note']}</p>
</div>
"""

    # Generate dials dashboard if layer data is provided
    dials_html = ""
    if l3b and l3c and l3d and l3e and l8 and l9:
        try:
            dials_html = generate_dials_dashboard(score_data, l1, l2, l3, l3b, l3c, l3d, l3e, l8, l9)
        except Exception as e:
            print(f"  ⚠️  Dials generation failed: {e}", flush=True)

    html = f"""
<html><body style="font-family: Arial, sans-serif; max-width: 700px; margin: auto; background: #0d0d0d; color: #e0e0e0; padding: 20px;">
<h1 style="color: {signal_color}; border-bottom: 2px solid {signal_color}; padding-bottom: 10px;">
  {emoji} UNDERTOW INDEX — {signal}
</h1>
<p style="color: #aaa;">{date_str}</p>
<div style="background: #1a1a1a; border-left: 4px solid {signal_color}; padding: 15px; margin: 20px 0; border-radius: 4px;">
  <h2 style="margin: 0; color: {signal_color};">Composite score: {score}/{score_data['max']} ({score_data['signal']})</h2>
  <p style="margin: 8px 0 0 0;">{score_data['summary']}</p>
</div>
{quality_html}
{dials_html}
{override_html}
{sanity_html}
<h3 style="color: #f0c040;">⚡ Active Stress Flags</h3>
<ul style="background: #1a1a1a; padding: 15px 15px 15px 30px; border-radius: 4px;">
{flags_html}
</ul>
{indicator_display}
<h3 style="color: #f0c040;">🏛️ The Boardroom Verdict</h3>
<div style="background: #1a1a1a; padding: 15px; border-radius: 4px; white-space: pre-wrap; line-height: 1.6;">
{boardroom}
</div>
<h3 style="color: #f0c040;">🎯 Trade Ideas</h3>
<div style="background: #1a1a1a; padding: 15px; border-radius: 4px; white-space: pre-wrap; line-height: 1.6;">
{trade_ideas}
</div>
{f'''<h3 style="color: #f0c040;">📊 NFCI SYSTEMIC RISK (Chicago Fed Weekly)</h3>
<div style="background: #1a1a1a; padding: 15px; border-radius: 4px; white-space: pre-wrap; line-height: 1.6; font-family: monospace; font-size: 13px;">
{layer9_html}
</div>''' if layer9_html else ''}
<h3 style="color: #f0c040;">📊 SOFR FUNDING STRESS (NY Fed Daily)</h3>
<div style="background: #1a1a1a; padding: 15px; border-radius: 4px; white-space: pre-wrap; line-height: 1.6; font-family: monospace; font-size: 13px;">
{layer8_html}
</div>
{f'''<h3 style="color: #f0c040;">🔬 Shadow Indicators (Test Mode)</h3>
<div style="background: #1a1a1a; padding: 15px; border-radius: 4px; white-space: pre-wrap; line-height: 1.6; font-family: monospace; font-size: 13px;">
{shadow_output}
</div>''' if shadow_output else ''}
<h3 style="color: #f0c040;">💎 Glint — Value Screen</h3>
<div style="background: #1a1a1a; padding: 15px; border-radius: 4px; white-space: pre-wrap; line-height: 1.6; font-family: monospace; font-size: 13px;">
{glint_html}
</div>
{f'''<h3 style="color: #f0c040;">🏛️💎 Boardroom Review of Glint Candidates</h3>
<div style="background: #1a1a1a; padding: 15px; border-radius: 4px; white-space: pre-wrap; line-height: 1.6;">
{glint_review}
</div>''' if glint_review else ''}
<hr style="border-color: #333; margin-top: 30px;">
<p style="color: #555; font-size: 12px;">Undertow Index — automated macro intelligence. Not financial advice.</p>
</body></html>
"""

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": "Undertow Index <onboarding@resend.dev>",
                "to": ALERT_EMAILS,
                "subject": f"{emoji} Undertow Index — {signal} ({score}/{score_data['max']}) — {datetime.datetime.now().strftime('%d %b %Y')}",
                "html": html
            },
            timeout=15
        )
        if response.status_code == 200:
            print(f"✅ Email sent to {', '.join(ALERT_EMAILS)}")
            return True
        else:
            print(f"❌ Email failed: {response.status_code} — {response.text}")
            return False
    except Exception as e:
        print(f"❌ Email error: {e}")
        return False

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# LAYER 8: SOFR FUNDING STRESS (CREDIT STRESS INDICATOR)
# ─────────────────────────────────────────────

def get_layer8():
    """SOFR Funding Stress: 3M SOFR Term Rate + Volume (NY Fed data).

    Measures short-term funding costs AND market liquidity. Elevated rates
    + low volume = funding squeeze (credit stress). Uses FRED API.

    Thresholds are DYNAMIC, calibrated to current Fed target rate:
    - CALM: SOFR < Fed_Target + 1.00pp AND volume > 2,950B
    - CAUTION: SOFR Fed_Target + 1.00pp to Fed_Target + 1.20pp OR volume 2,850-2,950B
    - STRESS: SOFR >= Fed_Target + 1.75pp AND volume < 2,850B

    Fed_Target is read from FED_TARGET_RATE environment variable (default 4.25%)
    Update FED_TARGET_RATE on Railway whenever Fed changes target.

    Returns {score (0-5), max, flags, data}.
    """
    try:
        fred_key = os.environ.get("FRED_API_KEY", "").strip()
        if not fred_key:
            return {"score": 0, "max": 5, "flags": ["SOFR: FRED_API_KEY not set"], "data": {}}

        # Fetch 3-Month SOFR Term Rate + Volume from FRED
        url = "https://api.stlouisfed.org/fred/series/observations"

        # SOFR 3M Term Rate (from NY Fed, via FRED)
        params_sofr = {
            "series_id": "SOFR3Mfsr",
            "api_key": fred_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 2  # Get 2 in case today's not published yet
        }

        # SOFR Volume (billions, from FRED) - often lags by 1-2 days
        params_vol = {
            "series_id": "SOFRVOL",
            "api_key": fred_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 2  # Get 2 in case today's not published
        }

        try:
            r_sofr = requests.get(url, params=params_sofr, timeout=10)
            r_vol = requests.get(url, params=params_vol, timeout=10)
        except Exception as e:
            print(f"  ⚠️  SOFR FRED API connection error: {e}", flush=True)
            return {"score": 0, "max": 5, "flags": [f"SOFR: FRED API unreachable ({str(e)[:40]})"], "data": {}}

        sofr_fetch_ok = r_sofr.status_code == 200
        vol_fetch_ok = r_vol.status_code == 200

        sofr_3m = None
        sofr_date = None
        volume_b = None

        # Parse SOFR 3M rate (critical feed - if missing, fail gracefully)
        if sofr_fetch_ok:
            try:
                obs_list = r_sofr.json().get("observations", [])
                if not obs_list:
                    print(f"  ⚠️  SOFR3Mfsr: No observations returned from FRED", flush=True)
                for obs in obs_list:
                    if obs["value"] != ".":
                        sofr_3m = float(obs["value"])
                        sofr_date = obs.get("date", "")
                        break
            except Exception as e:
                print(f"  ⚠️  SOFR3Mfsr parse error: {e}", flush=True)
                print(f"      Response text: {r_sofr.text[:200]}", flush=True)
        else:
            print(f"  ⚠️  SOFR3Mfsr fetch failed: HTTP {r_sofr.status_code}", flush=True)
            print(f"      Response: {r_sofr.text[:200]}", flush=True)

        # Parse volume (optional - works if feed exists, skips gracefully if not)
        if vol_fetch_ok:
            try:
                obs_list = r_vol.json().get("observations", [])
                if not obs_list:
                    print(f"  ⚠️  SOFRVOL: No observations returned from FRED (data may lag 1-2 days)", flush=True)
                for obs in obs_list:
                    if obs["value"] != ".":
                        volume_b = float(obs["value"])
                        break
            except Exception as e:
                print(f"  ⚠️  SOFRVOL parse error: {e}", flush=True)
                print(f"      Response text: {r_vol.text[:200]}", flush=True)
        else:
            print(f"  ⚠️  SOFRVOL fetch failed: HTTP {r_vol.status_code} (volume stress detection disabled)", flush=True)
            print(f"      Response: {r_vol.text[:200]}", flush=True)

        if sofr_3m is None:
            return {"score": 0, "max": 5, "flags": ["SOFR3Mfsr: FRED data unavailable"], "data": {}}

        # Score based on SOFR rate AND volume (liquidity stress)
        # Thresholds are DYNAMIC, tied to current Fed target rate
        score = 0
        status = "NORMAL"
        flags = []

        # Dynamic thresholds from config (based on FED_TARGET_RATE environment variable)
        sofr_warning_level = SOFR_WARNING_THRESHOLD  # Fed_Target + 1.75pp
        sofr_caution_level = SOFR_CAUTION_THRESHOLD  # Fed_Target + 1.20pp
        sofr_watch_level = FED_TARGET_RATE + 1.00  # Fed_Target + 1.00pp

        sofr_stress = sofr_3m >= sofr_warning_level  # SOFR critically elevated
        sofr_caution = sofr_caution_level <= sofr_3m < sofr_warning_level  # SOFR tightening
        sofr_watch = sofr_watch_level <= sofr_3m < sofr_caution_level  # SOFR slightly above Fed rate
        vol_stress = volume_b is not None and volume_b < 2850  # Volume drying up
        vol_caution = volume_b is not None and 2850 <= volume_b <= 2950

        if sofr_stress and vol_stress:
            # Both rate AND volume stressed = real funding crisis
            score = 5
            status = "STRESS"
            flags.append(f"SOFR funding crisis: {sofr_3m:.2f}% (threshold {sofr_warning_level:.2f}%) + volume {volume_b:.0f}B (collapsed)")
        elif sofr_stress or vol_stress:
            # Either rate spiking OR volume drying up = caution
            score = 2
            status = "CAUTION"
            if sofr_stress:
                flags.append(f"SOFR critically elevated: {sofr_3m:.2f}% (≥{sofr_warning_level:.2f}% warning level)")
            if vol_stress:
                flags.append(f"SOFR volume stress: {volume_b:.0f}B (below 2,850 threshold)")
        elif sofr_caution or vol_caution:
            # Elevated but not critical = watch closely
            score = 1
            status = "WATCH"
            if sofr_caution:
                flags.append(f"SOFR elevated: {sofr_3m:.2f}% ({sofr_caution_level:.2f}%-{sofr_warning_level:.2f}% caution band)")
            if vol_caution:
                flags.append(f"SOFR volume elevated: {volume_b:.0f}B")
        elif sofr_watch:
            # Above Fed rate but not yet concerning
            score = 0
            status = "WATCH"
            flags.append(f"SOFR slightly above Fed rate: {sofr_3m:.2f}% (Fed target {FED_TARGET_RATE:.2f}%)")

        date_str = sofr_date if sofr_date else datetime.datetime.utcnow().strftime("%Y-%m-%d")

        return {
            "score": score,
            "max": 5,
            "flags": flags,
            "data": {
                "sofr_3m_pct": sofr_3m,
                "sofr_volume_b": volume_b,
                "sofr_date": date_str,
                "sofr_status": status
            }
        }
    except Exception as e:
        err_msg = f"SOFR fetch error: {type(e).__name__}: {str(e)[:60]}"
        print(f"  ⚠️  {err_msg}", flush=True)
        return {"score": 0, "max": 5, "flags": [err_msg], "data": {}}


def get_layer9():
    """NFCI/ANFCI Weekly Systemic Risk Indicator (Chicago Fed) — DUAL-INDEX VALIDATION.

    NFCI: National Financial Conditions Index (105 indicators of systemic stress)
    ANFCI: Adjusted NFCI (filters out economic noise to isolate institutional panic)

    Both published by Chicago Fed, typically on Wednesdays.
    Baseline = 0.00 (average conditions since 1971).

    DUAL-INDEX FRAMEWORK (minimizes false alarms):
    - CALM: Both NFCI and ANFCI firmly negative (< -0.40)
    - CAUTION: Either index creeps toward 0.00, OR ANFCI accelerates faster than NFCI
               (indicates pure institutional panic emerging)
    - WARNING: Either index crosses >= 0.00 (confirmed systemic financial distress)

    Returns {score (0-5), max, flags, data with validation details}.
    """
    try:
        fred_key = os.environ.get("FRED_API_KEY", "").strip()
        if not fred_key:
            return {"score": 0, "max": 5, "flags": ["NFCI: FRED_API_KEY not set"], "data": {}}

        url = "https://api.stlouisfed.org/fred/series/observations"

        # Fetch NFCI — need 2 recent observations (current + prior week)
        params_nfci = {
            "series_id": "NFCI",
            "api_key": fred_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 3  # Get 3 to handle if latest isn't published yet
        }

        # Fetch ANFCI — need 2 recent observations for divergence detection
        params_anfci = {
            "series_id": "ANFCI",
            "api_key": fred_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 3  # Get 3 to handle if latest isn't published yet
        }

        try:
            r_nfci = requests.get(url, params=params_nfci, timeout=10)
            r_anfci = requests.get(url, params=params_anfci, timeout=10)
        except Exception as e:
            print(f"  ⚠️  NFCI FRED API connection error: {e}", flush=True)
            return {"score": 0, "max": 5, "flags": [f"NFCI: FRED API unreachable ({str(e)[:40]})"], "data": {}}

        nfci_fetch_ok = r_nfci.status_code == 200
        anfci_fetch_ok = r_anfci.status_code == 200

        # Parse NFCI current and prior
        nfci_observations = []
        if nfci_fetch_ok:
            try:
                obs_list = r_nfci.json().get("observations", [])
                if not obs_list:
                    print(f"  ⚠️  NFCI: No observations returned from FRED (published weekly, may lag)", flush=True)
                    return {"score": 0, "max": 5, "flags": ["NFCI: No recent FRED observations (published Wed)"], "data": {}}
                for obs in obs_list:
                    if obs["value"] != ".":
                        nfci_observations.append({
                            "value": float(obs["value"]),
                            "date": obs.get("date", "")
                        })
                if len(nfci_observations) < 1:
                    return {"score": 0, "max": 5, "flags": ["NFCI: No valid FRED observations"], "data": {}}
            except Exception as e:
                print(f"  ⚠️  NFCI parse error: {e}", flush=True)
                print(f"      Response text: {r_nfci.text[:300]}", flush=True)
                return {"score": 0, "max": 5, "flags": [f"NFCI parse error: {e}"], "data": {}}
        else:
            print(f"  ⚠️  NFCI fetch failed: HTTP {r_nfci.status_code}", flush=True)
            print(f"      Response: {r_nfci.text[:300]}", flush=True)
            return {"score": 0, "max": 5, "flags": [f"NFCI fetch failed: HTTP {r_nfci.status_code}"], "data": {}}

        # Parse ANFCI current and prior
        anfci_observations = []
        if anfci_fetch_ok:
            try:
                obs_list = r_anfci.json().get("observations", [])
                if not obs_list:
                    print(f"  ⚠️  ANFCI: No observations returned from FRED (may lag NFCI)", flush=True)
                for obs in obs_list:
                    if obs["value"] != ".":
                        anfci_observations.append({
                            "value": float(obs["value"]),
                            "date": obs.get("date", "")
                        })
            except Exception as e:
                print(f"  ⚠️  ANFCI parse error: {e}", flush=True)
                print(f"      Response text: {r_anfci.text[:300]}", flush=True)
                # ANFCI fetch failure is recoverable — use NFCI only
                anfci_observations = []
        else:
            print(f"  ⚠️  ANFCI fetch failed: HTTP {r_anfci.status_code}", flush=True)
            print(f"      Response: {r_anfci.text[:300]}", flush=True)
            # ANFCI is optional for scoring but helps divergence detection

        # Extract current values
        nfci_value = nfci_observations[0]["value"]
        nfci_date = nfci_observations[0]["date"]
        nfci_prior = nfci_observations[1]["value"] if len(nfci_observations) > 1 else None
        nfci_momentum = (nfci_value - nfci_prior) if nfci_prior is not None else 0

        anfci_value = anfci_observations[0]["value"] if anfci_observations else None
        anfci_prior = anfci_observations[1]["value"] if len(anfci_observations) > 1 else None
        anfci_momentum = (anfci_value - anfci_prior) if (anfci_value is not None and anfci_prior is not None) else None

        # Calculate spread: positive spread (NFCI > ANFCI) means economic/policy tightening
        #                  negative spread (ANFCI > NFCI) means pure institutional panic
        nfci_spread = (nfci_value - anfci_value) if anfci_value is not None else None

        # ────────────────────────────────────────────────────
        # DUAL-INDEX SCORING LOGIC
        # ────────────────────────────────────────────────────
        score = 0
        status = "CALM"
        flags = []

        # Check divergence: if ANFCI > NFCI, pure panic is visible
        anfci_divergence = False
        if anfci_value is not None and nfci_spread is not None and nfci_spread < -0.05:
            anfci_divergence = True
            flags.append(f"⚡ ANFCI divergence detected (ANFCI > NFCI by {abs(nfci_spread):.2f}) — institutional panic signal")

        # Check ANFCI acceleration: if ANFCI rising faster than NFCI
        anfci_accelerating = False
        if anfci_momentum is not None and nfci_momentum is not None and anfci_momentum > nfci_momentum + 0.10:
            anfci_accelerating = True
            flags.append(f"⚡ ANFCI accelerating ({anfci_momentum:+.2f}) faster than NFCI ({nfci_momentum:+.2f}) — pure stress building")

        # WARNING: Either index >= 0.00
        if nfci_value >= 0.00 or (anfci_value is not None and anfci_value >= 0.00):
            score = 5
            status = "WARNING"
            if nfci_value >= 0.00:
                flags.append(f"NFCI {nfci_value:+.2f} — systemic financial distress (institutional panic confirmed)")
            if anfci_value is not None and anfci_value >= 0.00:
                flags.append(f"ANFCI {anfci_value:+.2f} — pure institutional stress confirmed")

        # CAUTION: Either index -0.40 to <0.00, OR divergence/acceleration detected
        elif (nfci_value >= -0.40 or (anfci_value is not None and anfci_value >= -0.40)
              or anfci_divergence or anfci_accelerating):
            score = 2
            status = "CAUTION"
            if nfci_value >= -0.40 and nfci_value < 0:
                flags.append(f"NFCI {nfci_value:+.2f} — financial conditions tightening toward distress")
            if anfci_value is not None and anfci_value >= -0.40 and anfci_value < 0:
                flags.append(f"ANFCI {anfci_value:+.2f} — noise-filtered stress building")

        # CALM: Both indices firmly < -0.40 with no divergence
        else:
            score = 0
            status = "CALM"
            if nfci_value < -0.40 and (anfci_value is None or anfci_value < -0.40):
                flags.append(f"Both indices firmly negative (NFCI {nfci_value:.2f}, ANFCI {anfci_value:.2f if anfci_value else 'N/A'}) — healthy financial conditions")

        date_str = nfci_date if nfci_date else datetime.datetime.utcnow().strftime("%Y-%m-%d")

        return {
            "score": score,
            "max": 5,
            "flags": flags,
            "data": {
                "nfci_value": round(nfci_value, 2),
                "nfci_prior": round(nfci_prior, 2) if nfci_prior is not None else None,
                "nfci_momentum": round(nfci_momentum, 2),
                "anfci_value": round(anfci_value, 2) if anfci_value is not None else None,
                "anfci_prior": round(anfci_prior, 2) if anfci_prior is not None else None,
                "anfci_momentum": round(anfci_momentum, 2) if anfci_momentum is not None else None,
                "nfci_spread": round(nfci_spread, 2) if nfci_spread is not None else None,
                "anfci_divergence": anfci_divergence,
                "anfci_accelerating": anfci_accelerating,
                "nfci_date": date_str,
                "nfci_status": status
            }
        }
    except Exception as e:
        err_msg = f"NFCI fetch error: {type(e).__name__}: {str(e)[:60]}"
        print(f"  ⚠️  {err_msg}", flush=True)
        return {"score": 0, "max": 5, "flags": [err_msg], "data": {}}


def format_layer9_for_email(layer9_data):
    """
    Formats Layer 9 output into a clean text block for the email report.
    Displays NFCI + ANFCI dual-index validation with divergence & momentum detection.
    """
    data = layer9_data.get("data", {})

    if "nfci_value" not in data:
        if layer9_data.get("flags"):
            return f"📊 NFCI Systemic Risk: {layer9_data['flags'][0]}"
        return "📊 NFCI Systemic Risk: unavailable"

    nfci_val = data.get("nfci_value")
    nfci_momentum = data.get("nfci_momentum", 0)
    anfci_val = data.get("anfci_value")
    anfci_momentum = data.get("anfci_momentum")
    spread = data.get("nfci_spread")
    divergence = data.get("anfci_divergence", False)
    accelerating = data.get("anfci_accelerating", False)
    date_str = data.get("nfci_date", "")
    status = data.get("nfci_status", "UNKNOWN")

    lines = ["📊 NFCI BROAD SYSTEMIC RISK — CHICAGO FED WEEKLY INDICATOR (DUAL-INDEX)", ""]

    # Index values with momentum
    lines.append(f"NFCI (105 indicators): {nfci_val:+.2f} (momentum: {nfci_momentum:+.2f})")
    if anfci_val is not None:
        lines.append(f"ANFCI (noise-filtered):  {anfci_val:+.2f} (momentum: {anfci_momentum:+.2f if anfci_momentum is not None else 'N/A'})")
        if spread is not None:
            spread_interpretation = "panic" if spread < -0.05 else "economic/policy" if spread > 0.05 else "balanced"
            lines.append(f"Spread (NFCI-ANFCI): {spread:+.2f} ({spread_interpretation} tightening)")
    lines.append(f"Date: {date_str}")
    lines.append("")

    # Status and validation signals
    if status == "WARNING":
        lines.append("Status: 🔴 WARNING — systemic financial distress confirmed by dual-index")
    elif status == "CAUTION":
        lines.append("Status: 🟡 CAUTION — stress signal detected, monitor for escalation")
    else:
        lines.append("Status: 🟢 CALM — healthy financial conditions across all indices")

    # Validation details
    if divergence or accelerating:
        lines.append("")
        lines.append("Validation Signals:")
        if divergence:
            lines.append(f"  ⚡ DIVERGENCE: ANFCI > NFCI → pure institutional panic is visible")
        if accelerating:
            lines.append(f"  ⚡ ACCELERATION: ANFCI rising faster than NFCI → stress building")

    # Flags from analysis
    if layer9_data.get("flags"):
        lines.append("")
        for f in layer9_data["flags"]:
            lines.append(f"  ⚡ {f}")

    # Framework explanation
    lines.append("")
    lines.append("Framework (minimizes false alarms):")
    lines.append("  • CALM: Both indices < -0.40 (loose financial conditions)")
    lines.append("  • CAUTION: Either index -0.40 to 0.00, OR divergence/acceleration detected")
    lines.append("  • WARNING: Either index ≥ 0.00 (confirmed systemic stress)")

    return "\n".join(lines)


def format_layer8_for_email(layer8_data):
    """
    Formats Layer 8 output into a clean text block for the email report.
    Displays SOFR rate + volume to detect funding liquidity stress.
    """
    data = layer8_data.get("data", {})

    if "sofr_3m_pct" not in data:
        if layer8_data.get("flags"):
            return f"📊 SOFR Funding Stress: {layer8_data['flags'][0]}"
        return "📊 SOFR Funding Stress: unavailable"

    sofr_pct = data["sofr_3m_pct"]
    volume_b = data.get("sofr_volume_b")
    date_str = data.get("sofr_date", "")
    status = data.get("sofr_status", "UNKNOWN")

    lines = ["📊 SOFR FUNDING STRESS — NY FED CREDIT INDICATOR", ""]
    lines.append(f"SOFR 3M Term Rate: {sofr_pct:.2f}%")
    if volume_b is not None:
        lines.append(f"SOFR Trading Volume: ${volume_b:.0f}B")
    lines.append(f"Date: {date_str}")
    lines.append("")

    if status == "STRESS":
        lines.append("Status: 🔴 STRESS — funding liquidity crisis signal")
    elif status == "CAUTION":
        lines.append("Status: 🟡 CAUTION — elevated funding costs or volume stress, monitor closely")
    elif status == "WATCH":
        lines.append("Status: 🟡 WATCH — minor elevation, keep eyes on this")
    else:
        lines.append("Status: 🟢 NORMAL — baseline funding conditions")

    if layer8_data.get("flags"):
        lines.append("")
        for f in layer8_data["flags"]:
            lines.append(f"⚡ {f}")

    # Add context: show thresholds for transparency
    lines.append("")
    lines.append("Thresholds: CALM (SOFR<3.80% + Vol>2,950B) | CAUTION (SOFR 3.80-3.90% OR Vol 2,850-2,950B) | STRESS (SOFR>3.90% + Vol<2,850B)")

    return "\n".join(lines)


def main():
    print("=" * 60)
    print("UNDERTOW INDEX — RUNNING")
    print("=" * 60)

    print("\n[Layer 1] Equity pulse...")
    l1 = get_layer1()
    print(f"  Score: {l1['score']}/{l1['max']} | Flags: {len(l1['flags'])}")

    print("[Layer 2] Credit & yield curve...")
    l2 = get_layer2()
    print(f"  Score: {l2['score']}/{l2['max']} | Flags: {len(l2['flags'])}")

    print("[Layer 3] Macro tremors...")
    l3 = get_layer3()
    print(f"  Score: {l3['score']}/{l3['max']} | Flags: {len(l3['flags'])}")

    print("[Layer 3b] COT positioning & repo stress...", flush=True)
    l3b = get_layer3b()
    print(f"  Score: {l3b['score']}/{l3b['max']} | Flags: {len(l3b['flags'])}", flush=True)

    print("[Layers 3c/3e] Fetching SPY options chain (shared by both layers)...", flush=True)
    try:
        spy_chain = _fetch_spy_option_chain()
    except Exception as e:
        spy_chain = None
        print(f"  ⚠️  Shared options chain fetch failed: {e} (3c/3e will each retry individually)", flush=True)

    print("[Layer 3c] Options sentiment (put/call ratio)...", flush=True)
    l3c = get_layer3c(spy_chain)
    print(f"  Score: {l3c['score']}/{l3c['max']} | Flags: {len(l3c['flags'])}", flush=True)

    print("[Layer 3d] SKEW index (tail-risk pricing)...", flush=True)
    l3d = get_layer3d()
    print(f"  Score: {l3d['score']}/{l3d['max']} | Flags: {len(l3d['flags'])}", flush=True)

    print("[Layer 3e] Dealer gamma exposure (GEX)...", flush=True)
    l3e = get_layer3e(spy_chain)
    print(f"  Score: {l3e['score']}/{l3e['max']} | Flags: {len(l3e['flags'])}", flush=True)

    print("[Layer 8] SOFR funding stress (credit stress indicator)...", flush=True)
    l8 = get_layer8()
    print(f"  Score: {l8['score']}/{l8['max']} | Flags: {len(l8['flags'])}", flush=True)

    print("[Layer 9] NFCI/ANFCI broad systemic risk (Chicago Fed weekly)...", flush=True)
    l9 = get_layer9()
    print(f"  Score: {l9['score']}/{l9['max']} | Flags: {len(l9['flags'])}", flush=True)

    print("[Self-Test] Running sanity checks...", flush=True)
    sanity_warnings = run_sanity_checks(l1, l2, l3, l3b, l3c, l3d, l3e, l8, l9)
    if sanity_warnings:
        for w in sanity_warnings:
            print(f"  🚨 {w}", flush=True)
    else:
        print("  All checks passed.", flush=True)

    print("[Composite Score] Computing aggregate stress...")
    score_data = compute_score(l1, l2, l3, l3b, l3c, l3d, l3e, l8, l9)
    print(f"\n  {score_data['emoji']} SIGNAL: {score_data['signal']} ({score_data['score']}/{score_data['max']})")
    print(f"  {score_data['summary']}")

    for flag in l1["flags"] + l2["flags"] + l3["flags"]:
        print(f"  ⚠️  {flag}")

    # Glint runs before the Boardroom now, so the panel can review its
    # candidates as part of the same grounded session.
    print("\n[Glint] Screening watchlist for undervalued quality names...", flush=True)
    glint_results = []
    try:
        glint_results = run_glint()
        glint_html = build_glint_section(glint_results)
        candidates = sum(1 for r, f in glint_results if r.is_candidate)
        print(f"  {candidates} candidate(s) found", flush=True)
    except Exception as e:
        print(f"  ⚠️  Glint screen failed, skipping section: {e}", flush=True)
        glint_html = "💎 Glint screen unavailable today."

    all_flags = l1["flags"] + l2["flags"] + l3["flags"] + l3b["flags"] + l3d["flags"] + l3e["flags"] + l8["flags"] + l9["flags"]
    flags_text = "\n".join(all_flags) if all_flags else "No flags raised."
    raw_data = {**l1.get("data", {}), **l2.get("data", {}), **l3.get("data", {}),
                **l3b.get("data", {}), **l3d.get("data", {}), **l3e.get("data", {}), **l8.get("data", {}), **l9.get("data", {})}
    data_text = json.dumps(raw_data, indent=2)

    run_full, mode_reason = should_run_full_research(score_data["signal"])
    boardroom_mode = "full" if run_full else "cheap"
    print(f"\n[Layer 5] Boardroom mode: {boardroom_mode} ({mode_reason})", flush=True)

    research = None
    glint_review = ""
    if run_full and ANTHROPIC_API_KEY:
        try:
            print("  Generating framework-based board opinions...", flush=True)
            research = run_member_research(ANTHROPIC_API_KEY, score_data["signal"], data_text, flags_text)
            votes = sum(1 for r in research if r.get("vote"))
            print(f"  Board members voted: {votes}/{len(research)}", flush=True)
            boardroom = run_full_boardroom(ANTHROPIC_API_KEY, score_data, data_text, flags_text, research)
            boardroom = f"[Full framework-based run — {mode_reason}; all {len(research)} members voted]\n\n" + boardroom
            try:
                glint_review = run_glint_review(ANTHROPIC_API_KEY, research, glint_results, score_data)
            except Exception as e:
                print(f"  ⚠️  Glint review failed: {e}", flush=True)
                glint_review = "Boardroom review of Glint candidates unavailable today (call failed)."
        except Exception as e:
            print(f"  ⚠️  Full boardroom failed ({e}) — falling back to cheap board.", flush=True)
            research = None
            boardroom = run_boardroom(score_data, l1, l2, l3, l8)
            boardroom = "[Desk view — full grounded run FAILED today, this is the unresearched fallback]\n\n" + boardroom
    else:
        boardroom = run_boardroom(score_data, l1, l2, l3, l8)
        boardroom = f"[Desk view — {mode_reason}; member takes are NOT grounded in fresh research today]\n\n" + boardroom
    print(boardroom)

    tally = parse_boardroom_tally(boardroom)
    final_signal_data = apply_boardroom_override(score_data, tally)
    if final_signal_data["overridden"]:
        print(f"  🏛️  BOARDROOM OVERRIDE: {final_signal_data['override_note']}", flush=True)
    elif final_signal_data["override_note"]:
        print(f"  🏛️  {final_signal_data['override_note']}", flush=True)

    log_run(boardroom_mode, mode_reason, research, tally, final_signal_data, score_data)

    print("\n[Layer 6] Generating trade ideas...")
    trade_ideas = get_trade_ideas(score_data, l1, l2, l3, l8, effective_signal=final_signal_data["signal"])
    print(trade_ideas)

    # A failed LLM call must land in the red warnings box - rendered as a
    # blank section it reads as "nothing to report", which let an API
    # failure hide behind "Boardroom tally unavailable".
    if "Boardroom error:" in boardroom:
        sanity_warnings.append("🏛️ Boardroom API call FAILED — no panel verdict or tally today; headline is the raw composite signal only.")
    if "Trade ideas error:" in trade_ideas:
        sanity_warnings.append("🎯 Trade Ideas API call FAILED — section shows the error message, not ideas.")

    # Shadow indicators (test mode, Sep 26 - Oct 23)
    shadow_output = ""
    if SHADOW_INDICATORS_AVAILABLE:
        try:
            print("\n[Shadow Indicators] Running three new leading indicators (test mode)...", flush=True)
            breadth, breadth_status = fetch_breadth_indicator()
            vix_term, vix_status = fetch_vix_term_structure()
            qqq_ratio, qqq_status = fetch_qqq_put_call_ratio()
            # Note: TED Spread replaced with SOFR in Layer 8 (no longer in shadow indicators)

            print(f"  Market Breadth: {breadth:.1f}% [{breadth_status}]" if breadth else f"  Market Breadth: None [{breadth_status}]", flush=True)
            print(f"  VIX Term Ratio: {vix_term:.3f} [{vix_status}]" if vix_term else f"  VIX Term Ratio: None [{vix_status}]", flush=True)
            print(f"  QQQ Put/Call: {qqq_ratio:.2f} [{qqq_status}]" if qqq_ratio else f"  QQQ Put/Call: None [{qqq_status}]", flush=True)

            shadow_output = (
                f"\n📊 SHADOW INDICATORS (Test Period: Sep 26 — Oct 23)\n"
                f"Market Breadth: {breadth:.1f}% [{breadth_status}]\n"
                f"VIX Term Ratio: {vix_term:.3f} [{vix_status}]\n"
                f"QQQ Put/Call: {qqq_ratio:.2f} [{qqq_status}]\n"
                f"(Running in parallel — does NOT change RED/AMBER/GREEN signal until validated)"
            ) if (breadth and vix_term and qqq_ratio) else f"Shadow indicators incomplete (one or more failed to fetch)"
        except Exception as e:
            print(f"  ⚠️  Shadow indicators error: {e}", flush=True)
            shadow_output = f"Shadow indicators unavailable: {e}"
    else:
        shadow_output = "Shadow indicators module not loaded"

    print("\n[Layer 9] Formatting NFCI/ANFCI for email...")
    layer9_html = format_layer9_for_email(l9)
    print(layer9_html)

    print("\n[Layer 8] Formatting SOFR for email...")
    layer8_html = format_layer8_for_email(l8)
    print(layer8_html)

    print("\n[Data Quality] Building dashboard...")
    quality_html, quality_pct = build_data_quality_dashboard(l1, l2, l3, l3b, l3c, l3d, l3e, l8, l9)
    print(f"  Data quality: {quality_pct}% ({sum(1 for l in [l1, l2, l3, l3b, l3c, l3d, l3e, l8, l9] if l.get('data'))}/9 layers with data)")

    # Labeled inputs for Ark Protocol (not built yet): composite score,
    # the Boardroom's grounded market view, and its view on Glint's
    # candidates - logged as one JSON blob so Ark can consume reasoning,
    # not just one opaque number.
    ark_inputs = {
        "composite": {"score": score_data["score"], "max": score_data["max"], "signal": score_data["signal"]},
        "boardroom": {"mode": boardroom_mode, "tally": tally,
                      "final_signal": final_signal_data["signal"],
                      "overridden": final_signal_data["overridden"]},
        "glint_candidates": [
            {"ticker": f.ticker, "value_score": r.value_score, "price": f.price}
            for r, f in glint_results if r.is_candidate
        ],
        "glint_review_available": bool(glint_review),
    }
    print(f"ARK_INPUTS_JSON: {json.dumps(ark_inputs)}", flush=True)
    publish_ark_handoff(ark_inputs, ARK_HANDOFF_GIST_ID, GITHUB_GIST_TOKEN)

    print("\n[Email Report] Sending comprehensive analysis...")
    email_sent = send_email(score_data, l1, l2, l3, boardroom, trade_ideas, layer8_html, glint_html, sanity_warnings, final_signal_data, glint_review, extra_flags=l3b["flags"] + l3d["flags"] + l3e["flags"], shadow_output=shadow_output, l3b=l3b, l3c=l3c, l3d=l3d, l3e=l3e, l8=l8, l9=l9, layer9_html=layer9_html, quality_html=quality_html)

    if not email_sent:
        print("\n" + "=" * 60)
        print("🚨 UNDERTOW INDEX — RUN COMPLETED BUT EMAIL DID NOT SEND 🚨")
        print("The analysis above is real, but you will NOT receive today's report.")
        print("=" * 60)
        sys.exit(1)

    print("\n" + "=" * 60)
    print("UNDERTOW INDEX — COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    main()
