import time

import requests

from glint.data_provider import DataProvider, CompanyFundamentals

BASE_URL = "https://www.alphavantage.co/query"

# Alpha Vantage's free tier is capped at 25 requests/day and 5/minute.
# Each ticker costs 5 calls here, so this delay keeps a multi-ticker
# run under the per-minute limit; the daily cap still limits how many
# tickers can be checked per day.
SECONDS_BETWEEN_CALLS = 13


def _to_float(value):
    if value in (None, "None", "-", ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AlphaVantageProvider(DataProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(self, function: str, symbol: str, retries: int = 3, backoff_seconds: int = 5):
        params = {"function": function, "symbol": symbol, "apikey": self.api_key}
        for attempt in range(1, retries + 1):
            try:
                r = requests.get(BASE_URL, params=params, timeout=15)
                r.raise_for_status()
                data = r.json()
                if "Note" in data or "Information" in data:
                    # Rate limit or key-quota message instead of data.
                    raise RuntimeError(data.get("Note") or data.get("Information"))
                return data
            except Exception as e:
                if attempt < retries:
                    time.sleep(backoff_seconds * attempt)
                else:
                    print(f"  ⚠️  Alpha Vantage {function} fetch failed for {symbol} after {retries} attempts: {e}", flush=True)
                    return {}

    def get_fundamentals(self, ticker: str) -> CompanyFundamentals:
        overview = self._get("OVERVIEW", ticker)
        time.sleep(SECONDS_BETWEEN_CALLS)
        quote = self._get("GLOBAL_QUOTE", ticker)
        time.sleep(SECONDS_BETWEEN_CALLS)
        cash_flow = self._get("CASH_FLOW", ticker)
        time.sleep(SECONDS_BETWEEN_CALLS)
        balance_sheet = self._get("BALANCE_SHEET", ticker)
        time.sleep(SECONDS_BETWEEN_CALLS)
        income_statement = self._get("INCOME_STATEMENT", ticker)

        price = _to_float(quote.get("Global Quote", {}).get("05. price"))

        cash_flow_reports = cash_flow.get("annualReports", [])
        operating_cash_flow = _to_float(cash_flow_reports[0]["operatingCashflow"]) if cash_flow_reports else None
        capital_expenditures = _to_float(cash_flow_reports[0]["capitalExpenditures"]) if cash_flow_reports else None

        free_cash_flow = None
        if operating_cash_flow is not None and capital_expenditures is not None:
            free_cash_flow = operating_cash_flow - abs(capital_expenditures)

        market_cap = _to_float(overview.get("MarketCapitalization"))
        fcf_yield = None
        if free_cash_flow is not None and market_cap:
            fcf_yield = free_cash_flow / market_cap

        balance_reports = balance_sheet.get("annualReports", [])
        total_debt = _to_float(balance_reports[0]["shortLongTermDebtTotal"]) if balance_reports else None
        total_equity = _to_float(balance_reports[0]["totalShareholderEquity"]) if balance_reports else None
        debt_to_equity = None
        if total_debt is not None and total_equity:
            debt_to_equity = total_debt / total_equity

        income_reports = income_statement.get("annualReports", [])
        net_income_history = [
            v for v in (_to_float(r.get("netIncome")) for r in income_reports) if v is not None
        ]

        return CompanyFundamentals(
            ticker=ticker,
            sector=overview.get("Sector") or None,
            price=price,
            pe_ratio=_to_float(overview.get("PERatio")),
            pb_ratio=_to_float(overview.get("PriceToBookRatio")),
            market_cap=market_cap,
            operating_cash_flow=operating_cash_flow,
            capital_expenditures=capital_expenditures,
            free_cash_flow=free_cash_flow,
            fcf_yield=fcf_yield,
            total_debt=total_debt,
            total_equity=total_equity,
            debt_to_equity=debt_to_equity,
            net_income_history=net_income_history,
        )
