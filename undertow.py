import os
import json
import datetime
import requests
import yfinance as yf

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
RESEND_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "micahbrown4@me.com")

# ─────────────────────────────────────────────
# LAYER 1: EQUITY PULSE
# ─────────────────────────────────────────────
def get_layer1():
    try:
        tickers = yf.download("SPY RSP QQQ ^VIX NVDA", period="5d", interval="1d", progress=False)
        close = tickers["Close"]

        spy = float(close["SPY"].dropna().iloc[-1])
        spy_prev = float(close["SPY"].dropna().iloc[-2])
        rsp = float(close["RSP"].dropna().iloc[-1])
        rsp_prev = float(close["RSP"].dropna().iloc[-2])
        nvda = float(close["NVDA"].dropna().iloc[-1])
        nvda_prev = float(close["NVDA"].dropna().iloc[-2])
        vix = float(close["^VIX"].dropna().iloc[-1])

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
                "RSP_SPY_ratio_chg": round(ratio_chg, 2)
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
        def fred(series):
            url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series}&api_key={FRED_API_KEY}&limit=5&sort_order=desc&file_type=json"
            r = requests.get(url, timeout=10)
            obs = r.json()["observations"]
            return float(obs[0]["value"])

        t10 = fred("DGS10")
        t2 = fred("DGS2")
        spread = t10 - t2
        data["yield_curve_spread"] = round(spread, 3)

        if spread < 0:
            score += 2
            flags.append(f"Yield curve inverted: 10Y-2Y = {spread:.3f}% (recession signal)")
        elif spread < 0.3:
            score += 1
            flags.append(f"Yield curve flat: 10Y-2Y = {spread:.3f}%")

        hy = fred("BAMLH0A0HYM2")
        data["hy_spread"] = round(hy, 3)

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
        tickers = yf.download("JPY=X GC=F HG=F", period="5d", interval="1d", progress=False)
        close = tickers["Close"]

        yen = float(close["JPY=X"].dropna().iloc[-1])
        yen_prev = float(close["JPY=X"].dropna().iloc[-2])
        yen_chg = (yen - yen_prev) / yen_prev * 100

        gold = float(close["GC=F"].dropna().iloc[-1])
        copper = float(close["HG=F"].dropna().iloc[-1])
        copper_gold = copper / gold
        data["yen"] = round(yen, 4)
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

    except Exception as e:
        flags.append(f"Layer 3 error: {e}")

    return {"score": score, "max": 4, "flags": flags, "data": data}

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
        cot_url = "https://publicreporting.cftc.gov/resource/jun7-fc8e.json"
        cot_params = {
            "$where": "contract_market_name like '%E-MINI S%P 500%'",
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
            if net_pos < 0:
                score += 1
                flags.append(f"COT: leveraged funds net SHORT E-mini S&P ({net_pos:,.0f} contracts)")
        else:
            flags.append("COT: no data returned")
    except Exception as e:
        flags.append(f"Layer 3b COT error: {e}")

    try:
        def fred_latest(series_id):
            url = "https://api.stlouisfed.org/fred/series/observations"
            params = {"series_id": series_id, "api_key": FRED_API_KEY,
                      "file_type": "json", "sort_order": "desc", "limit": 5}
            r = requests.get(url, params=params, timeout=10)
            for o in r.json()["observations"]:
                if o["value"] != ".":
                    return float(o["value"])
            return None

        sofr = fred_latest("SOFR")
        dff = fred_latest("DFF")
        rrp = fred_latest("RRPONTSYD")
        dgs3mo = fred_latest("DGS3MO")

        if sofr is not None and dff is not None:
            spread = sofr - dff
            data["sofr_dff_spread"] = round(spread, 3)
            if spread > 0.10:
                score += 2
                flags.append(f"SOFR-Fed Funds spread widening ({spread:.2f}pp) - repo stress")
            elif spread > 0.05:
                score += 1
                flags.append(f"SOFR-Fed Funds spread elevated ({spread:.2f}pp)")

        if rrp is not None:
            data["reverse_repo_bn"] = round(rrp, 1)
            if rrp > 100:
                score += 1
                flags.append(f"Reverse repo usage spiking (${rrp:.0f}B)")

        if sofr is not None and dgs3mo is not None:
            ted = dgs3mo - sofr
            data["ted_spread_equiv"] = round(ted, 3)
            if ted < -0.15:
                score += 1
                flags.append(f"TED-equivalent spread inverted ({ted:.2f}pp) - funding stress")
    except Exception as e:
        flags.append(f"Layer 3b repo/TED error: {e}")

    return {"score": score, "max": 4, "flags": flags, "data": data}

def compute_score(l1, l2, l3, l3b):
    total = l1["score"] + l2["score"] + l3["score"] + l3b["score"]

    if total <= 4:
        signal = "GREEN"
        emoji = "🟢"
        summary = "Markets calm. No significant stress signals detected."
    elif total <= 10:
        signal = "AMBER"
        emoji = "🟡"
        summary = "Elevated risk. Multiple stress signals present. Watch closely."
    else:
        signal = "RED"
        emoji = "🔴"
        summary = "High alert. Significant macro stress across multiple indicators."

    return {"score": total, "max": 16, "signal": signal, "emoji": emoji, "summary": summary}

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
- Score: {score_data['score']}/16
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
8. Lee Robinson — event-driven macro, credit dislocations
9. Nassim Taleb — tail risk, fragility, black swans
10. Jim Rogers — global commodities, long-cycle macro
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
- A vote: CONFIRM {score_data['signal']} or UPGRADE (more severe) or DOWNGRADE (less severe)

Then give a BOARDROOM VERDICT:
- Final consensus signal (GREEN / AMBER / RED)
- 2-3 sentence synthesis of why
- Confidence level (Low / Medium / High)

CRITICAL: There are exactly 17 members listed above. Each member must appear exactly once - do not repeat any member's name in the panel discussion or in the vote tally, and do not invent additional members. Before writing the BOARDROOM VERDICT vote tally, re-count the panel section you just wrote: the CONFIRM + UPGRADE + DOWNGRADE vote counts MUST sum to exactly 17. Recheck this arithmetic before outputting the table.

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

# ─────────────────────────────────────────────
# LAYER 6: TRADE IDEAS
# ─────────────────────────────────────────────
def get_trade_ideas(score_data, l1, l2, l3):
    if not ANTHROPIC_API_KEY:
        return "Trade ideas unavailable — no API key."

    signal = score_data["signal"]
    score = score_data["score"]
    all_flags = l1["flags"] + l2["flags"] + l3["flags"]
    flags_text = "\n".join(all_flags) if all_flags else "No flags."

    prompt = f"""You are Michael Burry's trading desk AI. Current Undertow signal: {signal} ({score}/16).

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
def send_email(score_data, l1, l2, l3, boardroom, trade_ideas, layer8_html=""):
    if not RESEND_API_KEY:
        print("No Resend key — skipping email.")
        return

    date_str = datetime.datetime.now().strftime("%A %d %B %Y, %H:%M UTC")
    signal = score_data["signal"]
    emoji = score_data["emoji"]
    score = score_data["score"]

    all_flags = l1["flags"] + l2["flags"] + l3["flags"]
    flags_html = "".join(f"<li>{f}</li>" for f in all_flags) if all_flags else "<li>No flags</li>"
    signal_color = {"GREEN": "#2ecc71", "AMBER": "#f39c12", "RED": "#e74c3c"}.get(signal, "#999")

    html = f"""
<html><body style="font-family: Arial, sans-serif; max-width: 700px; margin: auto; background: #0d0d0d; color: #e0e0e0; padding: 20px;">
<h1 style="color: {signal_color}; border-bottom: 2px solid {signal_color}; padding-bottom: 10px;">
  {emoji} UNDERTOW INDEX — {signal}
</h1>
<p style="color: #aaa;">{date_str}</p>
<div style="background: #1a1a1a; border-left: 4px solid {signal_color}; padding: 15px; margin: 20px 0; border-radius: 4px;">
  <h2 style="margin: 0; color: {signal_color};">Score: {score}/16</h2>
  <p style="margin: 8px 0 0 0;">{score_data['summary']}</p>
</div>
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
                "to": [ALERT_EMAIL],
                "subject": f"{emoji} Undertow Index — {signal} ({score}/16) — {datetime.datetime.now().strftime('%d %b %Y')}",
                "html": html
            },
            timeout=15
        )
        if response.status_code == 200:
            print(f"✅ Email sent to {ALERT_EMAIL}")
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

    print("[Layer 4] Computing composite score...")
    score_data = compute_score(l1, l2, l3, l3b)
    print(f"\n  {score_data['emoji']} SIGNAL: {score_data['signal']} ({score_data['score']}/16)")
    print(f"  {score_data['summary']}")

    for flag in l1["flags"] + l2["flags"] + l3["flags"]:
        print(f"  ⚠️  {flag}")

    print("\n[Layer 5] Running The Boardroom...")
    boardroom = run_boardroom(score_data, l1, l2, l3)
    print(boardroom)

    print("\n[Layer 6] Generating trade ideas...")
    trade_ideas = get_trade_ideas(score_data, l1, l2, l3)
    print(trade_ideas)

    print("\n[Layer 8] Pulling IBKR portfolio...")
    layer8_data = get_layer8()
    layer8_html = format_layer8_for_email(layer8_data)
    print(layer8_html)

    print("\n[Layer 7] Sending email report...")
    send_email(score_data, l1, l2, l3, boardroom, trade_ideas, layer8_html)

    print("\n" + "=" * 60)
    print("UNDERTOW INDEX — COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    main()
