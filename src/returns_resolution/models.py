"""Domain models for the returns blueprint.

Money is Decimal everywhere and quantised to cents at construction, so
a figure that enters the system can be compared exactly against the
figure the engine later computes — that exactness is what the figure
verifier (verifier.py) relies on.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field, field_validator

CENT = Decimal("0.01")


def money(value: Decimal | str | int) -> Decimal:
    """Quantise a value to cents, HALF_UP — the one rounding rule."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class Product(BaseModel):
    sku: str
    name: str
    category: str
    unit_price: Decimal
    final_sale: bool = False

    @field_validator("unit_price", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))


class OrderLine(BaseModel):
    sku: str
    qty: int = Field(gt=0)
    unit_price: Decimal

    @field_validator("unit_price", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))

    @property
    def line_total(self) -> Decimal:
        return money(self.unit_price * self.qty)


class TenderPortion(BaseModel):
    """One payment tender an order was paid with (card, gift card, …)."""

    method: str
    amount: Decimal

    @field_validator("amount", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))


class Order(BaseModel):
    order_id: str
    customer_id: str
    placed_on: date
    delivered_on: date
    lines: list[OrderLine]
    shipping_paid: Decimal = Decimal("0.00")
    # How the order was paid, in recorded order. Absent on orders whose
    # source system did not supply a tender breakdown — the refund
    # engine then reports the tender allocation as not computable
    # (naming this input) rather than inventing a split.
    tenders: list[TenderPortion] | None = None

    @field_validator("shipping_paid", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))

    @property
    def items_total(self) -> Decimal:
        return money(sum((line.line_total for line in self.lines), Decimal("0.00")))

    @property
    def order_total(self) -> Decimal:
        return money(self.items_total + self.shipping_paid)

    def line_for(self, sku: str) -> OrderLine | None:
        for line in self.lines:
            if line.sku == sku:
                return line
        return None


class ReturnRequest(BaseModel):
    """One planted or submitted request to return items from an order."""

    request_id: str
    order_id: str
    sku: str
    qty: int = Field(gt=0)
    reason_code: str
    # Customer-declared condition: new_with_tags | like_new | used |
    # damaged_by_customer. The declaration is an input to policy, never
    # a decision — warehouse grading arrives in a later round.
    condition_declared: str
    requested_on: date
    channel: str = "web"
    # The carrier drop-off scan weight in grams, when the return was
    # scanned at intake (R4). Absent means nobody weighed the parcel —
    # the fraud engine then reports its weight signal as not
    # computable and names this input, rather than assuming a weight.
    returned_weight_grams: int | None = None


class ClauseHit(BaseModel):
    """One policy clause that fired, with its effect and the facts."""

    clause_id: str
    effect: str  # "allow" | "deny"
    detail: str


class EligibilityDecision(BaseModel):
    """The computed decision. Every later figure traces back to this."""

    case_id: str
    eligible: bool
    policy_pack_version: str
    clauses: list[ClauseHit]
    order_id: str
    sku: str
    qty: int
    item_amount: Decimal
    refund_due: Decimal
    delivered_on: date
    requested_on: date
    window_end: date | None
    ship_by: date | None

    @field_validator("item_amount", "refund_due", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))

    @property
    def allowed_amounts(self) -> set[Decimal]:
        return {self.item_amount, self.refund_due}

    @property
    def allowed_dates(self) -> set[date]:
        dates = {self.delivered_on, self.requested_on}
        if self.window_end is not None:
            dates.add(self.window_end)
        if self.ship_by is not None:
            dates.add(self.ship_by)
        return dates


class ComputationLine(BaseModel):
    """One named line of a refund computation, with its derivation.

    `amount` is None exactly when the line is not computable; the line
    then names the missing input instead of carrying a silent zero.
    `inputs` + `rule` + `policy_ref` are the trace: a verifier
    re-performs the arithmetic from these pinned inputs mechanically.
    """

    line_id: str  # item_amount | restocking_fee | return_shipping
    kind: str  # credit | deduction
    amount: Decimal | None = None
    status: str  # computed | not_computable
    missing_input: str | None = None
    inputs: dict[str, str] = {}
    rule: str = ""
    policy_ref: str = ""

    @field_validator("amount", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal | None:
        return None if v is None else money(Decimal(str(v)))


class TenderAllocation(BaseModel):
    """Part of the refund assigned to one payment tender."""

    method: str
    amount: Decimal

    @field_validator("amount", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))


class NotComputableComponent(BaseModel):
    component: str
    missing_input: str


class RefundComputation(BaseModel):
    """The money of record for a case (R2) — a content-addressed artefact.

    Pinned to the eligibility decision, the policy pack version and the
    order as stored (their canonical fingerprints are inputs to the
    computation id), so a computation can never silently drift from the
    decision and order it was computed from. `refund_total` is None
    while any money line is not computable; `is_final` says so plainly.
    The tender allocation is reported separately: its absence does not
    change the total, and the artefact states that rather than hiding it.
    """

    computation_id: str
    case_id: str
    engine_version: str
    policy_pack_version: str
    inputs_fingerprint: str
    eligible: bool
    lines: list[ComputationLine] = []
    total_deductions: Decimal | None = None
    refund_total: Decimal | None = None
    is_final: bool = False
    tender_allocations: list[TenderAllocation] = []
    not_computable: list[NotComputableComponent] = []

    @field_validator("total_deductions", "refund_total", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal | None:
        return None if v is None else money(Decimal(str(v)))

    @property
    def allowed_amounts(self) -> set[Decimal]:
        amounts = {line.amount for line in self.lines if line.amount is not None}
        amounts |= {a.amount for a in self.tender_allocations}
        if self.total_deductions is not None:
            amounts.add(self.total_deductions)
        if self.refund_total is not None:
            amounts.add(self.refund_total)
        return amounts


class RerouteStep(BaseModel):
    """One hop of a channel reroute: the cell whose channel was closed
    and the matrix-stated next cell the walk fell to. Recorded on the
    decision — a reroute that is not on the record did not happen
    (and one that happened is never silent)."""

    from_cell_id: str
    closed_channel: str
    to_cell_id: str


class DispositionDecision(BaseModel):
    """Where the returned item goes (R3) — a content-addressed artefact.

    Pinned to the case, the refund computation it was decided
    alongside (where one exists), the matrix version and the channel
    table version, and the exact facts evaluated: category, the
    customer's declared condition, item age in days and its band.
    `basis` is "declared" in R3 — the decision rests on the declared
    condition; warehouse grading (R7) will supersede it with a new
    linked decision via `supersedes`, never an in-place edit.

    `status` is "decided" exactly when a cell fired through an open
    channel; otherwise "not_decidable" with `missing_dimension`
    naming what the matrix could not be evaluated over — never a
    default disposition standing in for a missing fact.
    """

    disposition_id: str
    case_id: str
    computation_id: str | None = None
    matrix_version: str
    channel_table_version: str
    status: str  # decided | not_decidable
    disposition: str | None = None
    basis: str = "declared"
    cell_id: str | None = None
    channel: str | None = None
    category: str | None = None
    condition_declared: str
    age_days: int | None = None
    age_band: str | None = None
    missing_dimension: str | None = None
    reroutes: list[RerouteStep] = []
    inspection_unknowns: list[str] = []
    supersedes: str | None = None


class SignalResult(BaseModel):
    """One fraud signal's verdict (R4), with its inputs on the record.

    `status` is "computed" when the signal was measured; a signal
    whose evidence base is too thin is "insufficient_data" and one
    whose input is simply absent is "not_computable" — both name the
    situation in `missing_input` and contribute exactly zero, stated,
    never a silent assumption dressed as a measurement.
    """

    signal_id: str
    status: str  # computed | insufficient_data | not_computable
    raw_value: str | None = None
    contribution: Decimal = Decimal("0")
    inputs: dict[str, str] = {}
    missing_input: str | None = None
    rule: str = ""

    @field_validator("contribution", mode="before")
    @classmethod
    def _decimal(cls, v: object) -> Decimal:
        return Decimal(str(v)).quantize(Decimal("0.01"))


class FraudScore(BaseModel):
    """A case's fraud score (R4) — a content-addressed artefact.

    Pinned to the case, the scoring pack version and the exact input
    artefacts it was computed from (the customer's stored orders and
    stored cases, by id, with a fingerprint over their contents and
    the pack). A re-score after new evidence is a NEW artefact whose
    `supersedes` names the score it replaces — scores are never
    edited, so a block can never be lifted by rewriting history.
    """

    score_id: str
    case_id: str
    customer_id: str
    pack_version: str
    inputs_fingerprint: str
    input_case_ids: list[str] = []
    input_order_ids: list[str] = []
    signals: list[SignalResult] = []
    total_score: Decimal = Decimal("0")
    band: str  # CLEAR | MONITOR | FRAUD_REVIEW
    as_of: date
    supersedes: str | None = None

    @field_validator("total_score", mode="before")
    @classmethod
    def _decimal(cls, v: object) -> Decimal:
        return Decimal(str(v)).quantize(Decimal("0.01"))


class FraudReviewDecision(BaseModel):
    """The named fraud-review act (R4) — the ONLY way a FRAUD_REVIEW
    block is lifted or confirmed. It decides on one pinned score
    artefact; the score itself is never touched by the decision."""

    review_id: str
    case_id: str
    score_id: str
    reviewer: str
    outcome: str  # confirmed_fraud | cleared_with_reason
    reason_code: str
    pack_version: str


class ReturnCase(BaseModel):
    case_id: str
    request: ReturnRequest
    order_id: str
    customer_id: str
    status: str  # decided | summary_ready | summary_failed
    decision: EligibilityDecision | None = None
    computation: RefundComputation | None = None
    disposition: DispositionDecision | None = None
    summary: str | None = None
    # Fraud scoring (R4). `fraud_state` is derived from the current
    # score's band plus any review decisions of record:
    # clear | monitor | fraud_review (blocked) | fraud_confirmed
    # (blocked, sticky) | fraud_cleared (block lifted by decision).
    fraud_score: FraudScore | None = None
    fraud_review: FraudReviewDecision | None = None
    fraud_state: str = "clear"
