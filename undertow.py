import os
import json
import time
import datetime
import requests
import yfinance as yf

from glint.runner import run_glint
from glint.email_section import build_glint_section
from boardroom_research import (
    should_run_full_research, run_member_research, run_full_boardroom,
    run_glint_review, log_run,
)

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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
RESEND_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
# Comma-separated. Gmail copy exists so Claude (connected to that
# account) can review the daily emails directly during testing.
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "micahbrown4@me.com,micah.brown7@gmail.com,jakobgoulding@gmail.com,olisbrown@gmail.com")
ALERT_EMAILS = [e.strip() for e in ALERT_EMAIL.split(",") if e.strip()]

# ─────────────────────────────────────────────
def fred_get(series_id, retries=3, backoff_seconds=2):
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
            r = requests.get(url, params=params, timeout=10)
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
        tickers = yf_download_with_retry("SPY RSP QQQ ^VIX NVDA", period="5d", interval="1d")
        close = tickers["Close"]

        spy = float(close["SPY"].dropna().iloc[-1])
        spy_prev = float(close["SPY"].dropna().iloc[-2])
        rsp = float(close["RSP"].dropna().iloc[-1])
        rsp_prev = float(close["RSP"].dropna().iloc[-2])
        nvda = float(close["NVDA"].dropna().iloc[-1])
        nvda_prev = float(close["NVDA"].dropna().iloc[-2])
        vix = float(close["^VIX"].dropna().iloc[-1])
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
            "max": 5,
            "flags": flags,
            "data": {
                "SPY": round(spy, 2), "SPY_chg": round(spy_chg, 2),
                "RSP": round(rsp, 2), "RSP_chg": round(rsp_chg, 2),
                "NVDA": round(nvda, 2), "NVDA_chg": round(nvda_chg, 2),
                "VIX": round(vix, 2),
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
        spread = t10 - t2
        data["yield_curve_spread"] = round(spread, 3)
        data["yield_curve_date"] = min(t10_date, t2_date)

        if spread < 0:
            score += 2
            flags.append(f"Yield curve inverted: 10Y-2Y = {spread:.3f}% (recession signal)")
        elif spread < 0.3:
            score += 1
            flags.append(f"Yield curve flat: 10Y-2Y = {spread:.3f}%")

        hy, hy_date = fred_get("BAMLH0A0HYM2")
        data["hy_spread"] = round(hy, 3)
        data["hy_spread_date"] = hy_date

        if hy > 5.0:
            score += 2
            flags.append(f"HY credit spreads wide at {hy:.2f}% — stress building")
        elif hy > 3.5:
            score += 1
            flags.append(f"HY spreads elevated at {hy:.2f}%")

    except Exception as e:
        flags.append(f"Layer 2 error: {e}")

    return {"score": score, "max": 4, "flags": flags, "data": data}

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
            long_pos = float(cot_data[0].get("lev_money_positions_long", 0))
            short_pos = float(cot_data[0].get("lev_money_positions_short", 0))
            net_pos = long_pos - short_pos
            data["cot_long"] = long_pos
            data["cot_short"] = short_pos
            data["cot_net"] = net_pos
            data["cot_report_date"] = cot_data[0].get("report_date_as_yyyy_mm_dd", "")[:10]
            if net_pos < 0:
                score += 1
                flags.append(f"COT: leveraged funds net SHORT E-mini S&P ({net_pos:,.0f} contracts)")
        else:
            flags.append("COT: no data returned")
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


def run_sanity_checks(l1, l2, l3, l3b, l3c, l3d):
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
    # holiday), COT gets 10 (it only reports weekly with a lag).
    _check_freshness(warnings, "Layer 1", l1["data"].get("last_bar_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 2 (yield curve)", l2["data"].get("yield_curve_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 2 (HY spread)", l2["data"].get("hy_spread_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 3", l3["data"].get("last_bar_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 3b (COT)", l3b["data"].get("cot_report_date"), max_age_days=10)
    _check_freshness(warnings, "Layer 3b (SOFR-DFF)", l3b["data"].get("sofr_dff_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 3b (reverse repo)", l3b["data"].get("reverse_repo_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 3b (TED-equiv)", l3b["data"].get("ted_spread_date"), max_age_days=5)
    _check_freshness(warnings, "Layer 3d (SKEW)", l3d["data"].get("last_bar_date"), max_age_days=5)

    pcr = l3c["data"].get("put_call_ratio")
    if pcr is not None and not (0.1 <= pcr <= 5):
        warnings.append(f"Layer 3c sanity: put/call ratio {pcr} outside plausible range (0.1-5)")

    skew_val = l3d["data"].get("skew")
    if skew_val is not None and not (80 <= skew_val <= 200):
        warnings.append(f"Layer 3d sanity: SKEW {skew_val} outside plausible range (80-200)")

    for label, layer in [("Layer 1", l1), ("Layer 2", l2), ("Layer 3", l3), ("Layer 3b", l3b), ("Layer 3c", l3c), ("Layer 3d", l3d)]:
        if not (0 <= layer["score"] <= layer["max"]):
            warnings.append(f"{label} sanity: score {layer['score']} outside valid range 0-{layer['max']}")

    return warnings

# ──────────────────────────────────────────────
# LAYER 3c: OPTIONS SENTIMENT (PUT/CALL RATIO)
# ──────────────────────────────────────────────
# DISABLED as of 2026-07-30: CBOE's public CDN archive for this data is a
# frozen historical dump, not a live feed - verified it stops at 2012
# (and an alternate CBOE archive URL stops at 2019). It was silently
# scoring "today's" sentiment off that frozen data every day since this
# layer was added. No free live replacement was found. Contributes 0 and
# max 0 until a real live source is wired in - see the loud flag below
# instead of a silent absence.
def get_layer3c():
    return {
        "score": 0,
        "max": 0,
        "flags": ["Layer 3c DISABLED - CBOE's free put/call archive is stale (frozen since ~2012), not live data. Needs a paid/live options-data source before this can be trusted again."],
        "data": {},
    }


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

def compute_score(l1, l2, l3, l3b, l3c, l3d):
    # l3c is disabled (see get_layer3c) and always contributes 0/0, so the
    # real max is 21, not 23. Thresholds rescaled down from 7/14 out of 23
    # to 6/13 out of 21, matching the same proportion.
    total = l1["score"] + l2["score"] + l3["score"] + l3b["score"] + l3c["score"] + l3d["score"]

    if total <= 6:
        signal = "GREEN"
        emoji = "🟢"
        summary = "Markets calm. No significant stress signals detected."
    elif total <= 13:
        signal = "AMBER"
        emoji = "🟡"
        summary = "Elevated risk. Multiple stress signals present. Watch closely."
    else:
        signal = "RED"
        emoji = "🔴"
        summary = "High alert. Significant macro stress across multiple indicators."

    return {"score": total, "max": 21, "signal": signal, "emoji": emoji, "summary": summary}

# ─────────────────────────────────────────────
# LAYER 5: THE BOARDROOM
# ─────────────────────────────────────────────
def run_boardroom(score_data, l1, l2, l3):
    if not ANTHROPIC_API_KEY:
        return "Boardroom unavailable — no API key."

    all_flags = l1["flags"] + l2["flags"] + l3["flags"]
    flags_text = "\n".join(all_flags) if all_flags else "No flags raised."
    raw_data = {**l1.get("data", {}), **l2.get("data", {}), **l3.get("data", {})}
    data_text = json.dumps(raw_data, indent=2)

    prompt = f"""You are running The Boardroom — a council of the world's greatest investors and traders.

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
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 4500,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=150
        )
        data = response.json()
        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(text_blocks)
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
def get_trade_ideas(score_data, l1, l2, l3, effective_signal=None):
    if not ANTHROPIC_API_KEY:
        return "Trade ideas unavailable — no API key."

    # Use the Boardroom-adjusted signal if one was computed, so trade
    # ideas match whatever signal actually appears in the email headline
    # rather than the pre-Boardroom composite signal alone.
    signal = effective_signal or score_data["signal"]
    score = score_data["score"]
    all_flags = l1["flags"] + l2["flags"] + l3["flags"]
    flags_text = "\n".join(all_flags) if all_flags else "No flags."

    prompt = f"""You are Michael Burry's trading desk AI. Current Undertow signal: {signal} ({score}/{score_data['max']}).

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

Be specific. No waffle."""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=45
        )
        data = response.json()
        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(text_blocks)
    except Exception as e:
        return f"Trade ideas error: {e}"

# ─────────────────────────────────────────────
# LAYER 7: EMAIL via RESEND
# ─────────────────────────────────────────────
def send_email(score_data, l1, l2, l3, boardroom, trade_ideas, layer8_html="", glint_html="", sanity_warnings=None, final_signal_data=None, glint_review=""):
    if not RESEND_API_KEY:
        print("No Resend key — skipping email.")
        return

    date_str = datetime.datetime.now().strftime("%A %d %B %Y, %H:%M UTC")
    score = score_data["score"]

    # The headline uses the Boardroom-adjusted signal when available (a
    # real majority vote can move it one level from the composite score's
    # signal); falls back to the composite signal alone if the Boardroom
    # never ran or its tally couldn't be parsed.
    final_signal_data = final_signal_data or {"signal": score_data["signal"], "emoji": score_data["emoji"], "overridden": False, "override_note": None}
    signal = final_signal_data["signal"]
    emoji = final_signal_data["emoji"]

    all_flags = l1["flags"] + l2["flags"] + l3["flags"]
    flags_html = "".join(f"<li>{f}</li>" for f in all_flags) if all_flags else "<li>No flags</li>"
    signal_color = {"GREEN": "#2ecc71", "AMBER": "#f39c12", "RED": "#e74c3c"}.get(signal, "#999")

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
{override_html}
{sanity_html}
<h3 style="color: #f0c040;">⚡ Active Stress Flags</h3>
<ul style="background: #1a1a1a; padding: 15px 15px 15px 30px; border-radius: 4px;">
{flags_html}
</ul>
<h3 style="color: #f0c040;">🏛️ The Boardroom Verdict</h3>
<div style="background: #1a1a1a; padding: 15px; border-radius: 4px; white-space: pre-wrap; line-height: 1.6;">
{boardroom}
</div>
<h3 style="color: #f0c040;">🎯 Trade Ideas</h3>
<div style="background: #1a1a1a; padding: 15px; border-radius: 4px; white-space: pre-wrap; line-height: 1.6;">
{trade_ideas}
</div>
<h3 style="color: #f0c040;">📊 IBKR Portfolio</h3>
<div style="background: #1a1a1a; padding: 15px; border-radius: 4px; white-space: pre-wrap; line-height: 1.6; font-family: monospace; font-size: 13px;">
{layer8_html}
</div>
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
        else:
            print(f"❌ Email failed: {response.status_code} — {response.text}")
    except Exception as e:
        print(f"❌ Email error: {e}")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# LAYER 8: IBKR PORTFOLIO AWARENESS
# ─────────────────────────────────────────────
import time
import xml.etree.ElementTree as ET

IBKR_TOKEN = os.environ.get("IBKR_TOKEN")
IBKR_QUERY_ID = os.environ.get("IBKR_QUERY_ID")

def get_layer8():
    """
    Pulls current IBKR positions via Flex Web Service.
    Two-step flow: (1) request report generation, (2) poll/retrieve the XML.
    Returns dict with positions list, flags, and summary data.
    """
    if not IBKR_TOKEN or not IBKR_QUERY_ID:
        return {
            "available": False,
            "error": "IBKR_TOKEN or IBKR_QUERY_ID not set in environment.",
            "positions": [],
            "flags": []
        }

    try:
        # STEP 1: Request report generation
        send_url = (
            f"https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
            f"?t={IBKR_TOKEN}&q={IBKR_QUERY_ID}&v=3"
        )
        send_resp = requests.get(send_url, timeout=30)
        send_root = ET.fromstring(send_resp.text)

        status = send_root.attrib.get("status") or send_root.findtext("Status")
        if status != "Success":
            error_msg = send_root.findtext("ErrorMessage") or "Unknown error requesting Flex report."
            return {
                "available": False,
                "error": f"Flex request failed: {error_msg}",
                "positions": [],
                "flags": []
            }

        reference_code = send_root.findtext("ReferenceCode")
        if not reference_code:
            return {
                "available": False,
                "error": "No ReferenceCode returned from IBKR.",
                "positions": [],
                "flags": []
            }

        # STEP 2: Poll for the report — IBKR needs a few seconds to generate it
        get_url = (
            f"https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
            f"?t={IBKR_TOKEN}&q={reference_code}&v=3"
        )

        report_xml = None
        max_attempts = 15
        for attempt in range(max_attempts):
            time.sleep(5)  # give IBKR time to generate the report
            get_resp = requests.get(get_url, timeout=30)

            # If the report isn't ready, IBKR returns a small XML with status "Warn"/"Fail"
            # If it IS ready, IBKR returns the full FlexQueryResponse XML (much larger)
            if "<FlexQueryResponse" in get_resp.text:
                report_xml = get_resp.text
                break
            else:
                # Check if it's a genuine error vs "still generating"
                try:
                    err_root = ET.fromstring(get_resp.text)
                    err_status = err_root.attrib.get("status") or err_root.findtext("Status")
                    if err_status == "Fail":
                        error_msg = err_root.findtext("ErrorMessage") or "Unknown error retrieving report."
                        return {
                            "available": False,
                            "error": f"Flex retrieval failed: {error_msg}",
                            "positions": [],
                            "flags": []
                        }
                except ET.ParseError:
                    pass
                continue

        if not report_xml:
            return {
                "available": False,
                "error": "Report did not become ready in time (timed out after 30s polling).",
                "positions": [],
                "flags": []
            }

        # STEP 3: Parse the actual positions XML
        root = ET.fromstring(report_xml)
        positions = []
        flags = []

        for pos in root.iter("OpenPosition"):
            symbol = pos.attrib.get("symbol", "")
            description = pos.attrib.get("description", "")
            asset_class = pos.attrib.get("assetCategory", "")
            currency = pos.attrib.get("currency", "")
            quantity = float(pos.attrib.get("position", 0) or 0)
            mark_price = float(pos.attrib.get("markPrice", 0) or 0)
            position_value = float(pos.attrib.get("positionValue", 0) or 0)
            open_price = float(pos.attrib.get("openPrice", 0) or 0)
            pct_nav = float(pos.attrib.get("percentOfNAV", 0) or 0)
            unrealized_pl = float(pos.attrib.get("fifoPnlUnrealized", 0) or 0)
            strike = pos.attrib.get("strike", "")
            expiry = pos.attrib.get("expiry", "")
            put_call = pos.attrib.get("putCall", "")

            entry = {
                "symbol": symbol,
                "description": description,
                "asset_class": asset_class,
                "currency": currency,
                "quantity": quantity,
                "mark_price": mark_price,
                "position_value": position_value,
                "open_price": open_price,
                "pct_nav": pct_nav,
                "unrealized_pl": unrealized_pl,
                "strike": strike,
                "expiry": expiry,
                "put_call": put_call,
            }
            positions.append(entry)

            # ── RISK FLAGS ──
            # Concentration: any single position over 15% of NAV
            if abs(pct_nav) > 15:
                flags.append(
                    f"⚠️ {symbol} is {pct_nav:.1f}% of NAV — concentration risk"
                )

            # Drawdown: unrealized loss greater than 10% of position value
            if open_price > 0 and mark_price > 0:
                pct_move = ((mark_price - open_price) / open_price) * 100
                if pct_move < -10:
                    flags.append(
                        f"⚠️ {symbol} is down {abs(pct_move):.1f}% from entry (mark {mark_price} vs open {open_price})"
                    )

        # Sort positions by absolute position value, largest first
        positions.sort(key=lambda p: abs(p["position_value"]), reverse=True)

        return {
            "available": True,
            "error": None,
            "positions": positions,
            "flags": flags,
            "total_positions": len(positions)
        }

    except Exception as e:
        return {
            "available": False,
            "error": f"Layer 8 error: {e}",
            "positions": [],
            "flags": []
        }


def format_layer8_for_email(layer8_data):
    """
    Formats Layer 8 output into a clean text block for the email report.
    """
    if not layer8_data["available"]:
        return f"📊 IBKR Portfolio: unavailable ({layer8_data['error']})"

    positions = layer8_data["positions"]
    flags = layer8_data["flags"]

    if not positions:
        return "📊 IBKR Portfolio: no open positions found."

    lines = ["📊 IBKR PORTFOLIO — CURRENT POSITIONS", ""]

    for p in positions:
        symbol_display = p["symbol"]
        if p["asset_class"] == "OPT" and p["strike"] and p["expiry"]:
            symbol_display += f" {p['strike']}{p['put_call']} {p['expiry']}"

        pl_sign = "+" if p["unrealized_pl"] >= 0 else ""
        lines.append(
            f"  {symbol_display:<25} {p['asset_class']:<6} "
            f"Qty: {p['quantity']:>10.2f}  "
            f"Value: {p['currency']} {p['position_value']:>12,.2f}  "
            f"% NAV: {p['pct_nav']:>5.1f}%  "
            f"P/L: {pl_sign}{p['unrealized_pl']:,.2f}"
        )

    lines.append("")
    if flags:
        lines.append("⚡ Portfolio Flags:")
        for f in flags:
            lines.append(f"  {f}")
    else:
        lines.append("✅ No concentration or drawdown flags.")

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

    print("[Layer 3c] Options sentiment (put/call ratio)...", flush=True)
    l3c = get_layer3c()
    print(f"  Score: {l3c['score']}/{l3c['max']} | Flags: {len(l3c['flags'])}", flush=True)

    print("[Layer 3d] SKEW index (tail-risk pricing)...", flush=True)
    l3d = get_layer3d()
    print(f"  Score: {l3d['score']}/{l3d['max']} | Flags: {len(l3d['flags'])}", flush=True)

    print("[Self-Test] Running sanity checks...", flush=True)
    sanity_warnings = run_sanity_checks(l1, l2, l3, l3b, l3c, l3d)
    if sanity_warnings:
        for w in sanity_warnings:
            print(f"  🚨 {w}", flush=True)
    else:
        print("  All checks passed.", flush=True)

    print("[Layer 4] Computing composite score...")
    score_data = compute_score(l1, l2, l3, l3b, l3c, l3d)
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

    all_flags = l1["flags"] + l2["flags"] + l3["flags"] + l3b["flags"] + l3d["flags"]
    flags_text = "\n".join(all_flags) if all_flags else "No flags raised."
    raw_data = {**l1.get("data", {}), **l2.get("data", {}), **l3.get("data", {}),
                **l3b.get("data", {}), **l3d.get("data", {})}
    data_text = json.dumps(raw_data, indent=2)

    run_full, mode_reason = should_run_full_research(score_data["signal"])
    boardroom_mode = "full" if run_full else "cheap"
    print(f"\n[Layer 5] Boardroom mode: {boardroom_mode} ({mode_reason})", flush=True)

    research = None
    glint_review = ""
    if run_full and ANTHROPIC_API_KEY:
        try:
            print("  Researching living members (live web search)...", flush=True)
            research = run_member_research(ANTHROPIC_API_KEY, score_data["signal"], data_text, flags_text)
            found = sum(1 for r in research if r["found"])
            print(f"  Recent public commentary found for {found}/{len(research)} living members.", flush=True)
            boardroom = run_full_boardroom(ANTHROPIC_API_KEY, score_data, data_text, flags_text, research)
            boardroom = f"[Full grounded run — {mode_reason}; recent commentary found for {found}/{len(research)} living members]\n\n" + boardroom
            try:
                glint_review = run_glint_review(ANTHROPIC_API_KEY, research, glint_results, score_data)
            except Exception as e:
                print(f"  ⚠️  Glint review failed: {e}", flush=True)
                glint_review = "Boardroom review of Glint candidates unavailable today (call failed)."
        except Exception as e:
            print(f"  ⚠️  Full boardroom failed ({e}) — falling back to cheap board.", flush=True)
            research = None
            boardroom = run_boardroom(score_data, l1, l2, l3)
            boardroom = "[Desk view — full grounded run FAILED today, this is the unresearched fallback]\n\n" + boardroom
    else:
        boardroom = run_boardroom(score_data, l1, l2, l3)
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
    trade_ideas = get_trade_ideas(score_data, l1, l2, l3, effective_signal=final_signal_data["signal"])
    print(trade_ideas)

    print("\n[Layer 8] Pulling IBKR portfolio...")
    layer8_data = get_layer8()
    layer8_html = format_layer8_for_email(layer8_data)
    print(layer8_html)

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

    print("\n[Layer 7] Sending email report...")
    send_email(score_data, l1, l2, l3, boardroom, trade_ideas, layer8_html, glint_html, sanity_warnings, final_signal_data, glint_review)

    print("\n" + "=" * 60)
    print("UNDERTOW INDEX — COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    main()
