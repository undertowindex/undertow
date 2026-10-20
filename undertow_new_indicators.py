"""Shadow indicators for Undertow (test period Sep 26 - Oct 23).

Only indicators with a genuine measurement are computed here. An indicator
that cannot be measured honestly returns (None, "UNAVAILABLE: ...") so the
email shows it as unavailable instead of a made-up reading.

Audit fix (2026-10-20):
- Market breadth: UNAVAILABLE. The old version tested 5 mega-caps against a
  price > $100 threshold, which is not breadth. A real version needs S&P 500
  constituents and their 50-day moving averages.
- VIX term structure: UNAVAILABLE. The old version set the previous VIX to
  0.95 x today's, so the ratio was always 1.0526. A real version needs VIX3M
  or VIX futures.
- QQQ put/call: UNAVAILABLE. The old version was derived from VIX. A real
  version needs QQQ option volume.
- TED spread: computed from FRED in percentage points and converted to basis
  points. Not used in the production email.
"""

import os
import requests


UNAVAILABLE_BREADTH = "UNAVAILABLE: needs S&P 500 constituent data (not measured)"
UNAVAILABLE_VIX_TERM = "UNAVAILABLE: needs VIX3M or VIX futures (not measured)"
UNAVAILABLE_QQQ = "UNAVAILABLE: needs QQQ option volume (not measured)"


def fetch_breadth_indicator():
    """Market breadth (% of S&P 500 above 50-day MA). Not measured yet."""
    return None, UNAVAILABLE_BREADTH


def fetch_vix_term_structure():
    """VIX term structure (VIX / VIX3M or VX futures). Not measured yet."""
    return None, UNAVAILABLE_VIX_TERM


def fetch_ted_spread():
    """TED-style spread: SOFR minus fed funds, in basis points.

    FRED reports both series in percent, so the difference is a percentage-point
    value and is multiplied by 100 to get basis points before thresholds apply.
    Data source: FRED API (requires FRED_API_KEY in environment).
    """
    try:
        fred_key = os.environ.get("FRED_API_KEY", "").strip()

        if not fred_key:
            return None, "FRED_API_KEY not set"

        url = "https://api.stlouisfed.org/fred/series/observations"

        def latest(series_id):
            params = {
                "series_id": series_id,
                "api_key": fred_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 5,
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            for obs in resp.json().get("observations", []):
                if obs["value"] != ".":
                    return float(obs["value"])
            return None

        sofr_val = latest("SOFR")
        ff_val = latest("DFF")

        if sofr_val is None or ff_val is None:
            return None, "FRED data missing"

        ted_pp = sofr_val - ff_val          # percentage points
        ted_bp = ted_pp * 100.0             # basis points

        if ted_bp < 50:
            status = "NORMAL"
        elif ted_bp < 80:
            status = "CAUTION"
        else:
            status = "ALERT"

        return round(ted_bp, 1), status
    except Exception as e:
        print(f"  ⚠️  TED spread fetch error: {type(e).__name__}: {str(e)[:60]}", flush=True)
        return None, f"Error: {type(e).__name__}"


def fetch_qqq_put_call_ratio():
    """QQQ put/call ratio from option volume. Not measured yet."""
    return None, UNAVAILABLE_QQQ


def calculate_shadow_score(breadth, vix_term, ted, qqq_ratio):
    """Combine available shadow indicators into a 0-35 score.

    Each indicator contributes stress points:
    - Breadth: 100% = 0 stress, 0% = 10 stress
    - VIX term: 1.0 = 0 stress, 1.1+ = 10 stress
    - TED: 0 bp = 0 stress, 100 bp = 10 stress
    - QQQ ratio: 1.0 = 0 stress, 1.3+ = 10 stress

    Not used in the production email while the inputs are unavailable.
    """
    score = 0
    count = 0

    if breadth is not None:
        breadth_stress = max(0, min(10, 10 * (50 - breadth) / 50))
        score += breadth_stress
        count += 1

    if vix_term is not None:
        term_stress = max(0, min(10, 100 * (vix_term - 1.0)))
        score += term_stress
        count += 1

    if ted is not None:
        ted_stress = max(0, min(10, ted / 10))
        score += ted_stress
        count += 1

    if qqq_ratio is not None:
        ratio_stress = max(0, min(10, 100 * (qqq_ratio - 1.0)))
        score += ratio_stress
        count += 1

    if count == 0:
        return None

    avg_stress = score / count
    shadow_score = (avg_stress / 10) * 35
    return round(shadow_score, 1)


if __name__ == "__main__":
    print("=== UNDERTOW SHADOW INDICATORS (Test Mode) ===\n")

    breadth, breadth_status = fetch_breadth_indicator()
    print(f"{'✓' if breadth is not None else '✗'} Market Breadth: {breadth if breadth is not None else breadth_status}")

    vix_term, vix_status = fetch_vix_term_structure()
    print(f"{'✓' if vix_term is not None else '✗'} VIX Term Ratio: {vix_term if vix_term is not None else vix_status}")

    ted, ted_status = fetch_ted_spread()
    if ted is not None:
        print(f"✓ TED Spread: {ted}bp [{ted_status}]")
    else:
        print(f"✗ TED Spread: {ted_status}")

    qqq_ratio, qqq_status = fetch_qqq_put_call_ratio()
    print(f"{'✓' if qqq_ratio is not None else '✗'} QQQ Put/Call: {qqq_ratio if qqq_ratio is not None else qqq_status}")

    shadow = calculate_shadow_score(breadth, vix_term, ted, qqq_ratio)
    if shadow is not None:
        print(f"\nShadow Score: {shadow}/35")
    else:
        print("\nShadow Score: Unable to calculate (no measured inputs)")
