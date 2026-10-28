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
    # Supersession (R7): a computation re-run on the warehouse-graded
    # condition is a NEW artefact whose `supersedes` names the one it
    # replaces — the replaced artefact is never edited. Computations
    # before R7 (and every declared-basis computation) carry None.
    supersedes: str | None = None
    # Which condition the money was computed over: "declared" (the
    # customer's statement, R2) or "graded" (the warehouse's, R7 —
    # with the graded condition itself recorded alongside).
    condition_basis: str = "declared"
    graded_condition: str | None = None

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
    # The warehouse-graded condition (R7), recorded on graded-basis
    # decisions alongside the customer's original declaration. The
    # decision was evaluated over this condition; `condition_declared`
    # still states what the customer claimed, so the disagreement —
    # when there is one — is on the artefact, not lost.
    graded_condition: str | None = None
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
    # The provenance of the history the score was computed over
    # (R9): "store" for the artefacts of record, "memory" when the
    # engine's fact set was reconstructed from memory-port records.
    # A tag, not an input: it is excluded from the inputs
    # fingerprint and the score id, so a memory-sourced score over
    # the same facts is the SAME artefact — which is what makes the
    # store/memory parity check a comparison of equals.
    history_source: str = "store"

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


class RefundApproval(BaseModel):
    """A named approval act (R5) — Law 5's human gate, on the record.

    The act approves ONE pinned computation: its id and its refund
    total are recorded, so an approval taken against a computation
    that has since been superseded can never be mistaken for an
    approval of the current one — the gates only count approvals
    pinned to the case's current computation. `tier` is the approval
    requirement the computation's total fell under at act time,
    under the pack version recorded here.
    """

    approval_id: str
    case_id: str
    computation_id: str
    approver: str
    role: str
    as_of: date
    amount: Decimal
    tier: str  # auto | single | dual
    policy_pack_version: str

    @field_validator("amount", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))


class ExecutionTransition(BaseModel):
    """One state change of a refund execution, on the record."""

    state: str  # requested | executing | executed | failed
    at: date
    detail: str = ""


class RefundExecution(BaseModel):
    """One refund execution attempt (R5) — the retry-safe state machine.

    Keyed by its mandatory idempotency key: a retry with the same key
    continues THIS record (its transitions accumulate), a retry with
    a different key after money moved is refused as a duplicate
    refund before a record is even opened. `status` mirrors the last
    transition. A failed execution holds no receipt — the failure is
    recorded here, and the case stays executable.
    """

    execution_id: str
    case_id: str
    computation_id: str
    idempotency_key: str
    amount: Decimal
    provider: str
    status: str  # requested | executing | executed | failed
    transitions: list[ExecutionTransition] = []
    receipt_id: str | None = None
    failure_reason: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))


class RefundReceipt(BaseModel):
    """The money artefact (R5): proof a refund executed exactly once.

    Written only when the payments port confirms the movement. One
    receipt exists per executed computation and per idempotency key
    — both keys are unique in the store, which is what makes a
    double refund structurally impossible rather than unlikely. The
    tender allocation is the computation's own, carried through so
    the receipt states where the money went, tender by tender.
    """

    receipt_id: str
    case_id: str
    computation_id: str
    execution_id: str
    idempotency_key: str
    provider: str
    provider_ref: str
    amount: Decimal
    tender_allocations: list[TenderAllocation] = []
    status: str = "executed"
    executed_on: date

    @field_validator("amount", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))


class WarehouseReceipt(BaseModel):
    """The warehouse receipt act (R7): a named receiver takes the
    returned parcel into the warehouse, on the record.

    Recorded only once the carrier's own events say the parcel was
    received — the warehouse does not receipt parcels the carrier
    still shows in transit. The receiver is named because grading
    (R7) enforces separation of duties against this name: whoever
    received the parcel may not grade it.
    """

    warehouse_receipt_id: str
    case_id: str
    label_id: str
    received_by: str
    received_on: date


class GradingReport(BaseModel):
    """The warehouse grading act (R7) and its cascade, on one record.

    A named grader states the item's GRADED condition — the fact the
    rest of the case then re-derives from: the disposition is
    superseded by a graded-basis decision, the refund computation is
    re-run over the graded condition, and any money difference
    against an already-executed refund becomes a stated adjustment.
    Every link of that cascade is recorded here by artefact id; the
    superseded artefacts themselves are never edited.

    For a case held for inspection, `functional_test_result` and
    `data_wipe_status` are what inspection had to establish — a
    report on a held case without them does not finalise.
    `received_weight_grams` is the warehouse weigh-in: once on
    record it is the weight the fraud engine scores against (a
    re-score supersedes the intake-scan score, by link).
    """

    report_id: str
    case_id: str
    label_id: str
    warehouse_receipt_id: str
    grader: str
    graded_on: date
    declared_condition: str
    graded_condition: str
    functional_test_result: str | None = None  # pass | fail
    data_wipe_status: str | None = None  # verified | not_verified
    damage_notes: str = ""
    received_weight_grams: int | None = None
    wear_evidence: bool = False
    grading_pack_version: str
    # The cascade, by artefact id (None where a step did not fire).
    superseded_disposition_id: str | None = None
    new_disposition_id: str | None = None
    superseded_computation_id: str | None = None
    new_computation_id: str | None = None
    refund_delta: Decimal | None = None
    adjustment_id: str | None = None
    fraud_rescore_triggered: bool = False
    new_fraud_score_id: str | None = None

    @field_validator("refund_delta", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal | None:
        return None if v is None else money(Decimal(str(v)))


class RefundAdjustment(BaseModel):
    """A stated money position created by grading after execution (R7).

    Grading re-prices a refund that has ALREADY been paid. The
    difference is never silently absorbed:

    - `recovery_due` (the customer was over-refunded): the position
      stands as a recorded receivable, with the approval tier its
      collection would require noted on the artefact. This round
      builds NO clawback execution — collecting is a later, gated
      act that does not exist yet, and the artefact says so.
    - `top_up_due` (the customer was under-refunded): the position
      is settled through the SAME gates as any refund — approvals
      re-checked against the NEW computation, the fraud guard, and
      an idempotency key in the top-up key domain — by
      `execute_top_up`, which fills in the execution/receipt links
      and flips `status` to "executed".
    """

    adjustment_id: str
    case_id: str
    kind: str  # recovery_due | top_up_due
    amount: Decimal
    reason: str
    from_computation_id: str
    to_computation_id: str
    source_receipt_id: str | None = None
    grading_report_id: str
    approval_tier: str
    approval_note: str
    status: str = "open"  # open | executed (top_up_due only)
    created_on: date
    top_up_execution_id: str | None = None
    top_up_receipt_id: str | None = None
    idempotency_key: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))


class LabelCostReconciliation(BaseModel):
    """The label cost against the computation (R6) — a mismatch is
    surfaced here, on the artefact, never absorbed silently.

    `deducted_amount` is the R2 computation's return-shipping line —
    what the customer was charged for the return. `label_cost` is
    what the carrier actually charges for the label that fired.
    `status`:
    - "matched": the two are equal; nothing to explain.
    - "retailer_absorbed": the pack makes this category a free
      return (deduction 0.00 by rule) and the retailer carries the
      label cost — the variance is stated, and it is the pack's
      stated intent, not an error.
    - "variance": the two differ for any other reason; the variance
      stands on the record for a human to see.
    - "not_reconcilable": the computation's shipping line is not
      computable, so there is no deduction to reconcile against —
      the missing input is named.
    """

    deducted_amount: Decimal | None = None
    label_cost: Decimal
    variance: Decimal | None = None
    status: str  # matched | retailer_absorbed | variance | not_reconcilable
    detail: str = ""

    @field_validator("deducted_amount", "label_cost", "variance", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal | None:
        return None if v is None else money(Decimal(str(v)))


class ReturnLabel(BaseModel):
    """A return shipping label (R6) — a stored artefact of record.

    Issued only through the `create_return_label` gateway tool, for
    an eligible case with its fraud path open. One ACTIVE label per
    case: re-issuing returns the active artefact, and a fresh label
    may issue only after the active one is voided (the new label's
    `supersedes` names the voided one). The service level and cost
    come from the policy pack's carrier schedule — the artefact
    records the pack version and the exact line that fired.
    """

    label_id: str
    case_id: str
    provider: str
    carrier_ref: str
    service_level: str
    category: str
    weight_grams: int
    cost: Decimal
    reconciliation: LabelCostReconciliation
    status: str  # issued | voided
    issued_on: date
    policy_pack_version: str
    via_call_id: str | None = None
    supersedes: str | None = None

    @field_validator("cost", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))


class LabelVoid(BaseModel):
    """The void act (R6): a named actor voids a label, on the record.

    Voiding never edits the label artefact's history — the label's
    status flips to voided and this act stands as the reason. Only
    an active label can be voided, and only once.
    """

    void_id: str
    label_id: str
    case_id: str
    voided_by: str
    reason: str
    voided_on: date


class PickupBooking(BaseModel):
    """A carrier pickup booking (R6) — bulky categories only.

    Booked through the `book_pickup` gateway tool against an active
    label: the carrier collects the parcel instead of the customer
    dropping it off. The pack flags which categories are bulky; the
    refusal for any other category names that rule.
    """

    pickup_id: str
    case_id: str
    label_id: str
    provider: str
    carrier_ref: str
    scheduled_on: date
    window: str
    status: str = "booked"
    via_call_id: str | None = None


class CarrierEvent(BaseModel):
    """One tracking event from the carrier port (R6), read-only.

    Events are the carrier's record, produced by the port — the
    local double derives a deterministic script per label
    (tendered → in transit → received). Nothing in the service
    writes or edits them.
    """

    event_id: str
    label_id: str
    carrier_ref: str
    kind: str  # tendered | in_transit | received
    at: str  # ISO datetime from the carrier
    detail: str = ""


class ToolCallRecord(BaseModel):
    """One gateway tool invocation, on the record (R6).

    Every call through the ToolGateway — success or refusal — is
    stored: the tool, the case it acted on, a hash of the exact
    inputs, the outcome, and what the call produced or why it
    refused. This is the audit trail Law 6 asks for: carrier and
    label actions happen only as recorded tool calls.
    """

    call_id: str
    tool: str
    case_id: str
    inputs_hash: str
    status: str  # ok | error
    called_on: date
    result_ref: str | None = None
    error: str | None = None


class TimelineEntry(BaseModel):
    """One artefact of record on a case's timeline (R6), derived."""

    at: date
    stage: str
    artefact_id: str
    detail: str = ""


class TimelineGap(BaseModel):
    """An artefact the case's state expects but the record lacks.

    Named, never papered over: the timeline states what is missing
    and why it is expected, so a reader never mistakes an absent
    artefact for a completed step.
    """

    kind: str
    detail: str


class CaseTimeline(BaseModel):
    """The derived, read-only chronological view of a case (R6).

    Assembled in code at read time from the artefacts of record —
    never stored as prose. Entries are ordered by their recorded
    dates; `gaps` names expected-but-absent artefacts.
    """

    case_id: str
    as_of: date
    entries: list[TimelineEntry] = []
    gaps: list[TimelineGap] = []


class MessageTransition(BaseModel):
    """One state change of a customer message, on the record (R8).

    The lifecycle is draft → verified → queued → sent, with refused
    (verification failed) and withdrawn (superseded or stale) as
    terminal side states. Every transition — including a refusal,
    whose detail names the violations' count, and a withdrawal,
    whose detail names the replacement — is recorded here, so the
    message's whole road is auditable from the artefact itself.
    """

    state: str  # draft | verified | refused | queued | sent | withdrawn
    at: date
    actor: str = ""
    detail: str = ""


class CustomerMessage(BaseModel):
    """One customer-facing message (R8) — a stored artefact.

    Rendered from a typed template over the case's artefacts of
    record, verified by the messaging verifier before it may be
    queued, and sent only through the messaging port. The artefact
    pins what it was rendered against: `artefact_refs` names the
    artefacts (by role and id) and `artefact_fingerprint` hashes
    their payloads — at send time the fingerprint is recomputed,
    and a mismatch means the case moved under the message: it is
    withdrawn and re-drafted, never sent stale. A message that
    failed verification is stored as `refused` with its violations
    listed — never sent, never silently fixed.
    """

    message_id: str
    case_id: str
    message_type: str  # one of messaging.MESSAGE_TYPES
    status: str  # draft | verified | refused | queued | sent | withdrawn
    # Per-case creation order (1-based): message ids are content
    # hashes and do not sort chronologically, so the sequence is
    # what "the case's latest message of a type" means.
    seq: int = 0
    text: str
    violations: list[str] = []
    template_version: str
    disclosure_pack_version: str
    artefact_refs: dict[str, str] = {}
    artefact_fingerprint: str
    provider: str | None = None
    provider_ref: str | None = None
    supersedes: str | None = None
    created_on: date
    transitions: list[MessageTransition] = []


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
