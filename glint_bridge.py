"""Minimal bridge between new glint API and undertow's old expectations."""

from glint.glint.main import run as glint_run
from glint.glint.report import format_html_report
from glint.glint.tickers import FTSE_100, US_LARGE_CAP
from glint.glint.fetch import fetch_fundamentals
from glint.glint.score import score_stocks


class GlintCandidate:
    """Wraps a glint row to match old API expectations."""
    def __init__(self, row):
        self.row = row
        self.is_candidate = row.get("call") == "BUY"
        self.ticker = row.get("ticker")

    def __getitem__(self, key):
        return self.row[key]


def run_glint():
    """Run glint and return (candidate, flags) tuples matching old API."""
    universe = FTSE_100 + US_LARGE_CAP
    fundamentals = fetch_fundamentals(universe)
    rows, excluded = score_stocks(fundamentals)
    return [(GlintCandidate(row), {}) for row in rows]


def build_glint_section(glint_results):
    """Generate HTML from glint results."""
    if not glint_results:
        return "<p>No glint results.</p>"

    rows = [r for r, _ in glint_results]
    buy_calls = [r for r in rows if r.is_candidate]

    html_rows = []
    for r in rows[:15]:
        ticker = r["ticker"]
        name = (r.get("longName") or "")[:26]
        score = r.get("score", 0)
        call = r.get("call", "-")

        color = {"BUY": "#1a7f37", "HOLD": "#9a6700", "SELL": "#cf222e"}.get(call, "#333")
        bg = {"BUY": "#dafbe1", "HOLD": "#fff8c5", "SELL": "#ffebe9"}.get(call, "#f0f0f0")

        html_rows.append(f"""<tr>
<td style="padding:6px 8px;font-weight:600;">{ticker}</td>
<td style="padding:6px 8px;">{name}</td>
<td style="padding:6px 8px;text-align:center;"><span style="background:{bg};color:{color};font-weight:700;padding:2px 10px;border-radius:12px;font-size:12px;">{call}</span></td>
<td style="padding:6px 8px;text-align:right;font-weight:600;">{score:.2f}</td>
</tr>""")

    buy_list = ", ".join(r["ticker"] for r in buy_calls) if buy_calls else "none"

    return f"""<div style="font-family: -apple-system, Helvetica, Arial, sans-serif;">
<h3>💎 Glint Daily Value Screen</h3>
<p>Ranked {len(rows)} stocks. BUY calls ({len(buy_calls)} total): {buy_list}</p>
<table style="border-collapse:collapse;font-size:13px;">
<thead><tr style="border-bottom:2px solid #333;">
<th style="padding:6px 8px;text-align:left;">Ticker</th>
<th style="padding:6px 8px;text-align:left;">Name</th>
<th style="padding:6px 8px;text-align:center;">Call</th>
<th style="padding:6px 8px;text-align:right;">Score</th>
</tr></thead>
<tbody>{''.join(html_rows)}</tbody>
</table>
</div>"""
