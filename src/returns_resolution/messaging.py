"""Customer messaging — Law 8 made mechanical (R8).

Every customer-facing message the system can emit is a TYPED
TEMPLATE rendered from the case's artefacts of record:

- ``return_summary`` — the R1 summary, migrated onto the template
  registry: its body is composed by the model port exactly as
  before, but it now lives in the message lifecycle like every
  other message.
- ``label_instructions`` — the issued label: reference, service
  level, cost, the ship-by date, a booked pickup where one exists.
- ``grading_outcome_notice`` — what warehouse grading changed:
  the condition disagreement, the re-priced refund, the new
  disposition.
- ``fraud_review_notice`` — the review outcome, cleared or
  confirmed, worded from this module's templates ONLY: it never
  names a signal, a score, a band or a threshold (the disclosure
  pack below is the data that says so, and the verifier enforces
  it — including over free text).
- ``refund_executed_notice`` — the refund as executed: amount,
  date, the tenders it went back to.
- ``adjustment_notice`` — a grading adjustment position: a top-up
  sent, or a recovery standing on the record.

Free-form model text is allowed ONLY inside a template's stated
note slot — one optional closing paragraph, appended verbatim and
then verified like every other character of the message. Anything
outside a template cannot be drafted, so it cannot be sent.

The disclosure pack (versioned data, below) states what a customer
message may never contain: the fraud machinery's vocabulary (the
verifier additionally derives the scoring pack's signal names and
review reason codes at verification time, so a pack change cannot
drift away from the ban), score figures stated in points, and
promises the policy pack does not make — refund timing above all:
the policy pack states no refund timing, so any "within N days"
refund promise is unverifiable by construction and refused.

Sending rides a messaging port; the only implementation in this
build is `LocalMessagingDouble` — A LOCAL DOUBLE, labelled as such
on every delivery record. It delivers nothing to any real customer.
In production the port is bound to the retailer's messaging
provider; the lifecycle and the verifier do not change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from .models import (
    DispositionDecision,
    FraudReviewDecision,
    GradingReport,
    PickupBooking,
    RefundAdjustment,
    RefundComputation,
    RefundReceipt,
    ReturnCase,
    ReturnLabel,
)

MESSAGING_PACK_VERSION = "2026.10.1"

# The closed set of message types. A type outside this set cannot
# be drafted — there is no template for it, so there is no text.
MESSAGE_TYPES = [
    "return_summary",
    "label_instructions",
    "grading_outcome_notice",
    "fraud_review_notice",
    "refund_executed_notice",
    "adjustment_notice",
]

# Message lifecycle states. draft → verified → queued → sent is
# the only road to a customer; refused (verification failed) and
# withdrawn (superseded/stale) are terminal side states, kept on
# the record.
STATE_DRAFT = "draft"
STATE_VERIFIED = "verified"
STATE_REFUSED = "refused"
STATE_QUEUED = "queued"
STATE_SENT = "sent"
STATE_WITHDRAWN = "withdrawn"


class DisclosurePack(BaseModel):
    """What a customer message may never contain — as data.

    `forbidden_terms` are substrings matched case-insensitively.
    `forbidden_patterns` are regexes. The verifier adds, from the
    fraud scoring pack itself, every signal name and every review
    reason code — those vocabularies are forbidden by derivation,
    so they can never be edited out of the ban by editing this
    pack. A disclosure change is a new pack version.
    """

    version: str
    forbidden_terms: list[str]
    forbidden_patterns: list[str]


DISCLOSURE_PACK_V1 = DisclosurePack(
    version=MESSAGING_PACK_VERSION,
    forbidden_terms=[
        # The fraud machinery, in any of its spellings. Band names
        # are included in their machine forms; the customer-facing
        # templates speak of "an additional review", never of this.
        "fraud score",
        "fraud_score",
        "fraud review",
        "fraud_review",
        "fraudreview",
        "FRAUD_REVIEW",
        "scoring pack",
        "score band",
        "risk score",
        "return_velocity",
        "return velocity",
        "serial_returner_share",
        "serial returner",
        "wardrobing_indicator",
        "wardrobing indicator",
        "weight_mismatch",
        "weight mismatch",
        "card_churn",
        "card churn",
    ],
    forbidden_patterns=[
        # A score stated as points — the number a threshold compares
        # against is exactly what must never reach the customer.
        r"\b\d+(?:\.\d+)?\s*points\b",
        # Refund timing promises. The policy pack states NO refund
        # timing, so no timing claim about a refund can verify.
        r"(?i)\bwithin\s+\d+\s*(?:business\s+)?days?\b",
        r"(?i)\b\d+\s*(?:-|–|to)\s*\d+\s*business\s+days?\b",
        r"(?i)\brefund\b[^.]{0,60}\b(?:arrive|arrives|land|lands|reach|reaches)\b",
        # Certainty the policy pack does not offer.
        r"(?i)\bguarantee[ds]?\b",
    ],
)


def load_disclosure_pack(version: str | None = None) -> DisclosurePack:
    """Fail-closed loader: an unknown version is an error, never a
    silent fall back — a message must name the disclosure rules it
    was verified under."""
    if version is None or version == DISCLOSURE_PACK_V1.version:
        return DISCLOSURE_PACK_V1
    raise ValueError(f"unknown messaging disclosure pack version: {version!r}")


# How the templates speak of dispositions and conditions: plain
# words, fixed here, so the verifier's fact patterns and the
# templates can never drift apart — both read this module.
DISPOSITION_PHRASES = {
    "RESTOCK": "restocked",
    "REFURBISH": "refurbished",
    "OUTLET": "sent to our outlet",
    "LIQUIDATE": "liquidated",
    "RETURN_TO_VENDOR": "returned to the vendor",
    "RECYCLE": "recycled",
    "HOLD_FOR_INSPECTION": "held for inspection",
}

CONDITION_PHRASES = {
    "new_with_tags": "new with tags",
    "like_new": "like new",
    "used": "used",
    "damaged_by_customer": "damaged by the customer",
}


@dataclass
class MessageFacts:
    """Everything a template may draw on — artefacts of record only.

    Built by the service per message type (which checks the type's
    prerequisites and refuses, naming the missing artefact, when
    they are absent). A template renders from these fields and
    nothing else.
    """

    case: ReturnCase
    product_name: str
    computation: RefundComputation | None = None  # the current computation
    computations: list[RefundComputation] | None = None  # the chain, oldest first
    disposition: DispositionDecision | None = None  # the current decision
    dispositions: list[DispositionDecision] | None = None  # the chain, oldest first
    label: ReturnLabel | None = None  # the active label
    pickup: PickupBooking | None = None  # the latest booking
    report: GradingReport | None = None
    adjustment: RefundAdjustment | None = None  # the latest adjustment
    review: FraudReviewDecision | None = None  # the latest review decision
    receipt: RefundReceipt | None = None  # the original refund receipt
    top_up_receipt: RefundReceipt | None = None
    summary_text: str | None = None  # the composed body (return_summary)


def _with_note(text: str, model_note: str | None) -> str:
    """The one free-form slot: an optional closing paragraph.

    The note is appended verbatim — the model port's words, not the
    template's — and the verifier then checks it like every other
    character of the message. A note that states an unverified
    figure, a wrong fact or forbidden content refuses the WHOLE
    message; it is never silently trimmed."""
    if model_note and model_note.strip():
        return text + "\n\n" + model_note.strip()
    return text


def _render_return_summary(facts: MessageFacts, model_note: str | None) -> str:
    # The body is the composed summary (the model port's draft over
    # the computed decision, exactly as R1–R7 produced it). The
    # template contributes the lifecycle, not new prose.
    return _with_note(facts.summary_text or "", model_note)


def _render_label_instructions(facts: MessageFacts, model_note: str | None) -> str:
    label = facts.label
    decision = facts.case.decision
    text = (
        f"Your return label {label.carrier_ref} is ready for the {facts.product_name}. "
        f"Service level: {label.service_level}. Label cost: ${label.cost}. "
        f"Please ship the item back by {decision.ship_by}."
    )
    if facts.pickup is not None:
        text += (
            f" A carrier pickup is booked for {facts.pickup.scheduled_on} "
            f"({facts.pickup.window}) — the carrier will collect the labelled parcel."
        )
    return _with_note(text, model_note)


def _render_grading_outcome(facts: MessageFacts, model_note: str | None) -> str:
    report = facts.report
    declared_phrase = CONDITION_PHRASES[report.declared_condition]
    graded_phrase = CONDITION_PHRASES[report.graded_condition]
    sentences = [
        f"We have received and graded your return of the {facts.product_name}. "
        f"You described the item's condition as {declared_phrase}; "
        f"the warehouse graded the condition as {graded_phrase}."
    ]
    by_id = {c.computation_id: c for c in (facts.computations or [])}
    old = by_id.get(report.superseded_computation_id) if report.superseded_computation_id else None
    new = by_id.get(report.new_computation_id) if report.new_computation_id else None
    if old is not None and new is not None and report.refund_delta is not None:
        sentences.append(
            f"Your refund has been re-priced from ${old.refund_total} to "
            f"${new.refund_total} — a difference of ${abs(report.refund_delta)}."
        )
    dispositions = {d.disposition_id: d for d in _dispositions_of(facts)}
    old_disp = dispositions.get(report.superseded_disposition_id) if report.superseded_disposition_id else None
    new_disp = dispositions.get(report.new_disposition_id) if report.new_disposition_id else None
    if new_disp is not None and new_disp.disposition is not None:
        phrase = DISPOSITION_PHRASES[new_disp.disposition]
        if old_disp is not None and old_disp.disposition == "HOLD_FOR_INSPECTION":
            sentences.append(
                f"The item was held for inspection; following that inspection "
                f"it will be {phrase}."
            )
        else:
            sentences.append(f"The item will be {phrase}.")
    return _with_note(" ".join(sentences), model_note)


def _dispositions_of(facts: MessageFacts) -> list[DispositionDecision]:
    return facts.dispositions or []


def _render_fraud_review_outcome(facts: MessageFacts, model_note: str | None) -> str:
    """The review outcome, from the template only.

    Deliberately plain: the review is "an additional review", the
    outcome is stated, the money (where any moves) is the current
    computation's. No signal, score, band, threshold or reason code
    can appear here — the template has no slot for them, and the
    disclosure pack forbids their vocabulary everywhere else.
    """
    review = facts.review
    computation = facts.computation
    if review.outcome == "cleared_with_reason":
        text = (
            f"We have completed an additional review of your return of the "
            f"{facts.product_name}. The review is closed and your return "
            f"will proceed as normal."
        )
        if computation is not None and computation.is_final and computation.refund_total is not None:
            text += f" Your refund due is ${computation.refund_total}."
        return _with_note(text, model_note)
    # confirmed_fraud
    text = (
        f"We have completed an additional review of your return of the "
        f"{facts.product_name}. After that review we are unable to process "
        f"a refund for this return. The item amount was "
        f"${facts.case.decision.item_amount}."
    )
    return _with_note(text, model_note)


def _render_refund_executed(facts: MessageFacts, model_note: str | None) -> str:
    receipt = facts.receipt
    text = (
        f"Your refund of ${receipt.amount} for the {facts.product_name} "
        f"was sent on {receipt.executed_on}."
    )
    if receipt.tender_allocations:
        parts = ", ".join(
            f"${a.amount} to {a.method}" for a in receipt.tender_allocations
        )
        text += f" It was returned as: {parts}."
    return _with_note(text, model_note)


def _render_adjustment(facts: MessageFacts, model_note: str | None) -> str:
    adjustment = facts.adjustment
    by_id = {c.computation_id: c for c in (facts.computations or [])}
    new = by_id.get(adjustment.to_computation_id)
    if adjustment.kind == "top_up_due" and adjustment.status == "executed" and facts.top_up_receipt is not None:
        text = (
            f"After grading, your refund for the {facts.product_name} was "
            f"re-priced to ${new.refund_total}. The additional "
            f"${adjustment.amount} was sent on {facts.top_up_receipt.executed_on}."
        )
    elif adjustment.kind == "top_up_due":
        text = (
            f"After grading, your refund for the {facts.product_name} was "
            f"re-priced to ${new.refund_total}. An additional "
            f"${adjustment.amount} is due to you and is being processed."
        )
    else:  # recovery_due — a position on the record, never a clawback claim
        text = (
            f"After grading, the refund of record for the {facts.product_name} "
            f"is ${new.refund_total}. ${adjustment.amount} more than that was "
            f"refunded to you under the condition originally declared. This "
            f"amount stands as an amount owed on the case record."
        )
    return _with_note(text, model_note)


# The template registry: message type -> renderer. This is the
# whole universe of customer-facing text; drafting a type with no
# entry here is refused by the service before any text exists.
TEMPLATES = {
    "return_summary": _render_return_summary,
    "label_instructions": _render_label_instructions,
    "grading_outcome_notice": _render_grading_outcome,
    "fraud_review_notice": _render_fraud_review_outcome,
    "refund_executed_notice": _render_refund_executed,
    "adjustment_notice": _render_adjustment,
}


# ---------------------------------------------------------------------------
# The messaging port + its local double.
# ---------------------------------------------------------------------------

DOUBLE_MESSAGING = "local-double (messaging stand-in)"


class MessagingProviderError(RuntimeError):
    """The messaging provider failed. Nothing was delivered."""


class ProviderDelivery(BaseModel):
    """What the port returns on a confirmed delivery."""

    provider: str
    provider_ref: str


class MessagingPort(Protocol):
    """The port contract. Production binds the retailer's messaging
    provider here; the local double below is the offline stand-in."""

    provider_name: str

    def send(self, *, message_id: str, case_id: str, text: str) -> ProviderDelivery: ...


class LocalMessagingDouble:
    """LOCAL DOUBLE for the messaging port — delivers NOTHING to
    any real customer.

    Stands in for the retailer's messaging provider so the message
    lifecycle, the verifier gates and the staleness rules can be
    built and graded offline. Production sending rides the
    configured provider, never this class.

    Behaviour contract: idempotent by message id — sending the
    same message twice returns the original delivery record and
    `delivery_count` rises exactly once; `deliveries` keeps the
    exact text delivered per message id, so a gate can prove what
    would have reached the customer.
    """

    provider_name = DOUBLE_MESSAGING

    def __init__(self) -> None:
        self._by_id: dict[str, ProviderDelivery] = {}
        self.deliveries: dict[str, str] = {}
        self.delivery_count = 0

    def send(self, *, message_id: str, case_id: str, text: str) -> ProviderDelivery:
        existing = self._by_id.get(message_id)
        if existing is not None:
            return existing
        blob = json.dumps(
            {"message_id": message_id, "case_id": case_id, "text": text},
            sort_keys=True,
            separators=(",", ":"),
        )
        delivery = ProviderDelivery(
            provider=self.provider_name,
            provider_ref="double-msg-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
        )
        self._by_id[message_id] = delivery
        self.deliveries[message_id] = text
        self.delivery_count += 1
        return delivery
