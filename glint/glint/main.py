"""Glint: UK + US value screener. Fetches fundamentals, scores, prints/saves a report.

Usage:
    python3 -m glint.main [--limit N] [--out FILE] [--email]

--email actually sends the report (via Resend, needs RESEND_API_KEY) - used
by the Railway daily cron. The desktop icon (GLINT.command) does NOT pass
this flag, so manual runs just open the local HTML report, no email sent.
"""

import argparse
import logging
import sys

from .tickers import FTSE_100, US_LARGE_CAP
from .fetch import fetch_fundamentals
from .score import score_stocks
from .report import format_report, format_html_report
from .email_sender import send_report_email

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

FULL_UNIVERSE = FTSE_100 + US_LARGE_CAP


def run(limit=None, out=None, out_html=None, top_n=15, email=False):
    universe = FULL_UNIVERSE[:limit] if limit else FULL_UNIVERSE
    fundamentals = fetch_fundamentals(universe)
    rows, excluded = score_stocks(fundamentals)
    report = format_report(rows, universe_size=len(universe), fetched_count=len(fundamentals), excluded=excluded, top_n=top_n)
    print(report)
    if out:
        with open(out, "w") as f:
            f.write(report)

    html = None
    if out_html or email:
        html = format_html_report(rows, universe_size=len(universe), fetched_count=len(fundamentals), excluded=excluded, top_n=top_n)
    if out_html:
        with open(out_html, "w") as f:
            f.write(html)

    if email:
        buy_count = sum(1 for r in rows if r["call"] == "BUY")
        sent = send_report_email(html, buy_count)
        if not sent:
            print("EMAIL DID NOT SEND - the analysis above is real but was not delivered.")
            sys.exit(1)

    return report


def main():
    parser = argparse.ArgumentParser(description="Glint UK + US value screener")
    parser.add_argument("--limit", type=int, default=None, help="Only screen the first N tickers (for quick testing)")
    parser.add_argument("--out", type=str, default=None, help="Also write the plain-text report to this file")
    parser.add_argument("--out-html", type=str, default=None, help="Also write the HTML report to this file")
    parser.add_argument("--top", type=int, default=15, help="Number of top-ranked stocks to show (default 15)")
    parser.add_argument("--email", action="store_true", help="Send the report by email (Railway cron only)")
    args = parser.parse_args()
    run(limit=args.limit, out=args.out, out_html=args.out_html, top_n=args.top, email=args.email)


if __name__ == "__main__":
    sys.exit(main())
