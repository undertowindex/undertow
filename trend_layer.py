"""
Layer 10: Trend & Momentum (early-warning channel).

Why this exists
---------------
Layers 1-9 are snapshots: Layer 1 downloads 5 days of prices and scores today's
level. Nothing in the system looked at drawdowns, moving averages, tech/breadth
underperformance or how fast VIX was rising, so a fall that builds over days or
weeks was invisible until the level-based thresholds finally tripped.

Design rules
------------
* Pure functions on a DataFrame of daily closes. The live run and the backtest
  (backtest_trend.py) call exactly the same code, so what was tested is what runs.
* Uses only Yahoo Finance prices (no FRED), so it works on Railway where FRED is
  blocked.
* A component whose inputs are missing is UNAVAILABLE - it is never scored as 0 -
  and the layer reports how much of its scale it could actually verify.
* All thresholds live in the constants below. They are STARTING VALUES, chosen to
  be conventional, and have NOT been validated. Run backtest_trend.py to test them
  before relying on them.
"""

import numpy as np
import pandas as pd

# ---- Thresholds (provisional - see backtest_trend.py) ----------------------
SPY_DD_WARN, SPY_DD_STRESS = 0.03, 0.06        # drawdown from 252d high
VIX_SPIKE_WARN, VIX_SPIKE_STRESS = 0.15, 0.30  # VIX vs its own 20d average
QQQ_REL_WARN, QQQ_REL_STRESS = -0.02, -0.04    # QQQ/SPY 20d change
QQQ_DD_WARN, QQQ_DD_STRESS = 0.07, 0.12        # QQQ drawdown from 252d high
BREADTH_WARN, BREADTH_STRESS = -0.015, -0.03   # RSP/SPY 20d change
CREDIT_WARN, CREDIT_STRESS = -0.01, -0.02      # HYG/IEF 20d change
RATES_WARN_BP, RATES_STRESS_BP = 30, 50        # 10Y yield rise over 20d, in bp

AMBER_POINTS = 3   # headline floor: AMBER at or above this many points
RED_POINTS = 6     # headline floor: RED at or above this many points
MIN_COMPONENTS = 5 # fewer verifiable components than this => cannot say GREEN
HOLD_DAYS = 5      # a warning persists this many trading days (stops one-day bounces resetting it)

COMPONENT_MAX = {
    "spy_drawdown": 2, "spy_below_50d": 1, "spy_below_200d": 1, "vix_spike": 2,
    "qqq_vs_spy": 2, "qqq_drawdown": 2, "breadth_rsp_spy": 2,
    "credit_hyg_ief": 2, "rates_10y": 2,
}
LAYER_MAX = sum(COMPONENT_MAX.values())  # 16

TICKERS = ["SPY", "QQQ", "RSP", "HYG", "IEF", "^VIX", "^TNX"]


def compute_features(closes: pd.DataFrame) -> pd.DataFrame:
    """Daily features from a DataFrame of closes (columns = tickers)."""
    c = closes.sort_index().ffill(limit=3)
    f = pd.DataFrame(index=c.index)

    def has(*cols):
        return all(col in c.columns and c[col].notna().any() for col in cols)

    if has("SPY"):
        s = c["SPY"]
        f["spy_dd"] = 1 - s / s.rolling(252, min_periods=60).max()
        f["spy_vs_sma50"] = s / s.rolling(50, min_periods=50).mean() - 1
        f["spy_vs_sma200"] = s / s.rolling(200, min_periods=200).mean() - 1
    if has("QQQ"):
        q = c["QQQ"]
        f["qqq_dd"] = 1 - q / q.rolling(252, min_periods=60).max()
    if has("QQQ", "SPY"):
        r = c["QQQ"] / c["SPY"]
        f["qqq_rel20"] = r / r.shift(20) - 1
    if has("RSP", "SPY"):
        r = c["RSP"] / c["SPY"]
        f["rsp_rel20"] = r / r.shift(20) - 1
    if has("HYG", "IEF"):
        r = c["HYG"] / c["IEF"]
        f["credit20"] = r / r.shift(20) - 1
    if has("^VIX"):
        v = c["^VIX"]
        f["vix"] = v
        f["vix_vs_sma20"] = v / v.rolling(20, min_periods=20).mean() - 1
    if has("^TNX"):
        t = c["^TNX"]
        if t.dropna().median() > 15:   # some Yahoo feeds quote yield x10
            t = t / 10.0
        f["tnx_chg20_bp"] = (t - t.shift(20)) * 100
    return f


def _tier(series, warn, stress, higher_is_worse=True):
    """0/1/2 points from a feature; NaN where the feature is unavailable."""
    if series is None:
        return None
    if higher_is_worse:
        pts = np.where(series >= stress, 2, np.where(series >= warn, 1, 0))
    else:
        pts = np.where(series <= stress, 2, np.where(series <= warn, 1, 0))
    return pd.Series(pts, index=series.index, dtype=float).where(series.notna())


def score_components(f: pd.DataFrame) -> pd.DataFrame:
    """Per-day points for each component (NaN = unavailable that day)."""
    g = lambda col: f[col] if col in f.columns else None
    comps = {
        "spy_drawdown": _tier(g("spy_dd"), SPY_DD_WARN, SPY_DD_STRESS),
        "vix_spike": _tier(g("vix_vs_sma20"), VIX_SPIKE_WARN, VIX_SPIKE_STRESS),
        "qqq_vs_spy": _tier(g("qqq_rel20"), QQQ_REL_WARN, QQQ_REL_STRESS, False),
        "qqq_drawdown": _tier(g("qqq_dd"), QQQ_DD_WARN, QQQ_DD_STRESS),
        "breadth_rsp_spy": _tier(g("rsp_rel20"), BREADTH_WARN, BREADTH_STRESS, False),
        "credit_hyg_ief": _tier(g("credit20"), CREDIT_WARN, CREDIT_STRESS, False),
        "rates_10y": _tier(g("tnx_chg20_bp"), RATES_WARN_BP, RATES_STRESS_BP),
    }
    for name, col in (("spy_below_50d", "spy_vs_sma50"), ("spy_below_200d", "spy_vs_sma200")):
        x = g(col)
        comps[name] = None if x is None else (x < 0).astype(float).where(x.notna())
    out = pd.DataFrame({k: v for k, v in comps.items() if v is not None}, index=f.index)
    for k in COMPONENT_MAX:               # keep every column, all-NaN if no data
        if k not in out.columns:
            out[k] = np.nan
    return out[list(COMPONENT_MAX)]


def score_series(closes: pd.DataFrame) -> pd.DataFrame:
    """Points / verifiable scale / state for every day (used by the backtest)."""
    comps = score_components(compute_features(closes))
    avail = comps.notna()
    maxes = pd.Series(COMPONENT_MAX)
    res = pd.DataFrame(index=comps.index)
    res["points"] = comps.sum(axis=1, skipna=True)
    res["held_points"] = res["points"].rolling(HOLD_DAYS, min_periods=1).max()
    res["verified_max"] = (avail * maxes).sum(axis=1)
    res["components"] = avail.sum(axis=1)
    state = np.where(res["held_points"] >= RED_POINTS, "RED",
             np.where(res["held_points"] >= AMBER_POINTS, "AMBER", "GREEN"))
    res["state"] = state
    # Too little verified data: refuse to call it calm.
    res.loc[(res["components"] < MIN_COMPONENTS) & (res["state"] == "GREEN"), "state"] = "UNVERIFIED"
    return res


def build_layer10(closes: pd.DataFrame) -> dict:
    """Layer dict for the live run, in the same shape as the other layers."""
    f = compute_features(closes)
    comps = score_components(f)
    day = comps.dropna(how="all")
    if day.empty:
        return {"score": 0, "max": LAYER_MAX, "state": "UNVERIFIED", "components": 0,
                "flags": ["Layer 10 unavailable: no usable price history"], "data": {}}
    last = comps.iloc[-1]
    last_f = f.iloc[-1]
    res = score_series(closes).iloc[-1]
    flags, unavailable = [], []
    desc = {
        "spy_drawdown": lambda: f"SPY {last_f['spy_dd']*100:.1f}% below its 52-week high",
        "spy_below_50d": lambda: f"SPY {last_f['spy_vs_sma50']*100:.1f}% vs 50-day average",
        "spy_below_200d": lambda: f"SPY {last_f['spy_vs_sma200']*100:.1f}% vs 200-day average",
        "vix_spike": lambda: f"VIX {last_f['vix']:.1f}, {last_f['vix_vs_sma20']*100:+.0f}% vs its 20-day average",
        "qqq_vs_spy": lambda: f"QQQ/SPY 20-day change {last_f['qqq_rel20']*100:+.1f}% (tech lagging)",
        "qqq_drawdown": lambda: f"QQQ {last_f['qqq_dd']*100:.1f}% below its 52-week high",
        "breadth_rsp_spy": lambda: f"RSP/SPY 20-day change {last_f['rsp_rel20']*100:+.1f}% (breadth)",
        "credit_hyg_ief": lambda: f"HYG/IEF 20-day change {last_f['credit20']*100:+.1f}% (credit)",
        "rates_10y": lambda: f"10Y yield {last_f['tnx_chg20_bp']:+.0f}bp over 20 days",
    }
    for name in COMPONENT_MAX:
        pts = last[name]
        if pd.isna(pts):
            unavailable.append(name)
        elif pts > 0:
            flags.append(f"{desc[name]()} [{int(pts)}/{COMPONENT_MAX[name]}]")
    data = {k: (None if pd.isna(v) else round(float(v), 4)) for k, v in last_f.items()}
    data["last_bar_date"] = str(closes.dropna(how="all").index[-1])[:10]
    return {"score": int(res["points"]), "held_score": int(res["held_points"]),
            "max": LAYER_MAX, "state": res["state"],
            "components": int(res["components"]), "verified_max": int(res["verified_max"]),
            "unavailable": unavailable, "flags": flags, "data": data}


def apply_trend_floor(score_data: dict, l10: dict) -> dict:
    """Raise (never lower) the headline using Layer 10. Mutates and returns score_data."""
    order = {"GREEN": 0, "AMBER": 1, "RED": 2}
    emoji = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}
    state = l10.get("state", "UNVERIFIED")
    floor = {"RED": "RED", "AMBER": "AMBER", "UNVERIFIED": "AMBER"}.get(state, "GREEN")
    score_data["trend"] = {"state": state, "score": l10.get("score", 0), "max": l10.get("max", LAYER_MAX),
                           "components": l10.get("components", 0)}
    if order[floor] > order[score_data["signal"]]:
        old = score_data["signal"]
        score_data["signal"] = floor
        score_data["emoji"] = emoji[floor]
        if state == "UNVERIFIED":
            why = (f"Layer 10 (trend) could verify only {l10.get('components', 0)}/{len(COMPONENT_MAX)} "
                   f"components, so the headline cannot be GREEN")
        else:
            why = f"Layer 10 trend/momentum is {state} ({l10['score']}/{l10['max']} points: " + "; ".join(l10["flags"][:4]) + ")"
        score_data["summary"] = f"Raised {old} -> {floor}: {why}. " + score_data["summary"]
        score_data.setdefault("data_gaps", []).append(f"headline raised by Layer 10 ({state})")
    return score_data


def format_layer10_for_email(l10: dict) -> str:
    state = l10.get("state", "UNVERIFIED")
    icon = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}.get(state, "⚪")
    lines = [f"📉 LAYER 10 — TREND & MOMENTUM (early warning): {icon} {state} "
             f"({l10.get('score', 0)}/{l10.get('max', LAYER_MAX)} points today, {l10.get('held_score', 0)} held over {HOLD_DAYS} days; "
             f"{l10.get('components', 0)}/{len(COMPONENT_MAX)} components verified)"]
    lines += [f"  • {f}" for f in l10.get("flags", [])] or ["  • No trend stress detected"]
    if l10.get("unavailable"):
        lines.append("  ⚠️ Not verified today: " + ", ".join(l10["unavailable"]))
    lines.append("  Thresholds are provisional until backtested (see backtest_trend.py).")
    return "\n".join(lines)
