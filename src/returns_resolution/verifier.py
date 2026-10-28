"""The figure verifier — Law 2's enforcement on drafted text.

The model (or the offline composer standing in for it) drafts the
customer-facing summary. Before that text may be stored or sent, this
verifier extracts every amount ($X.XX) and every ISO date it states
and checks each one against the computed decision's allowed values.
Anything else — a transposed digit, an invented date — is a violation
and the summary is rejected. The check is pure code over the final
rendered text, so it holds whatever profile produced the draft.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .models import EligibilityDecision, money

_AMOUNT_RE = re.compile(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})")
_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def extract_amounts(text: str) -> list[Decimal]:
    amounts: list[Decimal] = []
    for raw in _AMOUNT_RE.findall(text):
        try:
            amounts.append(money(Decimal(raw.replace(",", ""))))
        except InvalidOperation:
            continue
    return amounts


def extract_dates(text: str) -> list[date]:
    dates: list[date] = []
    for year, month, day in _DATE_RE.findall(text):
        try:
            dates.append(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    return dates


def verify_summary(text: str, decision: EligibilityDecision) -> list[str]:
    """Return the list of violations; an empty list means verified."""
    violations: list[str] = []
    allowed_amounts = decision.allowed_amounts
    for amount in extract_amounts(text):
        if amount not in allowed_amounts:
            violations.append(f"amount ${amount} does not equal any computed decision value")
    allowed_dates = decision.allowed_dates
    for stated in extract_dates(text):
        if stated not in allowed_dates:
            violations.append(f"date {stated} does not equal any computed decision date")
    return violations
