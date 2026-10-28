"""FastAPI management surface: intake, cases, decisions, orders.

This is the local/operations API. The AgentCore Runtime contract lives
in runtime_app.py; both drive the same CaseService, so a case decided
through one surface reads identically through the other.
"""

from __future__ import annotations

from datetime import date

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, field_validator

from .gateway import ToolError
from .intake import ReferenceFeed
from .models import ReturnRequest
from .service import (
    ApprovalError,
    CarrierError,
    CaseService,
    DuplicateRefundError,
    FigureVerificationError,
    FraudHoldError,
    FraudReviewError,
    GradingError,
    PaymentsError,
)


class FraudReviewRequest(BaseModel):
    reviewer: str
    outcome: str
    reason_code: str

    @field_validator("reviewer")
    @classmethod
    def _named(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("a fraud-review decision requires a named reviewer")
        return v.strip()


class ApprovalRequest(BaseModel):
    approver: str
    role: str
    as_of: date
    computation_id: str | None = None

    @field_validator("approver")
    @classmethod
    def _named(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("a refund approval requires a named approver")
        return v.strip()


class ExecutionRequest(BaseModel):
    idempotency_key: str
    as_of: date

    @field_validator("idempotency_key")
    @classmethod
    def _keyed(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("an idempotency key is mandatory on every refund execution")
        return v.strip()


class LabelRequest(BaseModel):
    issued_on: date


class LabelVoidRequest(BaseModel):
    voided_by: str
    reason: str
    voided_on: date

    @field_validator("voided_by")
    @classmethod
    def _named(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("voiding a label requires a named actor")
        return v.strip()


class PickupRequest(BaseModel):
    scheduled_on: date
    label_id: str | None = None


class WarehouseReceiptRequest(BaseModel):
    received_by: str
    received_on: date

    @field_validator("received_by")
    @classmethod
    def _named(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("a warehouse receipt requires a named receiver")
        return v.strip()


class GradingRequest(BaseModel):
    grader: str
    graded_condition: str
    graded_on: date
    functional_test_result: str | None = None
    data_wipe_status: str | None = None
    damage_notes: str = ""
    received_weight_grams: int | None = None
    wear_evidence: bool = False

    @field_validator("grader")
    @classmethod
    def _named(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("a grading report requires a named grader")
        return v.strip()


def _order_view(order) -> dict:
    view = order.model_dump(mode="json")
    # Totals are computed properties (never stored), so the API adds
    # them explicitly — a reader should never have to re-derive money.
    view["items_total"] = str(order.items_total)
    view["order_total"] = str(order.order_total)
    return view


def create_app(service: CaseService) -> FastAPI:
    app = FastAPI(title="Returns Resolution & Disposition Agent")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/readiness")
    def readiness() -> dict:
        return {"status": "ready", "orders": len(service.list_orders())}

    @app.get("/orders")
    def orders() -> list[dict]:
        return [_order_view(o) for o in service.list_orders()]

    @app.get("/orders/{order_id}")
    def order(order_id: str) -> dict:
        found = service.get_order(order_id)
        if found is None:
            raise HTTPException(status_code=404, detail="order not found")
        return _order_view(found)

    @app.post("/intake/feed")
    def intake_feed() -> dict:
        results = []
        for request in ReferenceFeed().fetch_requests():
            try:
                case = service.ingest(request)
                results.append({"request_id": request.request_id, "case_id": case.case_id, "status": case.status})
            except FigureVerificationError as exc:
                results.append({"request_id": request.request_id, "status": "summary_failed", "violations": exc.violations})
        return {"ingested": results}

    @app.post("/returns", status_code=201)
    def submit_return(request: ReturnRequest) -> dict:
        try:
            case = service.ingest(request)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FigureVerificationError as exc:
            raise HTTPException(status_code=422, detail={"violations": exc.violations}) from exc
        return case.model_dump(mode="json")

    @app.get("/cases")
    def cases() -> list[dict]:
        return [c.model_dump(mode="json") for c in service.list_cases()]

    @app.get("/cases/{case_id}")
    def case(case_id: str) -> dict:
        found = service.get_case(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="case not found")
        return found.model_dump(mode="json")

    @app.get("/cases/{case_id}/decision")
    def decision(case_id: str) -> dict:
        found = service.get_case(case_id)
        if found is None or found.decision is None:
            raise HTTPException(status_code=404, detail="decision not found")
        return found.decision.model_dump(mode="json")

    @app.get("/cases/{case_id}/computation")
    def computation(case_id: str) -> dict:
        found = service.get_computation(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="computation not found")
        return found.model_dump(mode="json")

    @app.get("/cases/{case_id}/computation/verify")
    def computation_verify(case_id: str) -> dict:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        problems = service.verify_stored_computation(case_id)
        return {"case_id": case_id, "ok": not problems, "problems": problems}

    @app.get("/computations")
    def computations() -> list[dict]:
        return [c.model_dump(mode="json") for c in service.list_computations()]

    @app.get("/cases/{case_id}/disposition")
    def disposition(case_id: str) -> dict:
        found = service.get_disposition(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="disposition not found")
        return found.model_dump(mode="json")

    @app.get("/cases/{case_id}/disposition/verify")
    def disposition_verify(case_id: str) -> dict:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        problems = service.verify_stored_disposition(case_id)
        return {"case_id": case_id, "ok": not problems, "problems": problems}

    @app.get("/dispositions")
    def dispositions() -> list[dict]:
        return [d.model_dump(mode="json") for d in service.list_dispositions()]

    @app.get("/disposition/matrix")
    def matrix() -> dict:
        return service.get_matrix().model_dump(mode="json")

    @app.get("/disposition/matrix/cells/{cell_id}")
    def matrix_cell(cell_id: str) -> dict:
        cell = service.get_matrix().cell_by_id(cell_id)
        if cell is None:
            raise HTTPException(status_code=404, detail="matrix cell not found")
        return cell.model_dump(mode="json")

    @app.get("/disposition/channels")
    def channels() -> dict:
        return service.get_channels().model_dump(mode="json")

    @app.get("/cases/{case_id}/fraud-score")
    def fraud_score(case_id: str) -> dict:
        found = service.get_fraud_score(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="fraud score not found")
        return found.model_dump(mode="json")

    @app.get("/cases/{case_id}/fraud-score/signals")
    def fraud_signals(case_id: str) -> dict:
        found = service.get_fraud_score(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="fraud score not found")
        return {
            "case_id": case_id,
            "score_id": found.score_id,
            "pack_version": found.pack_version,
            "total_score": str(found.total_score),
            "band": found.band,
            "signals": [s.model_dump(mode="json") for s in found.signals],
        }

    @app.get("/cases/{case_id}/fraud-score/verify")
    def fraud_score_verify(case_id: str) -> dict:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        problems = service.verify_stored_fraud_score(case_id)
        return {"case_id": case_id, "ok": not problems, "problems": problems}

    @app.post("/cases/{case_id}/fraud-score/rescore")
    def fraud_rescore(case_id: str) -> dict:
        found = service.rescore_case(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="case not found")
        return found.model_dump(mode="json")

    @app.get("/fraud-scores")
    def fraud_scores() -> list[dict]:
        return [s.model_dump(mode="json") for s in service.list_fraud_scores()]

    @app.get("/fraud/scoring-pack")
    def scoring_pack() -> dict:
        return service.get_scoring_pack().model_dump(mode="json")

    @app.post("/cases/{case_id}/fraud-review", status_code=201)
    def fraud_review(case_id: str, body: FraudReviewRequest) -> dict:
        try:
            decision = service.record_fraud_review(
                case_id, reviewer=body.reviewer, outcome=body.outcome, reason_code=body.reason_code
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FraudReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return decision.model_dump(mode="json")

    @app.get("/cases/{case_id}/fraud-review")
    def fraud_review_read(case_id: str) -> dict:
        found = service.get_fraud_review(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="fraud review not found")
        return found.model_dump(mode="json")

    @app.post("/cases/{case_id}/approvals", status_code=201)
    def record_approval(case_id: str, body: ApprovalRequest) -> dict:
        try:
            approval = service.record_approval(
                case_id,
                approver=body.approver,
                role=body.role,
                as_of=body.as_of,
                computation_id=body.computation_id,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ApprovalError, FraudHoldError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return approval.model_dump(mode="json")

    @app.get("/cases/{case_id}/approvals")
    def approvals(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [a.model_dump(mode="json") for a in service.list_approvals(case_id)]

    @app.get("/cases/{case_id}/approval-status")
    def approval_status(case_id: str) -> dict:
        try:
            return service.approval_status(case_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/cases/{case_id}/refund-executions", status_code=201)
    def execute_refund(case_id: str, body: ExecutionRequest) -> dict:
        try:
            receipt = service.execute_refund(
                case_id, idempotency_key=body.idempotency_key, as_of=body.as_of
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ApprovalError, DuplicateRefundError, FraudHoldError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PaymentsError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return receipt.model_dump(mode="json")

    @app.get("/cases/{case_id}/receipt")
    def receipt(case_id: str) -> dict:
        found = service.get_receipt(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="receipt not found")
        return found.model_dump(mode="json")

    @app.get("/receipts")
    def receipts() -> list[dict]:
        return [r.model_dump(mode="json") for r in service.list_receipts()]

    @app.get("/cases/{case_id}/executions")
    def executions(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [e.model_dump(mode="json") for e in service.list_executions(case_id)]

    # -- carrier tools (R6): every act goes through the gateway ----------
    @app.get("/tools")
    def tools() -> list[dict]:
        return [d.model_dump(mode="json") for d in service.tool_definitions()]

    @app.get("/cases/{case_id}/tool-calls")
    def tool_calls(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [c.model_dump(mode="json") for c in service.list_tool_calls(case_id)]

    @app.post("/cases/{case_id}/labels", status_code=201)
    def create_label(case_id: str, body: LabelRequest) -> dict:
        try:
            label = service.invoke_tool(
                "create_return_label",
                {"case_id": case_id, "issued_on": body.issued_on},
                case_id=case_id,
                called_on=body.issued_on,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (CarrierError, FraudHoldError, ToolError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FigureVerificationError as exc:
            raise HTTPException(status_code=422, detail={"violations": exc.violations}) from exc
        return label.model_dump(mode="json")

    @app.get("/cases/{case_id}/labels")
    def labels(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [label.model_dump(mode="json") for label in service.list_labels(case_id)]

    @app.get("/cases/{case_id}/label")
    def active_label(case_id: str) -> dict:
        found = service.get_active_label(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="no active label for this case")
        return found.model_dump(mode="json")

    @app.get("/labels")
    def all_labels() -> list[dict]:
        return [label.model_dump(mode="json") for label in service.list_all_labels()]

    @app.post("/cases/{case_id}/labels/{label_id}/void", status_code=201)
    def void_label(case_id: str, label_id: str, body: LabelVoidRequest) -> dict:
        try:
            void = service.invoke_tool(
                "void_return_label",
                {
                    "case_id": case_id,
                    "label_id": label_id,
                    "voided_by": body.voided_by,
                    "reason": body.reason,
                    "voided_on": body.voided_on,
                },
                case_id=case_id,
                called_on=body.voided_on,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (CarrierError, ToolError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return void.model_dump(mode="json")

    @app.get("/cases/{case_id}/labels/{label_id}/events")
    def carrier_events(case_id: str, label_id: str, as_of: date) -> list[dict]:
        try:
            events = service.invoke_tool(
                "get_carrier_events",
                {"case_id": case_id, "label_id": label_id, "as_of": as_of},
                case_id=case_id,
                called_on=as_of,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ToolError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return [e.model_dump(mode="json") for e in events]

    @app.post("/cases/{case_id}/pickups", status_code=201)
    def book_pickup(case_id: str, body: PickupRequest) -> dict:
        try:
            booking = service.invoke_tool(
                "book_pickup",
                {"case_id": case_id, "scheduled_on": body.scheduled_on, "label_id": body.label_id},
                case_id=case_id,
                called_on=body.scheduled_on,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (CarrierError, ToolError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return booking.model_dump(mode="json")

    @app.get("/cases/{case_id}/pickups")
    def pickups(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [p.model_dump(mode="json") for p in service.list_pickups(case_id)]

    @app.get("/cases/{case_id}/timeline")
    def timeline(case_id: str, as_of: date) -> dict:
        try:
            view = service.get_timeline(case_id, as_of)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return view.model_dump(mode="json")

    # -- warehouse receipt + grading (R7) --------------------------------
    @app.post("/cases/{case_id}/warehouse-receipts", status_code=201)
    def record_warehouse_receipt(case_id: str, body: WarehouseReceiptRequest) -> dict:
        try:
            receipt = service.record_warehouse_receipt(
                case_id, received_by=body.received_by, received_on=body.received_on
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GradingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return receipt.model_dump(mode="json")

    @app.get("/cases/{case_id}/warehouse-receipts")
    def warehouse_receipts(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [r.model_dump(mode="json") for r in service.list_warehouse_receipts(case_id)]

    @app.post("/cases/{case_id}/grading-reports", status_code=201)
    def record_grading(case_id: str, body: GradingRequest) -> dict:
        try:
            report = service.record_grading(
                case_id,
                grader=body.grader,
                graded_condition=body.graded_condition,
                graded_on=body.graded_on,
                functional_test_result=body.functional_test_result,
                data_wipe_status=body.data_wipe_status,
                damage_notes=body.damage_notes,
                received_weight_grams=body.received_weight_grams,
                wear_evidence=body.wear_evidence,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GradingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FigureVerificationError as exc:
            raise HTTPException(status_code=422, detail={"violations": exc.violations}) from exc
        return report.model_dump(mode="json")

    @app.get("/cases/{case_id}/grading-reports")
    def grading_reports(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [r.model_dump(mode="json") for r in service.list_grading_reports(case_id)]

    @app.get("/cases/{case_id}/adjustments")
    def adjustments(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [a.model_dump(mode="json") for a in service.list_adjustments(case_id)]

    @app.post("/cases/{case_id}/top-up-executions", status_code=201)
    def execute_top_up(case_id: str, body: ExecutionRequest) -> dict:
        try:
            receipt = service.execute_top_up(
                case_id, idempotency_key=body.idempotency_key, as_of=body.as_of
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ApprovalError, DuplicateRefundError, FraudHoldError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PaymentsError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return receipt.model_dump(mode="json")

    @app.post("/cases/{case_id}/evidence")
    async def evidence(case_id: str, request: Request, kind: str = "photo", filename: str = "evidence.bin") -> dict:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        content = await request.body()
        return service.attach_evidence(case_id, kind, filename, content)

    return app
