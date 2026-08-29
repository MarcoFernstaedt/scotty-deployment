from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

PROFESSIONAL_DISCLAIMER = (
    "This analysis is preliminary. Verify source data and conclusions with the appropriate "
    "real-estate, appraisal, inspection, tax, legal, lending, or other qualified professional."
)
_CENTS = Decimal("0.01")


def _decimal(value: Decimal, field: str) -> Decimal:
    if type(value) is not Decimal:
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value


def preliminary_analysis(
    asking_price: Decimal,
    estimated_value: Decimal,
    estimated_monthly_rent: Decimal,
) -> dict[str, str]:
    asking = _decimal(asking_price, "asking_price")
    value = _decimal(estimated_value, "estimated_value")
    rent = _decimal(estimated_monthly_rent, "estimated_monthly_rent")
    if asking < 0 or rent < 0:
        raise ValueError("prices and rent must not be negative")
    if value <= 0:
        raise ValueError("estimated_value must be positive")
    gap = (value - asking).quantize(_CENTS, rounding=ROUND_HALF_UP)
    yield_percent = ((rent * Decimal(12) / value) * Decimal(100)).quantize(
        _CENTS, rounding=ROUND_HALF_UP
    )
    return {
        "value_gap": format(gap, "f"),
        "gross_rent_yield_percent": format(yield_percent, "f"),
        "disclaimer": PROFESSIONAL_DISCLAIMER,
    }
