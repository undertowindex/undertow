from __future__ import annotations

from dataclasses import dataclass

from glint.data_provider import CompanyFundamentals

# Quality gates — a stock must clear all of these to count as a
# "quality survivor," regardless of how cheap it looks. These feed
# the long list Ark Protocol will draw on, so junky names can't sneak
# through just because their multiples are low.
MAX_DEBT_TO_EQUITY = 2.0

# Value thresholds — score how much of a bargain a quality survivor
# is. Phase 1 compares against fixed ceilings/floors rather than true
# sector/historical averages, since Alpha Vantage's free tier doesn't
# provide peer or historical-price data. Revisit once Glint moves to
# a paid provider.
MAX_PE = 25
MAX_PB = 4
MIN_FCF_YIELD = 0.04


@dataclass
class ScreenResult:
    ticker: str
    passed_quality: bool
    quality_reasons: list[str]
    value_score: int
    value_reasons: list[str]

    @property
    def is_candidate(self) -> bool:
        return self.passed_quality and self.value_score > 0


def _check_quality(f: CompanyFundamentals) -> tuple[bool, list[str]]:
    reasons = []
    passed = True

    if f.free_cash_flow is None or f.free_cash_flow <= 0:
        passed = False
        reasons.append("Free cash flow is not positive")
    else:
        reasons.append("Free cash flow is positive")

    if not f.net_income_history:
        passed = False
        reasons.append("No net income history available")
    elif any(v <= 0 for v in f.net_income_history):
        passed = False
        loss_years = sum(1 for v in f.net_income_history if v <= 0)
        reasons.append(f"Unprofitable in {loss_years} of {len(f.net_income_history)} reported years")
    else:
        reasons.append(f"Profitable in all {len(f.net_income_history)} reported years")

    if f.debt_to_equity is None:
        passed = False
        reasons.append("Debt/equity unavailable")
    elif f.debt_to_equity > MAX_DEBT_TO_EQUITY:
        passed = False
        reasons.append(f"Debt/equity {f.debt_to_equity:.2f} exceeds {MAX_DEBT_TO_EQUITY}")
    else:
        reasons.append(f"Debt/equity {f.debt_to_equity:.2f} is manageable")

    return passed, reasons


def _score_value(f: CompanyFundamentals) -> tuple[int, list[str]]:
    score = 0
    reasons = []

    if f.pe_ratio is not None and 0 < f.pe_ratio <= MAX_PE:
        score += 1
        reasons.append(f"P/E {f.pe_ratio:.1f} is at or below {MAX_PE}")

    if f.pb_ratio is not None and 0 < f.pb_ratio <= MAX_PB:
        score += 1
        reasons.append(f"P/B {f.pb_ratio:.1f} is at or below {MAX_PB}")

    if f.fcf_yield is not None and f.fcf_yield >= MIN_FCF_YIELD:
        score += 1
        reasons.append(f"FCF yield {f.fcf_yield:.1%} is at or above {MIN_FCF_YIELD:.0%}")

    return score, reasons


def screen(f: CompanyFundamentals) -> ScreenResult:
    passed_quality, quality_reasons = _check_quality(f)
    value_score, value_reasons = _score_value(f)
    return ScreenResult(
        ticker=f.ticker,
        passed_quality=passed_quality,
        quality_reasons=quality_reasons,
        value_score=value_score,
        value_reasons=value_reasons,
    )
