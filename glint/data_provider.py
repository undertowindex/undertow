from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class CompanyFundamentals:
    """Standardized fundamentals shape. Every provider must fill this in
    the same way, so the screening logic never needs to know which
    provider produced the data."""

    ticker: str
    sector: str | None
    price: float | None

    pe_ratio: float | None
    pb_ratio: float | None
    market_cap: float | None

    operating_cash_flow: float | None
    capital_expenditures: float | None
    free_cash_flow: float | None
    fcf_yield: float | None

    total_debt: float | None
    total_equity: float | None
    debt_to_equity: float | None

    net_income_history: list[float] = field(default_factory=list)

    fifty_two_week_low: float | None = None
    fifty_two_week_high: float | None = None

    @property
    def price_position_pct(self) -> float | None:
        """Where the current price sits in its 52-week range: 0 = at the
        low, 100 = at the high. Just context on how far off recent lows a
        candidate is — not an entry signal or a recommendation."""
        if self.price is None or self.fifty_two_week_low is None or self.fifty_two_week_high is None:
            return None
        spread = self.fifty_two_week_high - self.fifty_two_week_low
        if spread <= 0:
            return None
        return (self.price - self.fifty_two_week_low) / spread * 100


class DataProvider(ABC):
    @abstractmethod
    def get_fundamentals(self, ticker: str) -> CompanyFundamentals:
        ...


def get_provider() -> DataProvider:
    provider_name = os.environ.get("GLINT_DATA_PROVIDER", "yahoo_finance")

    if provider_name == "yahoo_finance":
        from glint.yahoo_finance_provider import YahooFinanceProvider
        return YahooFinanceProvider()

    if provider_name == "alpha_vantage":
        from glint.alpha_vantage_provider import AlphaVantageProvider
        return AlphaVantageProvider(os.environ.get("ALPHA_VANTAGE_API_KEY", ""))

    raise ValueError(f"Unknown GLINT_DATA_PROVIDER: {provider_name!r}")
