"""Pulls fundamentals for a list of tickers via yfinance (free, unofficial Yahoo Finance API)."""

import time
import logging

import yfinance as yf

logger = logging.getLogger("glint.fetch")

# Fields pulled from yfinance's Ticker.info dict, used for value scoring.
FIELDS = [
    "longName",
    "sector",
    "currency",
    "marketCap",
    "trailingPE",
    "priceToBook",
    "dividendYield",
    "returnOnEquity",
    "debtToEquity",
    "currentPrice",
]


def fetch_fundamentals(tickers, pause_seconds=0.5):
    """Fetch fundamentals for each ticker one at a time.

    Returns a dict: {ticker: {field: value}}. Tickers that fail to fetch are
    logged and skipped rather than aborting the whole run.
    """
    results = {}
    for i, ticker in enumerate(tickers):
        try:
            info = yf.Ticker(ticker).info
            if not info or info.get("trailingPE") is None and info.get("priceToBook") is None:
                logger.warning("No usable fundamentals for %s, skipping", ticker)
                continue
            results[ticker] = {field: info.get(field) for field in FIELDS}
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", ticker, exc)
        if pause_seconds and i < len(tickers) - 1:
            time.sleep(pause_seconds)
    return results
