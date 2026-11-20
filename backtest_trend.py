#!/usr/bin/env python3
"""
Backtest of the Layer 10 trend/momentum signal against real market history.

Run on a machine with internet access (your Mac or Railway):
    pip install yfinance pandas numpy
    python3 backtest_trend.py                 # real data, 1999 -> today
    python3 backtest_trend.py --synthetic     # plumbing check only, no network

It answers three questions with numbers, not narrative:
  1. In each >=10% S&P 500 decline, when did the signal first go AMBER/RED, and how
     much of the fall was still ahead at that point?
  2. How often does it cry wolf? (AMBER/RED spells with no further decline after)
  3. Would acting on it have helped? (exposure 100% GREEN / 50% AMBER / 0% RED vs
     buy-and-hold and vs a plain "SPY below its 200-day average" rule.)

HONEST LIMITS
  * Signals are applied to the NEXT day's return (no look-ahead).
  * No trading costs or taxes. Switching costs would reduce the results.
  * The threshold grid at the end is for checking ROBUSTNESS. Do not pick the single
    best-looking cell: that is overfitting to 25 years that will not repeat exactly.
  * Only price-based components are tested. Layers 2-9 (FRED etc.) are not.
"""
import sys
import argparse
import numpy as np
import pandas as pd
import trend_layer as t

EXPOSURE = {"GREEN": 1.0, "UNVERIFIED": 0.5, "AMBER": 0.5, "RED": 0.0}


def load_prices(synthetic=False):
    if synthetic:
        rng = np.random.default_rng(7)
        n = 6500
        idx = pd.bdate_range("2000-01-03", periods=n)
        m = rng.normal(0.0004, 0.009, n)
        for start, ln, mag in ((800, 60, -0.012), (2200, 80, -0.010), (4500, 25, -0.02), (5800, 120, -0.006)):
            m[start:start + ln] += mag          # injected declines
        vix = 16 + np.abs(pd.Series(m).rolling(10).std().fillna(0.009).values - 0.009) * 2500 + rng.normal(0, 1, n)
        def asset(beta, idio, s):
            return s * np.exp(np.cumsum(beta * m + rng.normal(0, idio, n)))
        return pd.DataFrame({"SPY": asset(1, 0.0006, 100), "QQQ": asset(1.2, 0.0015, 50),
                             "RSP": asset(0.95, 0.001, 40), "HYG": asset(0.2, 0.0008, 60),
                             "IEF": asset(0.0, 0.002, 80), "^VIX": vix,
                             "^TNX": 4 + rng.normal(0, 0.012, n).cumsum()}, index=idx)
    import yfinance as yf
    raw = yf.download(" ".join(t.TICKERS), start="1999-01-01", progress=False, auto_adjust=True)
    closes = raw["Close"].dropna(how="all")
    missing = [x for x in t.TICKERS if x not in closes.columns or closes[x].notna().sum() < 500]
    if missing:
        print(f"WARNING: little or no data for {missing}; those components are unavailable.")
    return closes


def drawdown_episodes(spy, threshold=0.10):
    """Peak-to-trough declines of at least `threshold` in SPY (closing prices)."""
    eps, peak_i, peak = [], 0, spy.iloc[0]
    in_ep, trough_i = False, 0
    for i, p in enumerate(spy.values):
        if p >= peak:
            if in_ep:
                eps.append((peak_i, trough_i)); in_ep = False
            peak, peak_i = p, i
            trough_i = i
        else:
            if not in_ep and (peak - p) / peak >= threshold:
                in_ep, trough_i = True, i
            if in_ep and p < spy.values[trough_i]:
                trough_i = i
    if in_ep:
        eps.append((peak_i, trough_i))
    return eps


def episode_report(spy, state):
    rows = []
    for pi, ti in drawdown_episodes(spy):
        peak_d, trough_d = spy.index[pi], spy.index[ti]
        window = state.iloc[pi:ti + 1]
        warn = window[window.isin(["AMBER", "RED", "UNVERIFIED"])]
        total = (spy.iloc[ti] / spy.iloc[pi] - 1) * 100
        if warn.empty:
            rows.append((peak_d.date(), trough_d.date(), total, None, None, None, None)); continue
        wd = warn.index[0]
        wp = spy.loc[wd]
        dd_at = (wp / spy.iloc[pi] - 1) * 100            # how far down already when it warned
        left = (spy.iloc[ti] / wp - 1) * 100             # decline still ahead at the warning
        rows.append((peak_d.date(), trough_d.date(), total, wd.date(), dd_at, left, (wd - peak_d).days))
    return rows


def strategy_stats(spy_ret, exposure):
    r = spy_ret * exposure.shift(1).reindex(spy_ret.index).fillna(1.0)
    eq = (1 + r.fillna(0)).cumprod()
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    mdd = (eq / eq.cummax() - 1).min()
    vol = r.std() * np.sqrt(252)
    return cagr * 100, mdd * 100, (cagr / vol if vol else np.nan), exposure.mean() * 100


def false_alarm_report(spy, state, horizon=60, further=0.05):
    """Share of AMBER/RED spells that were followed by a further >=5% drop within 60 days."""
    warn = state.isin(["AMBER", "RED"]).values
    px = spy.values
    spells, i = [], 0
    while i < len(warn):
        if warn[i]:
            j = i
            while j + 1 < len(warn) and warn[j + 1]:
                j += 1
            spells.append((i, j)); i = j + 1
        else:
            i += 1
    hit = 0
    for a, b in spells:
        fwd = px[a:min(a + horizon, len(px) - 1) + 1]
        if (fwd.min() / px[a] - 1) <= -further:
            hit += 1
    return len(spells), hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="plumbing check on fake data (no network)")
    args = ap.parse_args()

    closes = load_prices(args.synthetic)
    if args.synthetic:
        print("*** SYNTHETIC DATA - this only checks that the script runs. Numbers mean nothing. ***\n")
    spy = closes["SPY"].dropna()
    res = t.score_series(closes).reindex(spy.index)
    state = res["state"].fillna("UNVERIFIED")
    ret = spy.pct_change()

    print(f"Period: {spy.index[0].date()} -> {spy.index[-1].date()}  ({len(spy)} trading days)")
    print("Days in state: " + ", ".join(f"{k} {100*(state==k).mean():.0f}%" for k in ("GREEN", "AMBER", "RED", "UNVERIFIED")))

    print("\n1) S&P 500 declines of >=10%: when did the signal first warn?")
    print(f"{'peak':<11}{'trough':<11}{'fall':>7}  {'first warning':<14}{'already down':>13}{'still ahead':>12}{'days after peak':>16}")
    eps = episode_report(spy, state)
    missed = 0
    for pk, tr, tot, wd, dd, left, days in eps:
        if wd is None:
            missed += 1
            print(f"{str(pk):<11}{str(tr):<11}{tot:>6.1f}%  {'NO WARNING':<14}")
        else:
            print(f"{str(pk):<11}{str(tr):<11}{tot:>6.1f}%  {str(wd):<14}{dd:>12.1f}%{left:>11.1f}%{days:>16}")
    print(f"Episodes: {len(eps)} | no warning before trough: {missed}")
    caught = [e for e in eps if e[3] is not None]
    if caught:
        print(f"Median decline already suffered at first warning: {np.median([e[4] for e in caught]):.1f}% | "
              f"median decline still ahead: {np.median([e[5] for e in caught]):.1f}%")

    n, hit = false_alarm_report(spy, state)
    print(f"\n2) False alarms: {n} AMBER/RED spells; {hit} ({100*hit/max(n,1):.0f}%) were followed by a further >=5% drop within 60 days.")

    print("\n3) Acting on it (next-day execution, no costs):")
    sma200_exp = (spy > spy.rolling(200).mean()).astype(float)
    vix_exp = (~(closes["^VIX"].reindex(spy.index) > 20)).astype(float) if "^VIX" in closes else sma200_exp
    rows = {
        "Buy & hold SPY": pd.Series(1.0, index=spy.index),
        "Trend layer (G100/A50/R0)": state.map(EXPOSURE).astype(float),
        "SPY > 200d else cash": sma200_exp,
        "VIX<=20 else cash": vix_exp,
    }
    print(f"{'rule':<28}{'CAGR%':>8}{'maxDD%':>9}{'CAGR/vol':>10}{'avg exp%':>10}")
    for name, e in rows.items():
        c, d, sh, ex = strategy_stats(ret, e)
        print(f"{name:<28}{c:>8.1f}{d:>9.1f}{sh:>10.2f}{ex:>10.0f}")

    print("\n4) Robustness grid (trend layer; do NOT just pick the best cell):")
    print(f"{'hold':>5}{'amber':>7}{'red':>5}{'CAGR%':>8}{'maxDD%':>9}{'avg exp%':>10}")
    base = (t.HOLD_DAYS, t.AMBER_POINTS, t.RED_POINTS)
    try:
        for hold in (0, 5, 10):
            for amber, red in ((2, 5), (3, 6), (4, 7)):
                t.HOLD_DAYS, t.AMBER_POINTS, t.RED_POINTS = hold if hold else 1, amber, red
                st = t.score_series(closes).reindex(spy.index)["state"].fillna("UNVERIFIED")
                c, d, _, ex = strategy_stats(ret, st.map(EXPOSURE).astype(float))
                print(f"{hold:>5}{amber:>7}{red:>5}{c:>8.1f}{d:>9.1f}{ex:>10.0f}")
    finally:
        t.HOLD_DAYS, t.AMBER_POINTS, t.RED_POINTS = base
    print("\nRead this honestly: if the trend layer does not clearly beat 'SPY > 200d', or the grid is "
          "unstable, the extra components are not earning their place.")


if __name__ == "__main__":
    main()
