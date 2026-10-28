"""Carrier exceptions — when the return journey itself breaks (R18).

Rounds 1–17 assumed the journey: a label is issued, the carrier
moves the parcel, the warehouse receipts it, grading prices what
arrived. R18 is the machinery for the journeys that do not go that
way — and the discipline is the blueprint's own: the exception is
raised IN CODE from the carrier event record, its handling paths
are pack data with their consequences stated, and the money that
follows (a refund on trust, a claim against the carrier) moves
only through named acts and the existing gates.

- **Detection is arithmetic over the event record.** A label the
  carrier never scanned past the pack's tender window, or whose
  scans stop short of receipt past the pack's transit window,
  raises a `label_never_tendered` / `lost_in_transit` exception
  artefact; a carrier `damaged` event raises `damaged_in_transit`.
  The artefact binds the exact events it was raised from, so the
  raising re-performs. Detection is idempotent — the artefact is
  content-addressed over its facts, so a second pass over an
  unmoved record returns the standing artefact.
- **A case never sits in limbo.** A named staff act chooses the
  path — per kind, from the pack: reissue the label, close the
  return as customer-kept, refund as if received, or escalate.
  Each path's effect on eligibility and money is stated in the
  pack, on the artefact, never improvised at the call site.
  While an exception stands open (or escalated), refund
  execution is refused; a customer-kept resolution refuses it
  permanently — the item stayed with the customer.
- **Transit damage is the carrier's, never the customer's.**
  When the carrier's record carries a `damaged` event for the
  label and grading finds the item worse than declared, the
  worsening is attributed to transit (see the grading cascade):
  the fraud re-score does not fire, no recovery position is
  taken against the customer, the refund stands on the declared
  condition, and the disposition follows this pack's transit
  row — the retailer's remedy is the claim below, not the
  customer's money.
- **The carrier claim is assembled, not asserted.** A claim
  artefact binds the label, the carrier events, the receipt and
  grading report where they exist, and the figures of record;
  its amount is the pack's formula over those figures. Its
  lifecycle is insert-only acts — filed, acknowledged, then
  settled or denied by named actors. A settled amount is its
  own ledger fact: it lands in the money summary (R17) as its
  own line and never alters a customer figure retroactively.
- **The customer is told plainly, and never blamed.** The
  exception notice (messaging.py) speaks from this pack's
  wording: the exception is named, transit damage is stated
  as the carrier's, and no refund date is promised while an
  exception makes one unkeepable — the pack's blame vocabulary
  is what the eval gate scans customer payloads for.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, field_validator

from .disposition import CHANNEL_VENDOR, RETURN_TO_VENDOR
from .models import money

EXCEPTION_PACK_VERSION = "2026.10.1"

KIND_LABEL_NEVER_TENDERED = "label_never_tendered"
KIND_LOST_IN_TRANSIT = "lost_in_transit"
KIND_DAMAGED_IN_TRANSIT = "damaged_in_transit"
EXCEPTION_KINDS = [
    KIND_LABEL_NEVER_TENDERED,
    KIND_LOST_IN_TRANSIT,
    KIND_DAMAGED_IN_TRANSIT,
]

# The handling paths a named act may choose. `escalate` is the
# one non-terminal path: it keeps the exception open under a
# named owner. `grading_attribution` is NOT an act — it is the
# derived resolution of a damaged-in-transit exception once the
# grading cascade attributes the damage to transit; it appears
# in state views so the record says how the exception closed.
PATH_REISSUE_LABEL = "reissue_label"
PATH_CUSTOMER_KEPT = "customer_kept"
PATH_REFUND_AS_RECEIVED = "refund_as_received"
PATH_ESCALATE = "escalate"
PATH_GRADING_ATTRIBUTION = "grading_attribution"
# A second derived resolution (R19), on the attribution
# precedent: grading examined the carrier's damage on the
# exception's own label and found the item NOT worsened — the
# exception closes on that finding. It is not an act either;
# it appears in state views so the record says how the
# exception closed. Without it, a damaged exception whose
# item graded as declared (or better) could never resolve,
# and the case sat in limbo behind the carrier guard.
PATH_GRADING_CLEARED = "grading_cleared"
RESOLUTION_PATHS = [PATH_REISSUE_LABEL, PATH_CUSTOMER_KEPT, PATH_REFUND_AS_RECEIVED]

STATE_OPEN = "open"
STATE_ESCALATED = "escalated"
STATE_RESOLVED = "resolved"

CLAIM_STATE_FILED = "filed"
CLAIM_STATE_ACKNOWLEDGED = "acknowledged"
CLAIM_STATE_SETTLED = "settled"
CLAIM_STATE_DENIED = "denied"

CLAIM_ACT_ACKNOWLEDGED = "acknowledged"
CLAIM_ACT_SETTLED = "settled"
CLAIM_ACT_DENIED = "denied"
CLAIM_ACTIONS = [CLAIM_ACT_ACKNOWLEDGED, CLAIM_ACT_SETTLED, CLAIM_ACT_DENIED]

# The transit disposition row: a transit-damaged item is the
# vendor's problem, shipped back through the vendor channel —
# stated here as data, with the rule id the successor decision
# carries as its cell, so its provenance is on the artefact.
TRANSIT_RULE_ID = "EX-TRANSIT-DAMAGE"
TRANSIT_DISPOSITION = RETURN_TO_VENDOR
TRANSIT_CHANNEL = CHANNEL_VENDOR


class CarrierExceptionError(RuntimeError):
    """A carrier-exception refusal — every message names its gap
    (the window not yet met, the path the pack does not offer for
    this kind, the act the state does not allow)."""


class ExceptionPack(BaseModel):
    """The exception pack as data (versioned like every pack):
    the detection windows, the paths each kind offers with the
    consequence of each stated, the claim formula and its
    settlement threshold, the transit disposition row, the
    customer wording, and the blame vocabulary no customer
    payload about an exception may carry."""

    version: str
    never_tendered_days: int
    lost_in_transit_days: int
    damaged_claim_rate: Decimal
    claim_settlement_supervisor_threshold: Decimal
    paths: dict[str, list[str]]
    consequences: dict[str, str]
    claim_deny_reasons: list[str]
    transit_rule_id: str
    transit_disposition: str
    transit_channel: str
    transit_rationale: str
    customer_wording: dict[str, str]
    blame_terms: list[str]

    @field_validator("damaged_claim_rate", "claim_settlement_supervisor_threshold", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))

    def paths_for(self, kind: str) -> list[str]:
        return list(self.paths.get(kind, []))

    def consequence(self, kind: str, path: str) -> str:
        return self.consequences.get(f"{kind}:{path}", "")


EXCEPTION_PACK_V1 = ExceptionPack(
    version=EXCEPTION_PACK_VERSION,
    never_tendered_days=5,
    lost_in_transit_days=10,
    damaged_claim_rate=Decimal("0.50"),
    claim_settlement_supervisor_threshold=Decimal("250.00"),
    paths={
        KIND_LABEL_NEVER_TENDERED: [PATH_REISSUE_LABEL, PATH_CUSTOMER_KEPT, PATH_ESCALATE],
        KIND_LOST_IN_TRANSIT: [PATH_REFUND_AS_RECEIVED, PATH_ESCALATE],
        KIND_DAMAGED_IN_TRANSIT: [PATH_ESCALATE],
    },
    consequences={
        f"{KIND_LABEL_NEVER_TENDERED}:{PATH_REISSUE_LABEL}": (
            "the standing label is voided through the void act and a fresh label may "
            "issue; the refund computation is unchanged; the fresh label's journey "
            "is tracked on its own record, and this exception closes"
        ),
        f"{KIND_LABEL_NEVER_TENDERED}:{PATH_CUSTOMER_KEPT}": (
            "the parcel was never handed to the carrier, so the item remains with "
            "the customer: the return closes, no refund is payable, and refund "
            "execution is refused from this act onward; the eligibility decision "
            "stands as recorded"
        ),
        f"{KIND_LABEL_NEVER_TENDERED}:{PATH_ESCALATE}": (
            "the exception stays open under a named owner; refund execution stays "
            "refused while it stands"
        ),
        f"{KIND_LOST_IN_TRANSIT}:{PATH_REFUND_AS_RECEIVED}": (
            "the refund proceeds on the condition the customer declared, through "
            "the ordinary approval gates, exactly as if the parcel had been "
            "received; the retailer's recovery is the carrier claim, never the "
            "customer's money"
        ),
        f"{KIND_LOST_IN_TRANSIT}:{PATH_ESCALATE}": (
            "the exception stays open under a named owner; refund execution stays "
            "refused while it stands; a carrier claim may be filed from the "
            "escalated state"
        ),
        f"{KIND_DAMAGED_IN_TRANSIT}:{PATH_ESCALATE}": (
            "the exception stays open under a named owner; the substantive "
            "resolution is the grading cascade's transit attribution, and refund "
            "execution stays refused while the exception stands unresolved"
        ),
    },
    claim_deny_reasons=[
        "carrier_liability_refused",
        "insufficient_evidence",
        "outside_claim_window",
    ],
    transit_rule_id=TRANSIT_RULE_ID,
    transit_disposition=TRANSIT_DISPOSITION,
    transit_channel=TRANSIT_CHANNEL,
    transit_rationale=(
        "the carrier recorded the damage in transit: the item goes back to the "
        "vendor through the vendor channel, and the retailer's recovery is the "
        "carrier claim — the customer's refund is untouched"
    ),
    customer_wording={
        "label_never_tendered_open": (
            "We issued your return label {ref}, but the carrier's record shows "
            "no scan of the parcel being handed over. We have recorded a carrier "
            "exception for this return and are resolving it — you do not need "
            "to do anything."
        ),
        "lost_in_transit_open": (
            "The carrier's tracking for your return parcel (label {ref}) has "
            "stopped, and the parcel has not reached us. We have recorded it "
            "as lost in transit and are resolving it with the carrier. This is "
            "the carrier's record — it is not anything you did."
        ),
        "damaged_in_transit": (
            "Your return of the {product} reached us damaged in transit — the "
            "carrier recorded the damage while the parcel was in its care, so "
            "the damage is not attributed to you. Your refund stands at "
            "${total}, computed on the condition you declared."
        ),
        "resolved_grading_cleared": (
            "The carrier recorded your return parcel (label {ref}) as damaged "
            "in transit. When it reached us, our inspection found no damage "
            "to the item itself, so your return proceeds as normal — the "
            "damage was to the parcel, and it is not attributed to you."
        ),
        "resolved_refund_as_received": (
            "The carrier lost your return parcel (label {ref}) in transit. "
            "Your refund of ${total} proceeds on the condition you declared — "
            "you do not need to send anything further."
        ),
        "resolved_customer_kept": (
            "This return has been closed: the carrier's record for label "
            "{ref} shows the parcel was never handed over, so the item "
            "remains with you and no refund is payable for this return."
        ),
        "resolved_reissue_label": (
            "A replacement return label has been arranged for your return of "
            "the {product}; the earlier label ({ref}) no longer works. "
            "Please send the item back with the new label."
        ),
        "escalated": (
            "We have escalated this return's carrier exception with the "
            "carrier (label {ref}). The return stays open while they "
            "investigate, and we will notify you of the outcome."
        ),
    },
    blame_terms=[
        "damaged by the customer",
        "damaged by you",
        "you damaged",
        "your fault",
        "customer damage",
        "damaged it yourself",
    ],
)


def load_exception_pack(version: str | None = None) -> ExceptionPack:
    """Fail-closed loader, like every pack loader in this
    blueprint: an unknown version is an error, never a silent
    fall back."""
    if version is None or version == EXCEPTION_PACK_V1.version:
        return EXCEPTION_PACK_V1
    raise ValueError(f"unknown carrier exception pack version: {version!r}")


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash_id(prefix: str, payload: dict) -> str:
    return prefix + "-" + hashlib.sha256(_canonical(payload).encode()).hexdigest()[:16]


class CarrierException(BaseModel):
    """The exception artefact: raised in code from the carrier
    event record, binding the exact events it was raised from
    (their ids and kinds, in order), the label's issue date and
    the pack window that fired — so the raising re-performs
    byte for byte, and a tampered artefact fails its own
    verification. Insert-only: handling is acts, never edits."""

    exception_id: str
    case_id: str
    label_id: str
    kind: str
    raised_on: date
    issued_on: date
    window_days: int
    event_ids: list[str]
    event_kinds: list[str]
    pack_version: str


class CarrierExceptionAct(BaseModel):
    """One handling act on an exception: a named actor chooses a
    pack path (or escalates), on the record. Acts are insert-only;
    the exception's state is the fold of its acts, never a column
    anyone edits."""

    act_id: str
    exception_id: str
    case_id: str
    action: str  # one of the pack's paths for the kind, or escalate
    actor: str
    acted_on: date
    note: str = ""


class CarrierClaim(BaseModel):
    """The carrier claim artefact: assembled only from artefacts
    of record — the exception, the label, the carrier events it
    binds by id, the warehouse receipt and grading report where
    they exist, and the computation of record — with its amount
    computed by the pack's formula over the bound figures. The
    evidence fingerprint hashes those bindings; verification
    re-assembles the claim from the record and compares every
    field, so a claim that asserts anything the record does not
    hold fails."""

    claim_id: str
    case_id: str
    exception_id: str
    kind: str  # lost_in_transit | damaged_in_transit
    filed_by: str
    filed_on: date
    claimed_amount: Decimal
    item_amount: Decimal
    label_cost: Decimal
    label_id: str
    event_ids: list[str]
    warehouse_receipt_id: str | None = None
    grading_report_id: str | None = None
    computation_id: str | None = None
    evidence_fingerprint: str
    pack_version: str

    @field_validator("claimed_amount", "item_amount", "label_cost", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))


class CarrierClaimAct(BaseModel):
    """One lifecycle act on a claim — acknowledged, settled or
    denied, by a named actor. Insert-only; the claim's state is
    the fold of its acts. A settled act carries the amount the
    carrier actually paid: an external fact, recorded — bounded
    above by the claimed amount in code, and never an edit to
    any customer figure."""

    act_id: str
    claim_id: str
    case_id: str
    action: str  # acknowledged | settled | denied
    actor: str
    acted_on: date
    settled_amount: Decimal | None = None
    reason_code: str | None = None  # the pack's deny vocabulary, on a denial
    note: str = ""

    @field_validator("settled_amount", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal | None:
        return None if v is None else money(Decimal(str(v)))


def build_exception(
    *,
    case_id: str,
    label_id: str,
    kind: str,
    raised_on: date,
    issued_on: date,
    window_days: int,
    event_ids: list[str],
    event_kinds: list[str],
    pack: ExceptionPack,
) -> CarrierException:
    """Mint the exception artefact over its raising facts. The id
    hashes the full fact set, so detection over an unmoved record
    re-mints the identical artefact (and the store keeps the
    standing one), while any moved fact mints a new one."""
    facts = {
        "case_id": case_id,
        "label_id": label_id,
        "kind": kind,
        "issued_on": issued_on.isoformat(),
        "window_days": window_days,
        "event_ids": list(event_ids),
        "pack_version": pack.version,
    }
    return CarrierException(
        exception_id=_hash_id("CEX", facts),
        case_id=case_id,
        label_id=label_id,
        kind=kind,
        raised_on=raised_on,
        issued_on=issued_on,
        window_days=window_days,
        event_ids=list(event_ids),
        event_kinds=list(event_kinds),
        pack_version=pack.version,
    )


def exception_state(
    exception: CarrierException,
    acts: list[CarrierExceptionAct],
    *,
    transit_attributed: bool = False,
    grading_cleared: bool = False,
) -> tuple[str, str | None]:
    """The exception's state, folded from its acts in recorded
    order: (state, resolution path or None). Escalation keeps it
    open-but-escalated; a resolution path closes it. A damaged
    exception with no resolution act whose damage grading has
    attributed to transit is resolved by that attribution — a
    fact of record, stated here rather than written as an act
    nobody performed. A damaged exception whose grading
    finalised on its own label WITHOUT attributing the damage
    (the item graded no worse than declared) is resolved by
    that examination instead (`grading_cleared`, R19): without
    it the exception could never close and the case sat in
    limbo behind the carrier guard."""
    state = STATE_OPEN
    path: str | None = None
    for act in sorted(acts, key=lambda a: (a.acted_on, a.act_id)):
        if act.action == PATH_ESCALATE:
            if state == STATE_OPEN:
                state = STATE_ESCALATED
        elif act.action in RESOLUTION_PATHS:
            state = STATE_RESOLVED
            path = act.action
    if state != STATE_RESOLVED and exception.kind == KIND_DAMAGED_IN_TRANSIT:
        if transit_attributed:
            return STATE_RESOLVED, PATH_GRADING_ATTRIBUTION
        if grading_cleared:
            return STATE_RESOLVED, PATH_GRADING_CLEARED
    return state, path


def claim_amount_for(
    pack: ExceptionPack, kind: str, *, item_amount: Decimal, label_cost: Decimal
) -> Decimal:
    """The pack's claim formula over the figures of record: a
    lost parcel claims the item amount plus the label cost; a
    transit-damaged parcel claims the label cost plus the pack's
    stated rate of the item amount."""
    if kind == KIND_LOST_IN_TRANSIT:
        return money(item_amount + label_cost)
    if kind == KIND_DAMAGED_IN_TRANSIT:
        return money(label_cost + money(item_amount * pack.damaged_claim_rate))
    raise CarrierExceptionError(f"no carrier claim lies for exception kind {kind!r}")


# The lifecycle order within a single day (R19): acts fold by
# their recorded date, and on one date by the order the
# lifecycle itself states — an acknowledgement lands before a
# settlement or denial recorded the same day. Before R19 the
# tie-break was the acts' content-hash ids, so a same-day
# acknowledgement and settlement could fold in either order:
# the settlement was silently inert, the claim read merely
# acknowledged, and a second settlement could be recorded —
# while the money layer counted both (docs/red-team.md RT-01).
_CLAIM_ACT_ORDER = {CLAIM_ACT_ACKNOWLEDGED: 0, CLAIM_ACT_SETTLED: 1, CLAIM_ACT_DENIED: 1}


def _fold_claim_acts(
    acts: list[CarrierClaimAct],
) -> tuple[str, CarrierClaimAct | None]:
    """Fold a claim's acts to (state, the effective settlement
    act or None). The effective settlement is the ONE act the
    fold honours — the settle act that moved the claim from
    acknowledged to settled. Every money reader counts that
    act and no other: an act the fold cannot honour (recorded
    out of order before the R19 date guards, or a duplicate)
    moves no money."""
    state = CLAIM_STATE_FILED
    settlement: CarrierClaimAct | None = None
    ordered = sorted(
        acts,
        key=lambda a: (a.acted_on, _CLAIM_ACT_ORDER.get(a.action, 2), a.act_id),
    )
    for act in ordered:
        if act.action == CLAIM_ACT_ACKNOWLEDGED and state == CLAIM_STATE_FILED:
            state = CLAIM_STATE_ACKNOWLEDGED
        elif act.action == CLAIM_ACT_SETTLED and state == CLAIM_STATE_ACKNOWLEDGED:
            state = CLAIM_STATE_SETTLED
            settlement = act
        elif act.action == CLAIM_ACT_DENIED and state == CLAIM_STATE_ACKNOWLEDGED:
            state = CLAIM_STATE_DENIED
    return state, settlement


def claim_state(claim: CarrierClaim, acts: list[CarrierClaimAct]) -> str:
    """The claim's state, folded from its acts in lifecycle
    order: filed, then acknowledged, then settled or denied
    (terminal)."""
    return _fold_claim_acts(acts)[0]


def effective_settlement(acts: list[CarrierClaimAct]) -> CarrierClaimAct | None:
    """The settlement act of record for one claim — the act
    its state fold honours — or None when the claim does not
    stand settled. This, never a raw scan of settled acts, is
    what the money summary counts (R19, RT-01)."""
    return _fold_claim_acts(acts)[1]


def evidence_fingerprint(payload: dict) -> str:
    """The claim's evidence fingerprint: a hash over the artefact
    bindings and figures the claim asserts — recomputed at
    verification from the record, never trusted from the row."""
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def transit_successor_decision(
    *,
    case_id: str,
    computation_id: str | None,
    matrix_version: str,
    channel_table_version: str,
    category: str | None,
    condition_declared: str,
    graded_condition: str,
    age_days: int | None,
    age_band: str | None,
    damage_event_id: str,
    supersedes: str,
    pack: ExceptionPack,
):
    """Build the disposition a transit-damaged case resolves to,
    as a content-addressed artefact in the matrix decision's
    shape: basis "graded", the exception pack's transit rule as
    its cell, both conditions recorded, `supersedes` linking it
    to the decision it replaces — which is never edited.

    The id hashes the full fact set (including the exception
    pack version, the rule and the carrier's damage event), so
    the artefact is exactly reproducible from the grading report
    and the carrier record it rests on.
    """
    from .models import DispositionDecision

    facts = {
        "case_id": case_id,
        "computation_id": computation_id,
        "matrix_version": matrix_version,
        "channel_table_version": channel_table_version,
        "exception_pack_version": pack.version,
        "category": category,
        "condition_declared": condition_declared,
        "graded_condition": graded_condition,
        "age_days": age_days,
        "age_band": age_band,
        "basis": "graded",
        "rule_id": pack.transit_rule_id,
        "damage_event_id": damage_event_id,
        "supersedes": supersedes,
    }
    return DispositionDecision(
        disposition_id=_hash_id("DD", facts),
        case_id=case_id,
        computation_id=computation_id,
        matrix_version=matrix_version,
        channel_table_version=channel_table_version,
        status="decided",
        disposition=pack.transit_disposition,
        basis="graded",
        cell_id=pack.transit_rule_id,
        channel=pack.transit_channel,
        category=category,
        condition_declared=condition_declared,
        graded_condition=graded_condition,
        age_days=age_days,
        age_band=age_band,
        missing_dimension=None,
        reroutes=[],
        inspection_unknowns=[],
        supersedes=supersedes,
    )


def verify_transit_decision(decision, *, report, pack: ExceptionPack) -> list[str]:
    """Re-perform a transit disposition from the grading report
    it rests on; [] = intact. The report must carry the transit
    attribution and name the carrier damage event; the artefact
    is rebuilt from those facts and compared whole — a tampered
    disposition, channel or link is named."""
    if report.damage_attribution != "transit" or not report.damage_event_id:
        return [
            "the grading report does not attribute this case's damage to transit; "
            "no transit disposition can rest on it"
        ]
    expected = transit_successor_decision(
        case_id=decision.case_id,
        computation_id=decision.computation_id,
        matrix_version=decision.matrix_version,
        channel_table_version=decision.channel_table_version,
        category=decision.category,
        condition_declared=decision.condition_declared,
        graded_condition=decision.graded_condition or report.graded_condition,
        age_days=decision.age_days,
        age_band=decision.age_band,
        damage_event_id=report.damage_event_id,
        supersedes=decision.supersedes or "",
        pack=pack,
    )
    problems: list[str] = []
    if decision.disposition_id != expected.disposition_id:
        problems.append(
            f"disposition_id {decision.disposition_id} != recomputed {expected.disposition_id} "
            "(facts or pack versions changed since this artefact was written)"
        )
    if decision.model_dump(mode="json") != expected.model_dump(mode="json"):
        problems.append("stored disposition differs from a fresh transit resolution over the same facts")
    return problems
