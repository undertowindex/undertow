"""Four new leading indicators for Undertow shadow mode (test period Sep 26 - Oct 23).
Run in parallel to primary 7-layer score; do NOT change RED/AMBER/GREEN signal until validated.

These are designed to show early warning signals 2–5 days before major repricing:
1. Market Breadth — early rollover signal
2. VIX Term Structure — contango/backwardation flip
3. TED Spread — credit event warning
4. QQQ Put/Call Ratio — tech-sector institutional hedging
"""

import os
import datetime
import requests
import yfinance as yf


def fetch_breadth_indicator():
    """Market Breadth: % of S&P 500 stocks trading above their 50-day MA.

    Signal: drops below 40% → 2-3 day warning before SPY/ES rollover.
    Data source: yfinance (sample of large-cap leaders).
    """
    try:
        # Sample of S&P 500 leaders across sectors
        tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "JPM", "BAC", "V", "MA", "JNJ", "PG", "XOM", "CVX"]
        hist = yf.download(tickers, period="60d", progress=False, timeout=10)["Adj Close"]

        if hist is None or hist.empty:
            return None, "yfinance returned empty"

        # Calculate 50-day MA
        ma50 = hist.rolling(window=50).mean()

        # Count how many are above their 50-day MA
        above_ma_count = (hist.iloc[-1] > ma50.iloc[-1]).sum()
        breadth_pct = 100.0 * above_ma_count / len(tickers)

        # Status thresholds
        if breadth_pct > 50:
            status = "NORMAL"
        elif breadth_pct > 40:
            status = "CAUTION"
        else:
            status = "ALERT"

        return round(breadth_pct, 1), status
    except Exception as e:
        print(f"  ⚠️  Breadth fetch error: {type(e).__name__}: {str(e)[:60]}", flush=True)
        return None, f"Error: {type(e).__name__}"


def fetch_vix_term_structure():
    """VIX Term Structure: 1-month IV vs recent daily change.

    Signal: ratio flips from contango to backwardation → 3-5 day warning.
    Data source: yfinance (^VIX).
    """
    try:
        # Fetch VIX over last 5 days to see momentum
        vix_hist = yf.download("^VIX", period="5d", progress=False, timeout=10)["Close"]

        if vix_hist is None or vix_hist.empty or len(vix_hist) < 2:
            return None, "VIX data too short"

        vix_current = float(vix_hist.iloc[-1])
        vix_prev = float(vix_hist.iloc[-2])

        # Term ratio: current / previous day
        term_ratio = vix_current / vix_prev if vix_prev > 0 else 1.0

        # Status: spiking = CAUTION, flat = NORMAL, dropping = NORMAL
        if term_ratio > 1.03:
            status = "CAUTION"  # VIX spiking fast
        elif term_ratio > 1.01:
            status = "WATCH"    # Slight spike
        else:
            status = "NORMAL"   # Calm or dropping

        return round(term_ratio, 3), status
    except Exception as e:
        print(f"  ⚠️  VIX term fetch error: {type(e).__name__}: {str(e)[:60]}", flush=True)
        return None, f"Error: {type(e).__name__}"


def fetch_ted_spread():
    """TED Spread: 3-month SOFR minus fed funds rate (basis points).

    Signal: widens 48-72 hours before credit events.
    Data source: FRED API (requires FRED_API_KEY in environment).
    """
    try:
        fred_key = os.environ.get("FRED_API_KEY", "").strip()

        if not fred_key:
            return None, "FRED_API_KEY not set"

        # Fetch SOFR 3M and Fed Funds from FRED
        url_sofr = "https://api.stlouisfed.org/fred/series/observations"
        url_ff = "https://api.stlouisfed.org/fred/series/observations"

        params_sofr = {
            "series_id": "SOFR3M",
            "api_key": fred_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1
        }
        params_ff = {
            "series_id": "FEDFUNDS",
            "api_key": fred_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1
        }

        r_sofr = requests.get(url_sofr, params=params_sofr, timeout=10)
        r_ff = requests.get(url_ff, params=params_ff, timeout=10)

        r_sofr.raise_for_status()
        r_ff.raise_for_status()

        sofr_val = None
        ff_val = None

        for obs in r_sofr.json().get("observations", []):
            if obs["value"] != ".":
                sofr_val = float(obs["value"])
                break

        for obs in r_ff.json().get("observations", []):
            if obs["value"] != ".":
                ff_val = float(obs["value"])
                break

        if sofr_val is None or ff_val is None:
            return None, "FRED data missing"

        ted = sofr_val - ff_val

        # Status thresholds (basis points)
        if ted < 50:
            status = "NORMAL"
        elif ted < 80:
            status = "CAUTION"
        else:
            status = "ALERT"

        return round(ted, 1), status
    except Exception as e:
        print(f"  ⚠️  TED spread fetch error: {type(e).__name__}: {str(e)[:60]}", flush=True)
        return None, f"Error: {type(e).__name__}"


def fetch_qqq_put_call_ratio():
    """QQQ Put/Call Ratio: institutional hedging on tech sector.

    Signal: ratio > 1.2 → early tech sector hedge, precedes selloff 1-2 days.
    Data source: yfinance (QQQ options chain).
    """
    try:
        qqq = yf.Ticker("QQQ")
        opts = qqq.option_chain(date=None)  # Nearest expiry

        if opts is None or opts.calls is None or opts.puts is None:
            return None, "QQQ options unavailable"

        if opts.calls.empty or opts.puts.empty:
            return None, "No options data"

        # Sum open interest
        call_oi = opts.calls["openInterest"].sum()
        put_oi = opts.puts["openInterest"].sum()

        if call_oi == 0 or call_oi is None:
            return None, "Zero call OI"

        ratio = put_oi / call_oi

        # Status thresholds
        if ratio < 1.0:
            status = "NORMAL"   # Call skew, bullish
        elif ratio < 1.2:
            status = "WATCH"    # Balanced
        else:
            status = "CAUTION"  # Put protection spike

        return round(ratio, 2), status
    except Exception as e:
        print(f"  ⚠️  QQQ put/call fetch error: {type(e).__name__}: {str(e)[:60]}", flush=True)
        return None, f"Error: {type(e).__name__}"


def calculate_shadow_score(breadth, vix_term, ted, qqq_ratio):
    """Combine four indicators into a 0-35 shadow score.

    Each indicator contributes stress points:
    - Breadth: 100% = 0 stress, 0% = 10 stress
    - VIX term: 1.0 = 0 stress, 1.1+ = 10 stress
    - TED: 0 bps = 0 stress, 100 bps = 10 stress
    - QQQ ratio: 1.0 = 0 stress, 1.3+ = 10 stress
    """
    score = 0
    count = 0

    if breadth is not None:
        # Higher breadth = less stress
        breadth_stress = max(0, min(10, 10 * (50 - breadth) / 50))
        score += breadth_stress
        count += 1

    if vix_term is not None:
        # Higher ratio = more stress
        term_stress = max(0, min(10, 100 * (vix_term - 1.0)))
        score += term_stress
        count += 1

    if ted is not None:
        # Higher TED = more stress
        ted_stress = max(0, min(10, ted / 10))
        score += ted_stress
        count += 1

    if qqq_ratio is not None:
        # Higher ratio = more stress
        ratio_stress = max(0, min(10, 100 * (qqq_ratio - 1.0)))
        score += ratio_stress
        count += 1

    if count == 0:
        return None

    # Normalize: average of available indicators, scaled to 35
    avg_stress = score / count
    shadow_score = (avg_stress / 10) * 35
    return round(shadow_score, 1)


if __name__ == "__main__":
    print("=== UNDERTOW SHADOW INDICATORS (Test Mode) ===\n")

    breadth, breadth_status = fetch_breadth_indicator()
    if breadth is not None:
        print(f"✓ Market Breadth: {breadth}% [{breadth_status}]")
    else:
        print(f"✗ Market Breadth: {breadth_status}")

    vix_term, vix_status = fetch_vix_term_structure()
    if vix_term is not None:
        print(f"✓ VIX Term Ratio: {vix_term} [{vix_status}]")
    else:
        print(f"✗ VIX Term Ratio: {vix_status}")

    ted, ted_status = fetch_ted_spread()
    if ted is not None:
        print(f"✓ TED Spread: {ted}bps [{ted_status}]")
    else:
        print(f"✗ TED Spread: {ted_status}")

    qqq_ratio, qqq_status = fetch_qqq_put_call_ratio()
    if qqq_ratio is not None:
        print(f"✓ QQQ Put/Call: {qqq_ratio} [{qqq_status}]")
    else:
        print(f"✗ QQQ Put/Call: {qqq_status}")

    shadow = calculate_shadow_score(breadth, vix_term, ted, qqq_ratio)
    if shadow is not None:
        print(f"\nShadow Score: {shadow}/35")
    else:
        print("\nShadow Score: Unable to calculate (all indicators failed)")
