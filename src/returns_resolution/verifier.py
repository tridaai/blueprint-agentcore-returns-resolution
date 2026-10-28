"""The verifiers — Law 2's and Law 8's enforcement on drafted text.

The figure verifier (R1) is the original check: the model (or the
offline composer standing in for it) drafts the customer-facing
summary, and before that text may be stored or sent, every amount
($X.XX) and every ISO date it states is checked against the computed
decision's allowed values. Anything else — a transposed digit, an
invented date — is a violation and the summary is rejected.

The messaging verifier (R8) generalises it to EVERY customer-facing
message: figures (amounts, dates, percentages) must equal computed
artefact values; stated FACTS (a disposition, a condition) must
equal the facts of record; and no FORBIDDEN content may appear —
the fraud machinery's internal vocabulary, other customers' data,
internal artefact ids beyond the case's own public references, and
promises the policy pack does not make (the disclosure rules are
versioned data, in messaging.py). Both checks are pure code over
the final rendered text, so they hold whatever produced the draft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from .models import EligibilityDecision, RefundComputation, ReturnLabel, money

_AMOUNT_RE = re.compile(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})")
_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


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


def verify_summary(
    text: str,
    decision: EligibilityDecision,
    computation: RefundComputation | None = None,
    label: ReturnLabel | None = None,
) -> list[str]:
    """Return the list of violations; an empty list means verified.

    The allowed amount universe is the decision's values plus — from
    R2, when a refund computation is supplied — every computed line,
    total and tender allocation of the computation. A figure that only
    the computation produced (a restocking fee, the final refund after
    deductions) verifies against the computation, and nothing else
    new becomes sayable: the universe is still a closed computed set.

    From R6, when a return label is supplied, the label's cost joins
    the universe — a summary may quote the label cost only as the
    label artefact states it. The label's variance and deducted
    amount are already sayable when they equal computation figures;
    the label adds its cost and nothing else.
    """
    violations: list[str] = []
    allowed_amounts = set(decision.allowed_amounts)
    if computation is not None:
        allowed_amounts |= computation.allowed_amounts
    if label is not None:
        allowed_amounts.add(label.cost)
    for amount in extract_amounts(text):
        if amount not in allowed_amounts:
            violations.append(f"amount ${amount} does not equal any computed decision value")
    allowed_dates = decision.allowed_dates
    for stated in extract_dates(text):
        if stated not in allowed_dates:
            violations.append(f"date {stated} does not equal any computed decision date")
    return violations


# ---------------------------------------------------------------------------
# The messaging verifier (R8) — Law 8 over every customer message.
# ---------------------------------------------------------------------------

# How a disposition may be recognised in prose. The templates speak
# through messaging.DISPOSITION_PHRASES; these patterns recognise
# the same statements (and their close variants) wherever they
# appear — including in a free-form note slot. The verb forms are
# matched exactly (restock / restocks / restocked) rather than by
# bare stem: a stem match would read the fee vocabulary —
# "restocking fee" — as a disposition statement, which it is not.
_DISPOSITION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("RESTOCK", re.compile(r"(?i)\brestock(?:s|ed)?\b")),
    ("REFURBISH", re.compile(r"(?i)\brefurbish(?:es|ed)?\b")),
    ("OUTLET", re.compile(r"(?i)\boutlet\b")),
    ("LIQUIDATE", re.compile(r"(?i)\bliquidat(?:e|es|ed)\b")),
    (
        "RETURN_TO_VENDOR",
        re.compile(r"(?i)\breturn(?:ed|s|ing)?\s+to\s+(?:the\s+)?vendor\b|\bsent\s+back\s+to\s+(?:the\s+)?vendor\b"),
    ),
    ("RECYCLE", re.compile(r"(?i)\brecycl(?:e|es|ed)\b")),
    (
        "HOLD_FOR_INSPECTION",
        re.compile(r"(?i)\bheld\s+for\s+inspection\b|\bhold\s+for\s+inspection\b|\binspection\s+hold\b"),
    ),
]

# Conditions are recognised only in the constructions the templates
# (and honest prose) state them in — a bare "used" is ordinary
# English and is not, by itself, a stated condition fact.
_CONDITION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("new_with_tags", re.compile(r"(?i)\bnew\s+with\s+tags\b")),
    ("like_new", re.compile(r"(?i)\blike[-\s]new\b")),
    ("used", re.compile(r"(?i)\bcondition\s+as\s+used\b|\bin\s+used\s+condition\b")),
    (
        "damaged_by_customer",
        re.compile(r"(?i)\bdamaged\s+by\s+(?:the\s+)?customer\b|\bcustomer\s+damage\b"),
    ),
]

# Internal artefact ids: a two-to-four-letter prefix, a dash, and a
# hex suffix — RC-… (computations), FS-… (scores), DD-… (disposition
# decisions), FR-… (reviews), RA-…/RX-…/PR-… (approvals, executions,
# receipts), RL-…/LV-…/PK-… (labels, voids, pickups), GR-…/ADJ-…/
# WHR-… (grading, adjustments, warehouse receipts), MSG-… (messages).
# Carrier and provider references (double-label-…, double-payref-…,
# double-msg-…) never match this shape, and any id that IS a public
# reference of this case is admitted explicitly via the context.
_ARTEFACT_ID_RE = re.compile(r"\b[A-Z]{2,4}-[0-9a-f]{8,}\b")
_CUSTOMER_ID_RE = re.compile(r"\bCUST-[A-Z0-9]+\b")
_CASE_ID_RE = re.compile(r"\bCASE-\d+\b")
_ORDER_ID_RE = re.compile(r"\bORD-\d+\b")


@dataclass
class MessageContext:
    """The verified universe one message is checked against.

    Built by the service from the case's artefacts of record:
    every figure any artefact computed, every disposition and
    condition the record states, the case's own public references,
    and the resolved disclosure rules (the messaging pack's terms
    and patterns plus the scoring pack's signal names and review
    reason codes, derived at build time).
    """

    case_id: str
    order_id: str
    allowed_amounts: set[Decimal] = field(default_factory=set)
    allowed_dates: set[date] = field(default_factory=set)
    allowed_percents: set[Decimal] = field(default_factory=set)
    disposition_facts: set[str] = field(default_factory=set)
    condition_facts: set[str] = field(default_factory=set)
    allowed_refs: set[str] = field(default_factory=set)
    forbidden_terms: list[str] = field(default_factory=list)
    forbidden_patterns: list[str] = field(default_factory=list)


def extract_percents(text: str) -> list[Decimal]:
    percents: list[Decimal] = []
    for raw in _PERCENT_RE.findall(text):
        try:
            percents.append(Decimal(raw))
        except InvalidOperation:
            continue
    return percents


def verify_message_text(text: str, context: MessageContext) -> list[str]:
    """Return the list of violations; an empty list means verified.

    Three families of checks, all over the final rendered text:
    figures (amounts, dates, percentages) against the computed
    universe; facts (dispositions, conditions stated in prose)
    against the facts of record; and forbidden content — the
    disclosure vocabulary, customer identifiers, internal artefact
    ids that are not this case's public references, and references
    to other cases or orders.
    """
    violations: list[str] = []
    for amount in extract_amounts(text):
        if amount not in context.allowed_amounts:
            violations.append(f"amount ${amount} does not equal any computed artefact value")
    for stated in extract_dates(text):
        if stated not in context.allowed_dates:
            violations.append(f"date {stated} does not equal any computed artefact date")
    for percent in extract_percents(text):
        if percent not in context.allowed_percents:
            violations.append(f"percentage {percent}% does not equal any computed rate")

    for disposition, pattern in _DISPOSITION_PATTERNS:
        if pattern.search(text) and disposition not in context.disposition_facts:
            violations.append(
                f"fact: message states disposition {disposition} but the dispositions "
                f"of record are {sorted(context.disposition_facts) or 'none'}"
            )
    for condition, pattern in _CONDITION_PATTERNS:
        if pattern.search(text) and condition not in context.condition_facts:
            violations.append(
                f"fact: message states condition {condition} but the conditions "
                f"of record are {sorted(context.condition_facts) or 'none'}"
            )

    lowered = text.lower()
    for term in context.forbidden_terms:
        if term.lower() in lowered:
            violations.append(
                f"forbidden content: {term!r} must never be disclosed to the customer "
                "(messaging disclosure pack)"
            )
    for pattern in context.forbidden_patterns:
        match = re.search(pattern, text)
        if match:
            violations.append(
                f"forbidden content: {match.group(0)!r} matches a disclosure-pack "
                "prohibition (a score figure, or a promise the policy pack does not make)"
            )

    for token in _CUSTOMER_ID_RE.findall(text):
        violations.append(
            f"forbidden content: customer identifier {token} is internal and never customer-facing"
        )
    for token in _ARTEFACT_ID_RE.findall(text):
        if token not in context.allowed_refs:
            violations.append(
                f"forbidden content: internal artefact id {token} is not a public "
                "reference of this case"
            )
    for token in _CASE_ID_RE.findall(text):
        if token != context.case_id:
            violations.append(f"forbidden content: message references another case ({token})")
    for token in _ORDER_ID_RE.findall(text):
        if token != context.order_id:
            violations.append(f"forbidden content: message references another order ({token})")
    return violations
