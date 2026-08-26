"""Formats scored stock rows into a daily report (plain text and color HTML)."""

from datetime import date

METHODOLOGY = (
    "How this works: every stock is compared against the rest of today's "
    "universe on 5 value metrics, each converted to a percentile (0-100%, "
    "higher = more attractive on that metric). The percentiles are averaged "
    "into one composite score (0-1). Score >= 0.65 -> BUY, <= 0.35 -> SELL, "
    "else HOLD. A call reflects relative cheapness/quality vs today's peer "
    "group, not a prediction of price movement, and is not financial advice."
)

METRIC_GLOSSARY = [
    ("P/E", "Price / Earnings — price per share divided by profit per share. Lower usually means cheaper relative to profit."),
    ("P/B", "Price / Book — price per share divided by net asset value per share. Lower means trading closer to (or below) accounting book value."),
    ("DivYld", "Dividend Yield — annual dividend as a % of share price. Higher means more income per pound invested."),
    ("ROE", "Return on Equity — profit as a % of shareholder equity. Higher means the company generates more profit per pound of capital."),
    ("D/E", "Debt / Equity — total debt as a % of shareholder equity. Lower means less reliant on borrowing, generally lower financial risk."),
]


def _metric_strs(r):
    pe = r.get("trailingPE")
    pb = r.get("priceToBook")
    div = r.get("dividendYield")
    roe = r.get("returnOnEquity")
    de = r.get("debtToEquity")
    return {
        "pe": f"{pe:.1f}" if pe is not None else "-",
        "pb": f"{pb:.1f}" if pb is not None else "-",
        "div": f"{div:.1f}%" if div is not None else "-",
        "roe": f"{roe*100:.1f}%" if roe is not None else "-",
        "de": f"{de:.0f}" if de is not None else "-",
    }


def format_report(rows, universe_size, fetched_count, excluded=None, top_n=15):
    lines = []
    lines.append(f"Glint Daily UK + US Value Screener — {date.today().isoformat()}")
    lines.append(f"Universe: {universe_size} tickers (FTSE 100 + US large-cap), fundamentals fetched for {fetched_count}, ranked {len(rows)}")
    if excluded:
        lines.append(f"Excluded (insufficient fundamental data, e.g. investment trusts): {', '.join(excluded)}")
    lines.append("")
    lines.append(METHODOLOGY)
    lines.append("")
    for abbr, desc in METRIC_GLOSSARY:
        lines.append(f"  {abbr}: {desc}")
    lines.append("")

    shown = rows[:top_n]
    lines.append(f"Top {len(shown)} of {len(rows)} ranked stocks:")
    header = f"{'Ticker':<10}{'Name':<28}{'Call':<6}{'Score':<7}{'P/E':<8}{'P/B':<7}{'DivYld':<8}{'ROE':<8}{'D/E':<8}"
    lines.append(header)
    lines.append("-" * len(header))

    for r in shown:
        name = (r.get("longName") or "")[:26]
        m = _metric_strs(r)
        lines.append(
            f"{r['ticker']:<10}{name:<28}{r['call']:<6}{r['score']:<7.2f}"
            f"{m['pe']:<8}{m['pb']:<7}{m['div']:<8}{m['roe']:<8}{m['de']:<8}"
        )

    buys = [r for r in rows if r["call"] == "BUY"]
    lines.append("")
    lines.append(f"BUY calls today ({len(buys)} total): {', '.join(r['ticker'] for r in buys) if buys else 'none'}")

    return "\n".join(lines)


_CALL_COLORS = {"BUY": "#1a7f37", "HOLD": "#9a6700", "SELL": "#cf222e"}
_CALL_BG = {"BUY": "#dafbe1", "HOLD": "#fff8c5", "SELL": "#ffebe9"}


def format_html_report(rows, universe_size, fetched_count, excluded=None, top_n=15):
    shown = rows[:top_n]
    buys = [r for r in rows if r["call"] == "BUY"]

    glossary_rows = "".join(
        f"<tr><td style='padding:2px 10px 2px 0;font-weight:600;white-space:nowrap;'>{abbr}</td>"
        f"<td style='padding:2px 0;color:#444;'>{desc}</td></tr>"
        for abbr, desc in METRIC_GLOSSARY
    )

    table_rows = ""
    for r in shown:
        name = (r.get("longName") or "")[:32]
        m = _metric_strs(r)
        color = _CALL_COLORS.get(r["call"], "#333")
        bg = _CALL_BG.get(r["call"], "#f0f0f0")
        table_rows += (
            "<tr>"
            f"<td style='padding:6px 8px;font-weight:600;'>{r['ticker']}</td>"
            f"<td style='padding:6px 8px;color:#333;'>{name}</td>"
            f"<td style='padding:6px 8px;text-align:center;'>"
            f"<span style='background:{bg};color:{color};font-weight:700;padding:2px 10px;border-radius:12px;font-size:12px;'>{r['call']}</span></td>"
            f"<td style='padding:6px 8px;text-align:right;'>{r['score']:.2f}</td>"
            f"<td style='padding:6px 8px;text-align:right;'>{m['pe']}</td>"
            f"<td style='padding:6px 8px;text-align:right;'>{m['pb']}</td>"
            f"<td style='padding:6px 8px;text-align:right;'>{m['div']}</td>"
            f"<td style='padding:6px 8px;text-align:right;'>{m['roe']}</td>"
            f"<td style='padding:6px 8px;text-align:right;'>{m['de']}</td>"
            "</tr>"
        )

    excluded_html = (
        f"<p style='color:#666;font-size:12px;'>Excluded (insufficient fundamental data, e.g. investment trusts): {', '.join(excluded)}</p>"
        if excluded else ""
    )

    html = f"""
<div style="font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 720px;">
  <h2 style="margin-bottom:4px;">Glint Daily UK + US Value Screener — {date.today().isoformat()}</h2>
  <p style="color:#666;font-size:13px;margin-top:0;">
    Universe: {universe_size} tickers (FTSE 100 + US large-cap), fundamentals fetched for {fetched_count}, ranked {len(rows)}.
    Showing top {len(shown)}.
  </p>
  {excluded_html}
  <p style="font-size:13px;color:#333;background:#f6f8fa;padding:10px 12px;border-radius:6px;">{METHODOLOGY}</p>
  <table style="border-collapse:collapse;font-size:13px;margin-bottom:14px;">{glossary_rows}</table>

  <table style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead>
      <tr style="border-bottom:2px solid #333;text-align:left;">
        <th style="padding:6px 8px;">Ticker</th>
        <th style="padding:6px 8px;">Name</th>
        <th style="padding:6px 8px;text-align:center;">Call</th>
        <th style="padding:6px 8px;text-align:right;">Score</th>
        <th style="padding:6px 8px;text-align:right;">P/E</th>
        <th style="padding:6px 8px;text-align:right;">P/B</th>
        <th style="padding:6px 8px;text-align:right;">DivYld</th>
        <th style="padding:6px 8px;text-align:right;">ROE</th>
        <th style="padding:6px 8px;text-align:right;">D/E</th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>

  <p style="font-size:13px;margin-top:14px;"><strong>BUY calls today ({len(buys)} total):</strong> {', '.join(r['ticker'] for r in buys) if buys else 'none'}</p>
  <p style="font-size:11px;color:#999;">Sent by Glint. Running on free Yahoo Finance data (yfinance) for validation — not yet on a paid feed or a schedule.</p>
</div>
"""
    return html
