"""Composite value score and BUY/HOLD/SELL classification.

Approach: for each metric, rank every stock in the universe by percentile
(0 = least attractive, 1 = most attractive on that metric), then average the
available percentiles per stock into one composite score. Missing metrics
are simply left out of that stock's average rather than penalized.
"""

# metric -> True if higher raw value is more attractive (e.g. dividend yield),
# False if lower raw value is more attractive (e.g. P/E ratio).
METRICS_HIGHER_IS_BETTER = {
    "trailingPE": False,
    "priceToBook": False,
    "dividendYield": True,
    "returnOnEquity": True,
    "debtToEquity": False,
}

BUY_THRESHOLD = 0.65
SELL_THRESHOLD = 0.35

# Require at least this many of the 5 metrics to be present. Investment
# trusts / closed-end funds typically report only P/E and P/B (no dividend,
# ROE, or D/E from yfinance), which makes those two ratios alone a
# misleading signal — they're not comparable operating businesses, so they
# should not compete in the value ranking at all.
MIN_METRICS_REQUIRED = 3

# P/E and P/B go negative when earnings or book equity are negative (heavy
# losses, or buybacks that have eroded equity below zero) - financial
# distress, not cheapness. Under "lower is better" ranking a negative value
# sorts as the most attractive in the whole universe, which is backwards.
# Treated as not-applicable for that metric rather than ranked.
METRICS_EXCLUDE_NEGATIVE = {"trailingPE", "priceToBook"}


def _percentile_ranks(values_by_ticker, higher_is_better):
    """Given {ticker: value_or_None}, return {ticker: percentile in [0,1]}."""
    valid = [(t, v) for t, v in values_by_ticker.items() if v is not None]
    if not valid:
        return {}
    valid.sort(key=lambda tv: tv[1], reverse=higher_is_better)
    n = len(valid)
    ranks = {}
    for idx, (ticker, _) in enumerate(valid):
        # idx 0 = most attractive -> percentile close to 1
        ranks[ticker] = 1.0 - (idx / max(n - 1, 1))
    return ranks


def score_stocks(fundamentals):
    """fundamentals: {ticker: {field: value}} from fetch.fetch_fundamentals.

    Returns (rows, excluded_tickers). rows is sorted by composite score
    descending, each with the original fields plus 'score' and 'call'
    (BUY/HOLD/SELL). excluded_tickers lists tickers dropped for having too
    few of the 5 metrics to rank fairly (e.g. investment trusts).
    """
    eligible = {
        t: data
        for t, data in fundamentals.items()
        if sum(1 for m in METRICS_HIGHER_IS_BETTER if data.get(m) is not None) >= MIN_METRICS_REQUIRED
    }
    excluded = fundamentals.keys() - eligible.keys()

    metric_ranks = {}
    for metric, higher_is_better in METRICS_HIGHER_IS_BETTER.items():
        values = {}
        for t, data in eligible.items():
            v = data.get(metric)
            if metric in METRICS_EXCLUDE_NEGATIVE and v is not None and v <= 0:
                v = None
            values[t] = v
        metric_ranks[metric] = _percentile_ranks(values, higher_is_better)

    rows = []
    for ticker, data in eligible.items():
        applicable = [
            metric_ranks[metric][ticker]
            for metric in METRICS_HIGHER_IS_BETTER
            if ticker in metric_ranks[metric]
        ]
        if not applicable:
            continue
        score = sum(applicable) / len(applicable)
        if score >= BUY_THRESHOLD:
            call = "BUY"
        elif score <= SELL_THRESHOLD:
            call = "SELL"
        else:
            call = "HOLD"
        rows.append({**data, "ticker": ticker, "score": score, "call": call, "metrics_used": len(applicable)})

    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows, sorted(excluded)
