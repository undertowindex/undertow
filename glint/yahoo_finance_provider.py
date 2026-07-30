from __future__ import annotations

import yfinance as yf

from glint.data_provider import DataProvider, CompanyFundamentals


def _first(series):
    if series is None or len(series) == 0:
        return None
    value = series.iloc[0]
    return None if value != value else float(value)  # NaN check


def _history(series, limit: int = 4) -> list[float]:
    if series is None:
        return []
    return [float(v) for v in series.iloc[:limit] if v == v]  # drops NaN


class YahooFinanceProvider(DataProvider):
    def get_fundamentals(self, ticker: str) -> CompanyFundamentals:
        t = yf.Ticker(ticker)
        info = t.info

        price = info.get("currentPrice") or info.get("regularMarketPrice")

        cash_flow = t.cashflow
        operating_cash_flow = _first(cash_flow.loc["Operating Cash Flow"]) if "Operating Cash Flow" in cash_flow.index else None
        capital_expenditures = _first(cash_flow.loc["Capital Expenditure"]) if "Capital Expenditure" in cash_flow.index else None
        free_cash_flow = info.get("freeCashflow")

        market_cap = info.get("marketCap")
        fcf_yield = None
        if free_cash_flow is not None and market_cap:
            fcf_yield = free_cash_flow / market_cap

        balance_sheet = t.balance_sheet
        total_debt = _first(balance_sheet.loc["Total Debt"]) if "Total Debt" in balance_sheet.index else None
        total_equity = _first(balance_sheet.loc["Stockholders Equity"]) if "Stockholders Equity" in balance_sheet.index else None
        debt_to_equity = None
        if total_debt is not None and total_equity:
            debt_to_equity = total_debt / total_equity

        income_stmt = t.income_stmt
        net_income_history = _history(income_stmt.loc["Net Income"]) if "Net Income" in income_stmt.index else []

        return CompanyFundamentals(
            ticker=ticker,
            sector=info.get("sector"),
            price=price,
            pe_ratio=info.get("trailingPE"),
            pb_ratio=info.get("priceToBook"),
            market_cap=market_cap,
            operating_cash_flow=operating_cash_flow,
            capital_expenditures=capital_expenditures,
            free_cash_flow=free_cash_flow,
            fcf_yield=fcf_yield,
            total_debt=total_debt,
            total_equity=total_equity,
            debt_to_equity=debt_to_equity,
            net_income_history=net_income_history,
            fifty_two_week_low=info.get("fiftyTwoWeekLow"),
            fifty_two_week_high=info.get("fiftyTwoWeekHigh"),
        )
