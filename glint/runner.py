import os

from glint.data_provider import get_provider
from glint.screener import screen

DEFAULT_WATCHLIST = ["ADBE", "AAPL", "KO"]


def _get_watchlist():
    raw = os.environ.get("GLINT_WATCHLIST", "")
    if raw.strip():
        return [t.strip().upper() for t in raw.split(",") if t.strip()]
    return DEFAULT_WATCHLIST


def run_glint(watchlist=None):
    watchlist = watchlist or _get_watchlist()
    provider = get_provider()
    results = []
    for ticker in watchlist:
        try:
            fundamentals = provider.get_fundamentals(ticker)
            results.append((screen(fundamentals), fundamentals))
        except Exception as e:
            print(f"  ⚠️  Glint: failed to screen {ticker}: {e}", flush=True)
    return results
