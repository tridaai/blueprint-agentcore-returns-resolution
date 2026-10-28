"""Read-only data assembly for the operations console (R13).

The console is a view, not a second workflow. This module gathers
artefacts the service already owns and returns the exact figures of
record in a browser-ready shape. It introduces no decisions, no
money math beyond identifying which stored receipts settle which
stored adjustment, and no customer-message surface.

One deliberate narrowing: the browser receives an assessment's id,
total, band and provenance — never the detailed assessment payload.
The detailed payload remains available through the case APIs for
the systems and auditors that already use them; the console does
not need it to operate the queues.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .fraud import STATE_FRAUD_CONFIRMED, STATE_FRAUD_REVIEW
from .models import FraudScore, RefundAdjustment, RefundReceipt, ReturnCase
from .service import CaseService

STAGES = [
    "intake",
    "decided",
    "computed",
    "dispositioned",
    "approved",
    "executed",
    "labelled",
    "graded",
    "closed",
]


def _dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _assessment(score: FraudScore | None) -> dict | None:
    """The console-safe assessment view: identity, total and band only."""
    if score is None:
        return None
    return {
        "id": score.score_id,
        "total": str(score.total_score),
        "band": score.band,
        "pack_version": score.pack_version,
        "as_of": score.as_of.isoformat(),
        "supersedes": score.supersedes,
        "history_source": score.history_source,
    }


def _review_state(case: ReturnCase) -> str:
    return {
        "clear": "clear",
        "monitor": "monitor",
        "fraud_review": "awaiting_review",
        "fraud_confirmed": "confirmed",
        "fraud_cleared": "cleared",
    }.get(case.fraud_state, case.fraud_state)


def _receipt_rows(
    receipts: list[RefundReceipt], top_up_receipt_ids: set[str]
) -> list[dict]:
    rows = []
    for receipt in receipts:
        row = _dump(receipt)
        row["kind"] = "top_up" if receipt.receipt_id in top_up_receipt_ids else "refund"
        rows.append(row)
    return sorted(rows, key=lambda r: (r["executed_on"], r["receipt_id"]))


def _derive_stage(
    case: ReturnCase,
    *,
    has_computation: bool,
    has_disposition: bool,
    approval_satisfied: bool,
    has_refund_receipt: bool,
    has_label: bool,
    has_grading: bool,
) -> str:
    """The furthest recorded lifecycle stage, with terminal cases closed.

    This is a presentation grouping over artefacts of record. It
    does not alter the case's stored status or create a close act.
    """
    if case.decision is not None and not case.decision.eligible:
        return "closed"
    if case.fraud_state == STATE_FRAUD_CONFIRMED:
        return "closed"
    if has_grading:
        return "graded"
    if has_label:
        return "labelled"
    if has_refund_receipt:
        return "executed"
    if approval_satisfied:
        return "approved"
    if has_disposition:
        return "dispositioned"
    if has_computation:
        return "computed"
    if case.decision is not None:
        return "decided"
    return "intake"


def _case_summary(
    service: CaseService,
    case: ReturnCase,
    *,
    receipts: list[RefundReceipt],
    adjustments: list[RefundAdjustment],
    top_up_receipt_ids: set[str],
) -> dict:
    case_receipts = [r for r in receipts if r.case_id == case.case_id]
    receipt_rows = _receipt_rows(case_receipts, top_up_receipt_ids)
    refund_receipts = [r for r in receipt_rows if r["kind"] == "refund"]
    case_adjustments = [a for a in adjustments if a.case_id == case.case_id]
    labels = service.list_labels(case.case_id)
    grading_reports = service.list_grading_reports(case.case_id)
    computation = service.get_computation(case.case_id)
    disposition = service.get_disposition(case.case_id)
    approval = service.approval_status(case.case_id)
    messages = service.list_messages(case.case_id)

    return {
        "case_id": case.case_id,
        "customer_id": case.customer_id,
        "order_id": case.order_id,
        "sku": case.request.sku,
        "qty": case.request.qty,
        "requested_on": case.request.requested_on.isoformat(),
        "stage": _derive_stage(
            case,
            has_computation=computation is not None,
            has_disposition=disposition is not None,
            approval_satisfied=bool(approval.get("satisfied") and approval.get("approvers")),
            has_refund_receipt=bool(refund_receipts),
            has_label=bool(labels),
            has_grading=bool(grading_reports),
        ),
        "eligible": case.decision.eligible if case.decision else None,
        "computed_refund": (
            str(computation.refund_total) if computation and computation.refund_total is not None else None
        ),
        "computation_final": computation.is_final if computation else False,
        "disposition": disposition.disposition if disposition else None,
        "disposition_status": disposition.status if disposition else None,
        "assessment": _assessment(case.fraud_score),
        "review_state": _review_state(case),
        "review_hold": case.fraud_state == STATE_FRAUD_REVIEW,
        "approval": approval,
        "executed_refund_total": _sum_rows(refund_receipts),
        "receipts": receipt_rows,
        "adjustments": [_dump(a) for a in case_adjustments],
        "label_count": len(labels),
        "grading_count": len(grading_reports),
        "message_statuses": [m.status for m in messages],
    }


def _sum_rows(rows: list[dict]) -> str:
    from decimal import Decimal

    total = sum((Decimal(str(r["amount"])) for r in rows), Decimal("0.00"))
    return format(total, ".2f")


def build_console_data(service: CaseService) -> dict:
    """The board, queues, money and gateway views in one read."""
    cases = service.list_cases()
    receipts = service.list_receipts()
    adjustments = service.list_all_adjustments()
    top_up_receipt_ids = {
        a.top_up_receipt_id for a in adjustments if a.top_up_receipt_id is not None
    }
    summaries = [
        _case_summary(
            service,
            case,
            receipts=receipts,
            adjustments=adjustments,
            top_up_receipt_ids=top_up_receipt_ids,
        )
        for case in cases
    ]
    summaries.sort(key=lambda c: (c["requested_on"], c["case_id"]))

    receipt_rows = _receipt_rows(receipts, top_up_receipt_ids)
    review_queue = [c for c in summaries if c["review_hold"]]
    approval_queue = []
    for summary in summaries:
        approval = summary["approval"]
        if (
            approval.get("tier") in {"single", "dual"}
            and not approval.get("satisfied")
            and summary["computation_final"]
            and not any(r["kind"] == "refund" for r in summary["receipts"])
            and not summary["review_hold"]
        ):
            approval_queue.append(
                {
                    "case_id": summary["case_id"],
                    "customer_id": summary["customer_id"],
                    "amount": approval.get("refund_total"),
                    "tier": approval.get("tier"),
                    "approvals_required": approval.get("approvals_required"),
                    "approvers": approval.get("approvers", []),
                    "missing": approval.get("missing"),
                    "computation_id": approval.get("computation_id"),
                }
            )
    approval_queue.sort(key=lambda r: (r["amount"] or "", r["case_id"]), reverse=True)

    return {
        "stages": STAGES,
        "cases": summaries,
        "queues": {
            "review": review_queue,
            "approvals": approval_queue,
            "recovery": [
                _dump(a) for a in adjustments if a.kind == "recovery_due" and a.status == "open"
            ],
        },
        "money": {
            "receipts": receipt_rows,
            "adjustments": [_dump(a) for a in adjustments],
        },
        "gateway": {
            "invocations": [_dump(c) for c in service.list_all_tool_calls()],
            "policy_pack": _dump(service.get_gateway_policy_pack()),
        },
        "review_outcomes": service.get_scoring_pack().review_outcomes,
    }


def _max_timeline_date(service: CaseService, case: ReturnCase) -> date:
    dates: list[date] = [case.request.requested_on]
    if case.decision:
        dates.append(case.decision.requested_on)
    for approval in service.list_approvals(case.case_id):
        dates.append(approval.as_of)
    for execution in service.list_executions(case.case_id):
        dates.extend(t.at for t in execution.transitions)
    for receipt in service.list_receipts():
        if receipt.case_id == case.case_id:
            dates.append(receipt.executed_on)
    for label in service.list_labels(case.case_id):
        dates.append(label.issued_on)
    for warehouse_receipt in service.list_warehouse_receipts(case.case_id):
        dates.append(warehouse_receipt.received_on)
    for report in service.list_grading_reports(case.case_id):
        dates.append(report.graded_on)
    for adjustment in service.list_adjustments(case.case_id):
        dates.append(adjustment.created_on)
    for message in service.list_messages(case.case_id):
        dates.append(message.created_on)
        dates.extend(t.at for t in message.transitions)
    if case.fraud_score:
        dates.append(case.fraud_score.as_of)
    return max(dates)


def build_case_detail(service: CaseService, case_id: str) -> dict | None:
    """One case's artefact chain in workflow order, console-safe."""
    case = service.get_case(case_id)
    if case is None:
        return None

    adjustments = service.list_adjustments(case_id)
    all_adjustments = service.list_all_adjustments()
    top_up_receipt_ids = {
        a.top_up_receipt_id for a in all_adjustments if a.top_up_receipt_id is not None
    }
    receipts = [r for r in service.list_receipts() if r.case_id == case_id]
    order = service.get_order(case.order_id)
    order_view = None
    if order is not None:
        order_view = _dump(order)
        order_view["items_total"] = str(order.items_total)
        order_view["order_total"] = str(order.order_total)

    memory_records = [
        _dump(r)
        for r in service.retrieve_memory(
            case.customer_id, purpose="console_case_detail", actor="console"
        )
        if r.case_id == case_id
    ]
    timeline = service.get_timeline(case_id, _max_timeline_date(service, case))

    return {
        "case": {
            "case_id": case.case_id,
            "customer_id": case.customer_id,
            "order_id": case.order_id,
            "status": case.status,
            "summary": case.summary,
            "review_state": _review_state(case),
            "request": _dump(case.request),
        },
        "order": order_view,
        "decision": _dump(case.decision),
        "computations": [_dump(c) for c in service.list_computations_for_case(case_id)],
        "dispositions": [_dump(d) for d in service.list_dispositions_for_case(case_id)],
        "assessments": [
            _assessment(s) for s in service.list_fraud_scores_for_case(case_id)
        ],
        "review_decisions": [_dump(r) for r in service.list_fraud_reviews(case_id)],
        "approvals": [_dump(a) for a in service.list_approvals(case_id)],
        "approval_status": service.approval_status(case_id),
        "executions": [_dump(e) for e in service.list_executions(case_id)],
        "receipts": _receipt_rows(receipts, top_up_receipt_ids),
        "labels": [_dump(label) for label in service.list_labels(case_id)],
        "label_voids": [_dump(v) for v in service.list_voids(case_id)],
        "pickups": [_dump(p) for p in service.list_pickups(case_id)],
        "warehouse_receipts": [
            _dump(r) for r in service.list_warehouse_receipts(case_id)
        ],
        "grading_reports": [_dump(r) for r in service.list_grading_reports(case_id)],
        "adjustments": [_dump(a) for a in adjustments],
        "messages": [_dump(m) for m in service.list_messages(case_id)],
        "memory_records": memory_records,
        "cost_entries": [_dump(e) for e in service.list_cost_entries(case_id)],
        "timeline": _dump(timeline),
    }
