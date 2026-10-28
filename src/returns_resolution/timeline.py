"""The case timeline — a derived view, never stored prose (R6).

Assembled in code at read time from the artefacts of record: the
intake request, the eligibility decision, the refund computation,
the disposition, the fraud score chain and its review decisions,
approvals, refund executions and the receipt, labels and their
voids, pickups, and the carrier events read back through the
carrier port. Every entry carries the recorded date of its
artefact and the artefact's own id, so a reader can walk from any
line of the timeline to the record it summarises.

Gaps are first-class: an artefact type the case's state expects
but the record lacks is NAMED in `gaps` — a refund executed with
no label of record, a ship-by date passed with no label issued, a
fraud block still waiting on its named review decision. An absent
artefact is never papered over with a plausible line.
"""

from __future__ import annotations

from datetime import date

from .fraud import BLOCKED_STATES
from .models import (
    CarrierEvent,
    CaseTimeline,
    DispositionDecision,
    FraudReviewDecision,
    FraudScore,
    LabelVoid,
    PickupBooking,
    RefundApproval,
    RefundComputation,
    RefundExecution,
    RefundReceipt,
    ReturnCase,
    ReturnLabel,
    TimelineEntry,
    TimelineGap,
)

# Same-day ordering follows the workflow: what was decided first
# reads first. Dates are the primary key; this rank only breaks
# ties between artefacts recorded on the same day.
_STAGE_RANK = {
    "intake": 10,
    "decision": 20,
    "computation": 30,
    "disposition": 40,
    "fraud_score": 50,
    "fraud_review": 60,
    "label": 70,
    "label_void": 75,
    "pickup": 80,
    "carrier_event": 85,
    "approval": 90,
    "refund_execution": 100,
    "receipt": 110,
}


def build_timeline(
    *,
    case: ReturnCase,
    computation: RefundComputation | None,
    disposition: DispositionDecision | None,
    fraud_scores: list[FraudScore],
    fraud_reviews: list[FraudReviewDecision],
    approvals: list[RefundApproval],
    executions: list[RefundExecution],
    receipt: RefundReceipt | None,
    labels: list[ReturnLabel],
    voids: list[LabelVoid],
    pickups: list[PickupBooking],
    events_by_label: dict[str, list[CarrierEvent]],
    as_of: date,
) -> CaseTimeline:
    staged: list[tuple[date, int, int, TimelineEntry]] = []
    seq = 0

    def add(at: date, stage: str, artefact_id: str, detail: str) -> None:
        nonlocal seq
        staged.append(
            (at, _STAGE_RANK[stage], seq, TimelineEntry(at=at, stage=stage, artefact_id=artefact_id, detail=detail))
        )
        seq += 1

    request = case.request
    add(
        request.requested_on,
        "intake",
        request.request_id,
        f"return requested: {request.sku} x{request.qty}, declared {request.condition_declared}, "
        f"reason {request.reason_code} (channel {request.channel})",
    )

    decision = case.decision
    if decision is not None:
        clauses = ", ".join(f"{c.clause_id}({c.effect})" for c in decision.clauses)
        add(
            decision.requested_on,
            "decision",
            f"decision:{case.case_id}",
            f"eligibility {'approved' if decision.eligible else 'denied'} under policy pack "
            f"{decision.policy_pack_version}; clauses: {clauses}",
        )

    if computation is not None:
        if not computation.eligible:
            detail = "refund computation of record: no refund due (eligibility denied), final $0.00"
        elif computation.is_final:
            detail = f"refund computed: ${computation.refund_total} final, under pack {computation.policy_pack_version}"
        else:
            missing = ", ".join(c.missing_input for c in computation.not_computable)
            detail = f"refund computation NOT final — waiting on: {missing}"
        add(decision.requested_on if decision else request.requested_on, "computation", computation.computation_id, detail)

    if disposition is not None:
        if disposition.status == "decided":
            detail = (
                f"disposition {disposition.disposition} via {disposition.channel} "
                f"(cell {disposition.cell_id}, matrix {disposition.matrix_version}, basis: {disposition.basis})"
            )
        else:
            detail = f"disposition NOT DECIDABLE — missing dimension: {disposition.missing_dimension}"
        add(decision.requested_on if decision else request.requested_on, "disposition", disposition.disposition_id, detail)

    score_dates = {s.score_id: s.as_of for s in fraud_scores}
    for score in fraud_scores:
        add(
            score.as_of,
            "fraud_score",
            score.score_id,
            f"fraud score {score.total_score} → {score.band} (pack {score.pack_version})"
            + (f"; supersedes {score.supersedes}" if score.supersedes else ""),
        )
    for review in fraud_reviews:
        add(
            score_dates.get(review.score_id, request.requested_on),
            "fraud_review",
            review.review_id,
            f"fraud review by {review.reviewer}: {review.outcome} ({review.reason_code}) "
            f"on score {review.score_id}",
        )

    for label in labels:
        rec = label.reconciliation
        add(
            label.issued_on,
            "label",
            label.label_id,
            f"return label {label.carrier_ref} issued via {label.provider}: {label.service_level}, "
            f"{label.weight_grams} g, cost ${label.cost} (reconciliation: {rec.status})",
        )
    for void in voids:
        add(
            void.voided_on,
            "label_void",
            void.void_id,
            f"label {void.label_id} voided by {void.voided_by}: {void.reason}",
        )
    for pickup in pickups:
        add(
            pickup.scheduled_on,
            "pickup",
            pickup.pickup_id,
            f"carrier pickup booked ({pickup.carrier_ref}) for label {pickup.label_id}, "
            f"window {pickup.window}",
        )
    for label in labels:
        for event in events_by_label.get(label.label_id, []):
            add(
                date.fromisoformat(event.at[:10]),
                "carrier_event",
                event.event_id,
                f"carrier event {event.kind} on label {label.carrier_ref}: {event.detail}",
            )

    for approval in approvals:
        add(
            approval.as_of,
            "approval",
            approval.approval_id,
            f"refund of ${approval.amount} approved by {approval.approver} ({approval.role}) "
            f"for computation {approval.computation_id} (tier {approval.tier})",
        )
    for execution in executions:
        for transition in execution.transitions:
            detail = f"refund execution {transition.state}"
            if transition.detail:
                detail += f" — {transition.detail}"
            if execution.status == "failed" and transition.state == "failed" and execution.failure_reason:
                detail += f" (reason on record: {execution.failure_reason})"
            add(transition.at, "refund_execution", execution.execution_id, detail)
    if receipt is not None:
        add(
            receipt.executed_on,
            "receipt",
            receipt.receipt_id,
            f"refund executed: ${receipt.amount} via {receipt.provider} "
            f"(ref {receipt.provider_ref}, key {receipt.idempotency_key})",
        )

    staged.sort(key=lambda item: (item[0], item[1], item[2]))
    entries = [entry for _at, _rank, _seq, entry in staged]

    # --- gaps: expected by the case's state, absent from the record ---
    gaps: list[TimelineGap] = []
    if decision is not None and decision.eligible:
        if computation is None:
            gaps.append(
                TimelineGap(kind="refund_computation", detail="eligible case has no refund computation of record")
            )
        if disposition is None:
            gaps.append(
                TimelineGap(kind="disposition", detail="eligible case has no disposition decision of record")
            )
        if disposition is not None and disposition.status == "decided":
            active = [label for label in labels if label.status == "issued"]
            if not active:
                if voids:
                    gaps.append(
                        TimelineGap(
                            kind="return_label",
                            detail="the case's label was voided and no replacement label has been issued",
                        )
                    )
                elif receipt is not None:
                    gaps.append(
                        TimelineGap(
                            kind="return_label",
                            detail="the refund was executed with no return label of record",
                        )
                    )
                elif decision.ship_by is not None and as_of > decision.ship_by:
                    gaps.append(
                        TimelineGap(
                            kind="return_label",
                            detail=(
                                f"the ship-by date {decision.ship_by} has passed and no return "
                                "label has been issued"
                            ),
                        )
                    )
        if case.fraud_state in BLOCKED_STATES and not fraud_reviews:
            gaps.append(
                TimelineGap(
                    kind="fraud_review_decision",
                    detail=(
                        f"case is under fraud hold (state={case.fraud_state}) and no named "
                        "fraud-review decision is on record — the block waits on that act"
                    ),
                )
            )

    return CaseTimeline(case_id=case.case_id, as_of=as_of, entries=entries, gaps=gaps)
