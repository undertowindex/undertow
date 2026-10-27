"""Direct-from-publisher data feeds for Layers 8 (SOFR) and 9 (NFCI/ANFCI).

Why this exists: FRED (api.stlouisfed.org) is blocked from Railway's network,
and the old code then silently substituted invented values (SOFR = Fed target,
volume = 3000B, NFCI = -0.20) that scored as calm and showed as LIVE.

Rules for everything in here:
  * Return real observations or None. Never invent a value.
  * Only LIVE fetches are written to the cache. A cached value is always
    labelled as cached and expires (SOFR 5 days, NFCI 14 days).
  * Each fetcher returns the source name so the email can say where the
    number came from.
"""
import csv
import datetime
import io
import json
import os
import tempfile

import requests

NYFED_SOFR_URL = "https://markets.newyorkfed.org/api/rates/secured/sofr/last/5.json"
CHICAGOFED_NFCI_URL = ("https://www.chicagofed.org/-/media/publications/nfci/"
                       "nfci-data-series-csv.csv")
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

SOFR_CACHE_MAX_AGE_DAYS = 5
NFCI_CACHE_MAX_AGE_DAYS = 14

_CACHE_DIR = os.environ.get("UNDERTOW_CACHE_DIR") or tempfile.gettempdir()
# "v2" file names on purpose: older caches may contain the invented fallback
# values the previous code wrote, and must never be read back as real data.
SOFR_CACHE = os.path.join(_CACHE_DIR, "undertow_sofr_cache_v2.json")
NFCI_CACHE = os.path.join(_CACHE_DIR, "undertow_nfci_cache_v2.json")


def _get(url, params=None, timeout=10, attempts=2):
    """GET with one retry. Returns (response or None, error text or None)."""
    last_err = None
    for _ in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "undertow/1.0"})
            if r.status_code == 200:
                return r, None
            last_err = f"HTTP {r.status_code}"
        except Exception as e:  # network, proxy, DNS, timeout
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
    return None, last_err


def _fred_obs(series_id, key, limit=3, timeout=8):
    r, err = _get(FRED_URL, {"series_id": series_id, "api_key": key, "file_type": "json",
                             "sort_order": "desc", "limit": limit},
                  timeout=timeout, attempts=1)
    if r is None:
        return None, err
    try:
        out = []
        for o in r.json().get("observations", []):
            if o.get("value") not in (None, "", "."):
                out.append({"date": o.get("date", ""), "value": float(o["value"])})
        return (out or None), (None if out else "no observations")
    except Exception as e:
        return None, f"parse error: {e}"


# ── SOFR ─────────────────────────────────────────────────────────────

def fetch_sofr(fred_key=""):
    """Latest overnight SOFR. Returns (data dict or None, errors list).

    data = {"rate": pct, "volume_b": billions or None, "date": ISO, "source": str}
    The NY Fed's own API is tried first, then FRED's overnight "SOFR" series.
    Both are overnight SOFR, which is the series the scoring rule was tested on.
    """
    errors = []
    r, err = _get(NYFED_SOFR_URL)
    if r is None:
        errors.append(f"NY Fed SOFR: {err}")
    else:
        try:
            for row in r.json().get("refRates", []):  # newest first
                if row.get("type", "SOFR") != "SOFR" or row.get("percentRate") is None:
                    continue
                vol = row.get("volumeInBillions")
                return ({"rate": float(row["percentRate"]),
                         "date": row.get("effectiveDate", ""),
                         "volume_b": float(vol) if vol is not None else None,
                         "source": "NY Fed (overnight SOFR)"}, errors)
            errors.append("NY Fed SOFR: no usable rows")
        except Exception as e:
            errors.append(f"NY Fed SOFR parse error: {e}")

    if fred_key:
        rate, err = _fred_obs("SOFR", fred_key, limit=2)
        if rate:
            vol, _ = _fred_obs("SOFRVOL", fred_key, limit=2)
            return ({"rate": rate[0]["value"], "date": rate[0]["date"],
                     "volume_b": vol[0]["value"] if vol else None,
                     "source": "FRED (overnight SOFR)"}, errors)
        errors.append(f"FRED SOFR: {err}")
    else:
        errors.append("FRED SOFR: no API key")
    return None, errors


def sofr_spread_score(sofr_pct, range_top_pct):
    """Score funding stress as SOFR's distance above the TOP of the Fed's target range.

    Backtested on real NY Fed data (Sep 2019, Mar 2023, Oct-Nov 2025, Dec 2025,
    Sep-Oct 2026): flags the 2019 and Oct-2025 squeezes, stays at 0 on normal days.
    Returns (score 0-5, status, spread_bp).
    """
    bp = (sofr_pct - range_top_pct) * 100
    if bp > 30:
        return 5, "STRESS", bp
    if bp > 15:
        return 2, "CAUTION", bp
    if bp > 5:
        return 1, "WATCH", bp
    return 0, "NORMAL", bp


# ── NFCI / ANFCI ─────────────────────────────────────────────────────

def _parse_chicago_csv(text, keep=3):
    """Chicago Fed CSV -> newest-first [{date, nfci, anfci}], bad rows skipped."""
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        try:
            d = datetime.datetime.strptime(rec["Friday_of_Week"].strip(), "%m/%d/%Y")
            rows.append({"date": d.strftime("%Y-%m-%d"),
                         "nfci": float(rec["NFCI"]), "anfci": float(rec["ANFCI"])})
        except Exception:
            continue  # truncated or malformed row
    rows.sort(key=lambda x: x["date"], reverse=True)
    return rows[:keep]


def fetch_nfci(fred_key=""):
    """Latest NFCI + ANFCI. Returns (data dict or None, errors list).

    data = {"rows": [{date, nfci, anfci}, ...newest first], "source": str}
    The Chicago Fed's own CSV carries both indices from one publication, so
    they are always the same week. FRED is the backup.
    """
    errors = []
    r, err = _get(CHICAGOFED_NFCI_URL, timeout=15)
    if r is not None:
        rows = _parse_chicago_csv(r.text)
        if rows:
            return {"rows": rows, "source": "Chicago Fed (direct)"}, errors
        errors.append("Chicago Fed NFCI: no parsable rows")
    else:
        errors.append(f"Chicago Fed NFCI: {err}")

    if fred_key:
        n, e1 = _fred_obs("NFCI", fred_key)
        a, e2 = _fred_obs("ANFCI", fred_key)
        if n:
            amap = {o["date"]: o["value"] for o in (a or [])}
            rows = [{"date": o["date"], "nfci": o["value"], "anfci": amap.get(o["date"])}
                    for o in n]
            return {"rows": rows, "source": "FRED"}, errors
        errors.append(f"FRED NFCI: {e1}")
    return None, errors


# ── cache (live data only) ───────────────────────────────────────────

def write_cache(path, payload):
    try:
        with open(path, "w") as f:
            json.dump({"saved_utc": datetime.datetime.utcnow().isoformat(),
                       "payload": payload}, f)
    except Exception:
        pass  # caching is best-effort


def read_cache(path, max_age_days):
    """Return (payload, age_days) or (None, None) if absent, unreadable or expired."""
    try:
        with open(path) as f:
            blob = json.load(f)
        saved = datetime.datetime.fromisoformat(blob["saved_utc"])
        age = (datetime.datetime.utcnow() - saved).total_seconds() / 86400
        if age > max_age_days:
            return None, None
        return blob["payload"], age
    except Exception:
        return None, None
