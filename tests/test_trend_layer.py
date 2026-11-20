"""Plumbing tests for trend_layer. Run: python3 -I -m unittest tests.test_trend_layer (from repo root)
NOTE: these prove the code behaves as designed. They do NOT validate the thresholds -
that is what backtest_trend.py is for."""
import os, sys, unittest
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trend_layer as t

def synthetic(n=420, crash_at=None, seed=1):
    """Correlated assets: one market factor plus small idiosyncratic noise (like real ETFs)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    m = rng.normal(0.0005, 0.004, n)
    def asset(beta, idio, start):
        return start * np.exp(np.cumsum(beta * m + rng.normal(0, idio, n)))
    d = pd.DataFrame({"SPY": asset(1.0, 0.0005, 400), "QQQ": asset(1.2, 0.0012, 350),
                      "RSP": asset(0.95, 0.0008, 150), "HYG": asset(0.15, 0.0006, 75),
                      "IEF": asset(0.0, 0.0015, 95),
                      "^VIX": 15 + rng.normal(0, 0.7, n),
                      "^TNX": 4.2 + rng.normal(0, 0.01, n).cumsum()}, index=idx)
    if crash_at is not None:
        k = crash_at
        for col, depth in (("SPY", 0.85), ("RSP", 0.85), ("QQQ", 0.78)):
            j = d.columns.get_loc(col)
            d.iloc[k:k + 12, j] *= np.linspace(1, depth, 12)
            d.iloc[k + 12:, j] *= depth
        d.iloc[k:k + 12, d.columns.get_loc("^VIX")] = np.linspace(16, 32, 12)
        d.iloc[k + 12:, d.columns.get_loc("^VIX")] = 30
        d.iloc[k:, d.columns.get_loc("HYG")] *= 0.97
    return d

class Tests(unittest.TestCase):
    def test_all_components_available_with_full_data(self):
        r = t.score_series(synthetic())
        self.assertEqual(r["components"].iloc[-1], len(t.COMPONENT_MAX))

    def test_calm_is_green_and_crash_is_red(self):
        d = synthetic(crash_at=380)
        r = t.score_series(d)
        self.assertEqual(r["state"].iloc[370], "GREEN")
        self.assertEqual(r["state"].iloc[-1], "RED")

    def test_missing_data_never_reads_green(self):
        r = t.score_series(synthetic()[["SPY", "^VIX"]])
        self.assertNotIn("GREEN", set(r["state"].iloc[-50:]))
        self.assertTrue((r["components"].iloc[-1] < t.MIN_COMPONENTS))

    def test_floor_only_raises(self):
        l10 = t.build_layer10(synthetic(crash_at=380))
        sd = {"signal": "GREEN", "emoji": "🟢", "summary": "calm", "data_gaps": []}
        out = t.apply_trend_floor(sd, l10)
        self.assertEqual(out["signal"], "RED")
        sd2 = {"signal": "RED", "emoji": "🔴", "summary": "x", "data_gaps": []}
        l10_calm = t.build_layer10(synthetic())
        self.assertEqual(t.apply_trend_floor(sd2, l10_calm)["signal"], "RED")

    def test_tnx_scaled_feed_handled(self):
        d = synthetic(); d["^TNX"] = d["^TNX"] * 10
        a = t.compute_features(d)["tnx_chg20_bp"].iloc[-1]
        b = t.compute_features(synthetic())["tnx_chg20_bp"].iloc[-1]
        self.assertAlmostEqual(a, b, places=6)

    def test_real_episode_warns_before_nov5_and_holds(self):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "spy_vix_2026.csv")
        r = t.score_series(pd.read_csv(p, index_col=0, parse_dates=True))
        self.assertGreaterEqual(r.loc["2026-11-02", "held_points"], t.AMBER_POINTS)   # before the 5 Nov drop
        for d in ("2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"):             # stays up through the bounce
            self.assertGreaterEqual(r.loc[d, "held_points"], t.AMBER_POINTS, d)
        self.assertLess(r.loc["2026-08-05":"2026-10-15", "held_points"].max(), t.AMBER_POINTS)  # no false AMBER Aug-mid Oct

if __name__ == "__main__":
    unittest.main()
