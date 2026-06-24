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
def compute_score(l1, l2, l3):
    total = l1["score"] + l2["score"] + l3["score"]

    if total <= 3:
        signal = "GREEN"
        emoji = "🟢"
        summary = "Markets calm. No significant stress signals detected."
    elif total <= 7:
        signal = "AMBER"
        emoji = "🟡"
        summary = "Elevated risk. Multiple stress signals present. Watch closely."
    else:
        signal = "RED"
        emoji = "🔴"
        summary = "High alert. Significant macro stress across multiple indicators."

    return {"score": total, "max": 12, "signal": signal, "emoji": emoji, "summary": summary}

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
- Score: {score_data['score']}/12
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

HISTORICAL GHOSTS:
8. Jesse Livermore — tape reading, market psychology
9. Benjamin Graham — margin of safety, intrinsic value
10. Sir John Templeton — contrarian global value
11. Charlie Munger — mental models, concentrated bets
12. André Kostolany — European macro, sentiment cycles

Each member should give:
- A 1-2 sentence view in their authentic voice
- A vote: CONFIRM {score_data['signal']} or UPGRADE (more severe) or DOWNGRADE (less severe)

Then give a BOARDROOM VERDICT:
- Final consensus signal (GREEN / AMBER / RED)
- 2-3 sentence synthesis of why
- Confidence level (Low / Medium / High)

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
                "max_tokens": 2000,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
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

    prompt = f"""You are Michael Burry's trading desk AI. Current Undertow signal: {signal} ({score}/12).

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
def send_email(score_data, l1, l2, l3, boardroom, trade_ideas):
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
  <h2 style="margin: 0; color: {signal_color};">Score: {score}/12</h2>
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
                "subject": f"{emoji} Undertow Index — {signal} ({score}/12) — {datetime.datetime.now().strftime('%d %b %Y')}",
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

    print("[Layer 4] Computing composite score...")
    score_data = compute_score(l1, l2, l3)
    print(f"\n  {score_data['emoji']} SIGNAL: {score_data['signal']} ({score_data['score']}/12)")
    print(f"  {score_data['summary']}")

    for flag in l1["flags"] + l2["flags"] + l3["flags"]:
        print(f"  ⚠️  {flag}")

    print("\n[Layer 5] Running The Boardroom...")
    boardroom = run_boardroom(score_data, l1, l2, l3)
    print(boardroom)

    print("\n[Layer 6] Generating trade ideas...")
    trade_ideas = get_trade_ideas(score_data, l1, l2, l3)
    print(trade_ideas)

    print("\n[Layer 7] Sending email report...")
    send_email(score_data, l1, l2, l3, boardroom, trade_ideas)

    print("\n" + "=" * 60)
    print("UNDERTOW INDEX — COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    main()
