"""Sends the daily Glint report via Resend (same provider/pattern as Undertow/Ark)."""

import os
import datetime
import requests

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "micahbrown4@me.com")
ALERT_EMAILS = [e.strip() for e in ALERT_EMAIL.split(",") if e.strip()]


def send_report_email(html_body, buy_count):
    """Returns True only on a confirmed 200 from Resend - callers must check
    this and fail loudly rather than silently completing (see Undertow's
    same fix from 2026-08-24: a report that silently failed to send is
    worse than no report)."""
    if not RESEND_API_KEY:
        print("No RESEND_API_KEY set - skipping email.")
        return False

    subject = f"Glint Daily UK + US Value Screener — {buy_count} BUY calls — {datetime.date.today().strftime('%d %b %Y')}"

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Glint <onboarding@resend.dev>",
                "to": ALERT_EMAILS,
                "subject": subject,
                "html": html_body,
            },
            timeout=15,
        )
        if response.status_code == 200:
            print(f"Email sent to {', '.join(ALERT_EMAILS)}")
            return True
        print(f"Email failed: {response.status_code} - {response.text}")
        return False
    except Exception as e:
        print(f"Email error: {e}")
        return False
