"""FastAPI management surface: intake, cases, decisions, orders.

This is the local/operations API. The AgentCore Runtime contract lives
in runtime_app.py; both drive the same CaseService, so a case decided
through one surface reads identically through the other.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator

from .access import (
    GATEWAY_ACTOR_FOR_ROLE,
    HIDING_REASONS,
    SURFACE_CASE,
    SURFACE_CONSOLE,
    SURFACE_COST_LEDGER,
    SURFACE_DENIAL_LOG,
    SURFACE_FRAUD,
    SURFACE_GATEWAY,
    SURFACE_MEMORY,
    SURFACE_MONEY,
    SURFACE_ORDER,
    SURFACE_QUARANTINE,
    SURFACE_REPORTS,
    AccessDeniedError,
    Principal,
)
from .console import build_case_detail, build_console_data
from .gateway import PolicyDeniedError, ToolError
from .intake import ReferenceFeed
from .model_gateway import ModelBudgetError, ModelGatewayError
from .models import ReturnRequest
from .quarantine import QuarantineError
from .reporting import ReportError
from .service import (
    ApprovalError,
    CarrierError,
    CaseService,
    DuplicateRefundError,
    FigureVerificationError,
    FraudHoldError,
    FraudReviewError,
    GradingError,
    MessagingError,
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


class ToolInvokeRequest(BaseModel):
    """The generic gateway invocation (R11): a tool, its exact
    arguments, the case it acts on, and the invoking actor the
    gateway policy pack evaluates."""

    case_id: str
    arguments: dict = {}
    actor: str = "staff"
    called_on: date


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


class MessageDraftRequest(BaseModel):
    message_type: str
    as_of: date
    model_note: str | None = None
    # Customer text offered for the message (R15): it lands in
    # quarantine first and only its released view is quoted.
    customer_note: str | None = None


class QuarantineSubmitRequest(BaseModel):
    source_surface: str
    field_name: str
    text: str
    received_on: date


class QuarantineReleaseRequest(BaseModel):
    released_by: str
    reason: str
    released_on: date

    @field_validator("released_by")
    @classmethod
    def _named(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("releasing a held text requires a named staff member")
        return v.strip()

    @field_validator("reason")
    @classmethod
    def _stated(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("releasing a held text requires a stated reason")
        return v.strip()


class MessageActRequest(BaseModel):
    as_of: date


class GradingRequest(BaseModel):
    grader: str
    graded_condition: str
    graded_on: date
    functional_test_result: str | None = None
    data_wipe_status: str | None = None
    damage_notes: str = ""
    received_weight_grams: int | None = None
    wear_evidence: bool = False
    # Free-text photo-evidence metadata (R15): field name → text.
    # Each field routes through quarantine before grading reads it.
    photo_metadata: dict[str, str] | None = None

    @field_validator("grader")
    @classmethod
    def _named(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("a grading report requires a named grader")
        return v.strip()


class ReportComputeRequest(BaseModel):
    kind: str
    window_start: date | None = None
    window_end: date | None = None
    as_of: date


def _order_view(order) -> dict:
    view = order.model_dump(mode="json")
    # Totals are computed properties (never stored), so the API adds
    # them explicitly — a reader should never have to re-derive money.
    view["items_total"] = str(order.items_total)
    view["order_total"] = str(order.order_total)
    return view


def _principal(
    request: Request, service: CaseService, *, surface: str, target: str = ""
) -> Principal | None:
    """The principal a call carries, if any (R16) — from the
    X-Principal-Id / X-Principal-Role headers. A call carrying no
    principal headers takes the pre-R16 trusted-local path. A
    call carrying a principal is always authenticated: a partial
    pair, a blank id or an unknown role is a 401 with the named
    reason — never a silent downgrade."""
    pid = request.headers.get("x-principal-id")
    role = request.headers.get("x-principal-role")
    if pid is None and role is None:
        return None
    raw: dict = {}
    if pid is not None:
        raw["id"] = pid
    if role is not None:
        raw["role"] = role
    try:
        return service.authenticate_principal(raw, surface=surface, target=target)
    except AccessDeniedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _guard(exc: AccessDeniedError, *, not_found: str | None = None) -> None:
    """Translate a guard refusal: the hiding reasons render as
    the resource's own not-found response (existence stays
    hidden — the payload is the endpoint's ordinary 404);
    every other refusal is a named 403."""
    if not_found is not None and exc.reason in HIDING_REASONS:
        raise HTTPException(status_code=404, detail=not_found) from exc
    raise HTTPException(status_code=403, detail=str(exc)) from exc


def _gateway_actor(
    request: Request, service: CaseService, case_id: str
) -> str | None:
    """For the gateway-backed endpoints (R16): the gateway actor
    a carried principal must invoke as, or None on the legacy
    path. The gateway surface grant is checked here; the R11
    policy pack then governs what that actor may do."""
    principal = _principal(request, service, surface=SURFACE_GATEWAY, target=case_id)
    if principal is None:
        return None
    try:
        service.require_surface(principal, SURFACE_GATEWAY, target=case_id)
    except AccessDeniedError as exc:
        _guard(exc)
    return GATEWAY_ACTOR_FOR_ROLE[principal.role]


def _manage_case(request: Request, service: CaseService, case_id: str) -> None:
    """Gate a working act (R16): with a principal, the caller
    must hold the pack's manage_case act for the case."""
    principal = _principal(request, service, surface=SURFACE_CASE, target=case_id)
    if principal is None:
        return
    try:
        service.require_manage_case(principal, case_id)
    except AccessDeniedError as exc:
        _guard(exc)


def create_app(service: CaseService) -> FastAPI:
    app = FastAPI(title="Returns Resolution & Disposition Agent")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/readiness")
    def readiness() -> dict:
        return {"status": "ready", "orders": len(service.list_orders())}

    @app.get("/console", include_in_schema=False)
    def console() -> FileResponse:
        """The single-file operations console (R13)."""
        return FileResponse(
            Path(__file__).parent / "static" / "console.html", media_type="text/html"
        )

    @app.get("/console/data")
    def console_data(request: Request) -> dict:
        """The console's read-only board, queue, money and gateway
        data. With a principal the payload is access-checked and
        scoped (R16): a customer principal receives only their own
        cases in the customer-safe shape."""
        principal = _principal(request, service, surface=SURFACE_CONSOLE, target="console_data")
        try:
            return build_console_data(service, principal)
        except AccessDeniedError as exc:
            _guard(exc)

    @app.get("/console/cases/{case_id}")
    def console_case(case_id: str, request: Request) -> dict:
        """One case's console-safe artefact chain, in workflow order."""
        principal = _principal(request, service, surface=SURFACE_CONSOLE, target=case_id)
        found = build_case_detail(service, case_id, principal)
        if found is None:
            raise HTTPException(status_code=404, detail="case not found")
        return found

    @app.get("/access/pack")
    def access_pack() -> dict:
        """The access pack as data (R16): the roles, their readable
        surfaces and their acts — the same pack the guard enforces."""
        return service.get_access_pack().model_dump(mode="json")

    @app.get("/access/denials")
    def access_denials(request: Request) -> list[dict]:
        """The denial log (R16): every recorded refusal. Reading it
        is itself a guarded surface — auditor and supervisor tier."""
        principal = _principal(request, service, surface=SURFACE_DENIAL_LOG, target="access_denials")
        try:
            entries = (
                service.list_access_denials_as(principal)
                if principal is not None
                else service.list_access_denials()
            )
        except AccessDeniedError as exc:
            _guard(exc)
        return [e.model_dump(mode="json") for e in entries]

    @app.get("/reports/pack")
    def report_pack() -> dict:
        """The report pack as data (R17): each report's population
        rule, window rule and measures — the definitions a stored
        report's figures are versioned by."""
        return service.get_report_pack().model_dump(mode="json")

    @app.post("/reports/compute", status_code=201)
    def compute_report(payload: ReportComputeRequest, request: Request) -> dict:
        """Compute a report from the artefacts of record (R17).
        With a principal, the caller must hold the pack's
        compute_report act — supervisor tier; computing is an
        act, and the auditor's read-only grant does not cover it."""
        principal = _principal(request, service, surface=SURFACE_REPORTS, target=payload.kind)
        try:
            report = (
                service.compute_report_as(
                    principal,
                    payload.kind,
                    window_start=payload.window_start,
                    window_end=payload.window_end,
                    as_of=payload.as_of,
                )
                if principal is not None
                else service.compute_report(
                    payload.kind,
                    window_start=payload.window_start,
                    window_end=payload.window_end,
                    as_of=payload.as_of,
                )
            )
        except AccessDeniedError as exc:
            _guard(exc)
        except ReportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return report.model_dump(mode="json")

    @app.get("/reports")
    def reports(request: Request, kind: str | None = None) -> list[dict]:
        """The stored reports (R17). A customer principal is
        refused with the hiding shape — reports are not a
        customer surface at all."""
        principal = _principal(request, service, surface=SURFACE_REPORTS, target="reports")
        try:
            found = (
                service.list_reports_as(principal, kind=kind)
                if principal is not None
                else service.list_reports(kind=kind)
            )
        except AccessDeniedError as exc:
            _guard(exc, not_found="report not found")
        return [r.model_dump(mode="json") for r in found]

    @app.get("/reports/{report_id}")
    def report(report_id: str, request: Request) -> dict:
        """One stored report. A customer probing a report id —
        real or invented — gets this endpoint's ordinary
        not-found shape; existence is never confirmed."""
        principal = _principal(request, service, surface=SURFACE_REPORTS, target=report_id)
        if principal is None:
            found = service.get_report(report_id)
        else:
            try:
                found = service.get_report_as(principal, report_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="report not found")
        if found is None:
            raise HTTPException(status_code=404, detail="report not found")
        return found.model_dump(mode="json")

    @app.get("/reports/{report_id}/verify")
    def verify_report_endpoint(report_id: str, request: Request) -> dict:
        """Re-perform one report from the stored record (R17):
        the figures are recomputed independently and compared;
        `verified` is true only when the re-performance matches
        exactly."""
        principal = _principal(request, service, surface=SURFACE_REPORTS, target=report_id)
        if principal is None:
            found = service.get_report(report_id)
        else:
            try:
                found = service.get_report_as(principal, report_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="report not found")
        if found is None:
            raise HTTPException(status_code=404, detail="report not found")
        problems = service.verify_management_report(found)
        return {"report_id": report_id, "verified": not problems, "problems": problems}

    @app.get("/orders")
    def orders(request: Request) -> list[dict]:
        principal = _principal(request, service, surface=SURFACE_ORDER, target="orders")
        if principal is None:
            return [_order_view(o) for o in service.list_orders()]
        try:
            return [_order_view(o) for o in service.list_orders_as(principal)]
        except AccessDeniedError as exc:
            _guard(exc)

    @app.get("/orders/{order_id}")
    def order(order_id: str, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_ORDER, target=order_id)
        if principal is None:
            found = service.get_order(order_id)
        else:
            try:
                found = service.get_order_as(principal, order_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="order not found")
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
            except ModelGatewayError as exc:
                results.append({"request_id": request.request_id, "status": "model_gateway_failed", "error": str(exc)})
        return {"ingested": results}

    @app.post("/returns", status_code=201)
    def submit_return(body: ReturnRequest, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_CASE, target=body.order_id)
        if principal is not None:
            try:
                service.require_intake(principal, body.order_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found=f"unknown order: {body.order_id}")
        try:
            case = service.ingest(body)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FigureVerificationError as exc:
            raise HTTPException(status_code=422, detail={"violations": exc.violations}) from exc
        except ModelBudgetError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except ModelGatewayError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return case.model_dump(mode="json")

    @app.get("/cases")
    def cases(request: Request) -> list[dict]:
        principal = _principal(request, service, surface=SURFACE_CASE, target="cases")
        if principal is None:
            return [c.model_dump(mode="json") for c in service.list_cases()]
        try:
            if principal.role == "customer":
                return [
                    service.get_case_view_as(principal, c.case_id)
                    for c in service.list_cases_as(principal)
                ]
            return [c.model_dump(mode="json") for c in service.list_cases_as(principal)]
        except AccessDeniedError as exc:
            _guard(exc)

    @app.get("/cases/{case_id}")
    def case(case_id: str, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_CASE, target=case_id)
        if principal is None:
            found = service.get_case(case_id)
            if found is None:
                raise HTTPException(status_code=404, detail="case not found")
            return found.model_dump(mode="json")
        try:
            view = service.get_case_view_as(principal, case_id)
        except AccessDeniedError as exc:
            _guard(exc, not_found="case not found")
        if view is None:
            raise HTTPException(status_code=404, detail="case not found")
        return view

    @app.get("/cases/{case_id}/decision")
    def decision(case_id: str, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_CASE, target=case_id)
        if principal is None:
            found = service.get_case(case_id)
        else:
            try:
                found = service.get_case_as(principal, case_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="decision not found")
        if found is None or found.decision is None:
            raise HTTPException(status_code=404, detail="decision not found")
        return found.decision.model_dump(mode="json")

    @app.get("/cases/{case_id}/computation")
    def computation(case_id: str, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_MONEY, target=case_id)
        if principal is not None:
            try:
                service.get_case_as(principal, case_id)
                service.require_surface(principal, SURFACE_MONEY, target=case_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="computation not found")
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
    def fraud_score(case_id: str, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_FRAUD, target=case_id)
        if principal is None:
            found = service.get_fraud_score(case_id)
        else:
            try:
                found = service.get_fraud_score_as(principal, case_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="fraud score not found")
        if found is None:
            raise HTTPException(status_code=404, detail="fraud score not found")
        return found.model_dump(mode="json")

    @app.get("/cases/{case_id}/fraud-score/signals")
    def fraud_signals(case_id: str, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_FRAUD, target=case_id)
        if principal is None:
            found = service.get_fraud_score(case_id)
        else:
            try:
                found = service.get_fraud_score_as(principal, case_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="fraud score not found")
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
    def fraud_score_verify(case_id: str, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_FRAUD, target=case_id)
        if principal is None:
            if service.get_case(case_id) is None:
                raise HTTPException(status_code=404, detail="case not found")
        else:
            try:
                service.get_fraud_score_as(principal, case_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="case not found")
            if service.get_case(case_id) is None:
                raise HTTPException(status_code=404, detail="case not found")
        problems = service.verify_stored_fraud_score(case_id)
        return {"case_id": case_id, "ok": not problems, "problems": problems}

    @app.post("/cases/{case_id}/fraud-score/rescore")
    def fraud_rescore(case_id: str, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_FRAUD, target=case_id)
        if principal is not None:
            try:
                service.require_manage_case(principal, case_id)
            except AccessDeniedError as exc:
                _guard(exc)
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
    def fraud_review(case_id: str, body: FraudReviewRequest, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_FRAUD, target=case_id)
        try:
            if principal is None:
                decision = service.record_fraud_review(
                    case_id, reviewer=body.reviewer, outcome=body.outcome, reason_code=body.reason_code
                )
            else:
                if body.reviewer != principal.id:
                    service.deny_principal_mismatch(
                        principal, surface=SURFACE_FRAUD, target=case_id,
                        field="reviewer", claimed=body.reviewer,
                    )
                decision = service.record_fraud_review_as(
                    principal, case_id, outcome=body.outcome, reason_code=body.reason_code
                )
        except AccessDeniedError as exc:
            _guard(exc)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FraudReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return decision.model_dump(mode="json")

    @app.get("/cases/{case_id}/fraud-review")
    def fraud_review_read(case_id: str, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_FRAUD, target=case_id)
        if principal is not None:
            try:
                service.get_fraud_score_as(principal, case_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="fraud review not found")
        found = service.get_fraud_review(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="fraud review not found")
        return found.model_dump(mode="json")

    @app.post("/cases/{case_id}/approvals", status_code=201)
    def record_approval(case_id: str, body: ApprovalRequest, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_MONEY, target=case_id)
        try:
            if principal is None:
                approval = service.record_approval(
                    case_id,
                    approver=body.approver,
                    role=body.role,
                    as_of=body.as_of,
                    computation_id=body.computation_id,
                )
            else:
                if body.approver != principal.id:
                    service.deny_principal_mismatch(
                        principal, surface=SURFACE_MONEY, target=case_id,
                        field="approver", claimed=body.approver, on=body.as_of,
                    )
                approval = service.record_approval_as(
                    principal, case_id, as_of=body.as_of, computation_id=body.computation_id
                )
        except AccessDeniedError as exc:
            _guard(exc)
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
    def execute_refund(case_id: str, body: ExecutionRequest, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_MONEY, target=case_id)
        try:
            if principal is None:
                receipt = service.execute_refund(
                    case_id, idempotency_key=body.idempotency_key, as_of=body.as_of
                )
            else:
                receipt = service.execute_refund_as(
                    principal, case_id, idempotency_key=body.idempotency_key, as_of=body.as_of
                )
        except AccessDeniedError as exc:
            _guard(exc)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ApprovalError, DuplicateRefundError, FraudHoldError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PaymentsError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return receipt.model_dump(mode="json")

    @app.get("/cases/{case_id}/receipt")
    def receipt(case_id: str, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_MONEY, target=case_id)
        if principal is not None:
            try:
                service.get_case_as(principal, case_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="receipt not found")
        found = service.get_receipt(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="receipt not found")
        return found.model_dump(mode="json")

    @app.get("/receipts")
    def receipts(request: Request) -> list[dict]:
        principal = _principal(request, service, surface=SURFACE_MONEY, target="receipts")
        rows = service.list_receipts()
        if principal is None:
            return [r.model_dump(mode="json") for r in rows]
        try:
            service.require_surface(principal, SURFACE_MONEY, target="receipts")
            if principal.role == "customer":
                own = {c.case_id for c in service.list_cases_as(principal)}
                rows = [r for r in rows if r.case_id in own]
        except AccessDeniedError as exc:
            _guard(exc)
        return [r.model_dump(mode="json") for r in rows]

    @app.get("/cases/{case_id}/executions")
    def executions(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [e.model_dump(mode="json") for e in service.list_executions(case_id)]

    # -- carrier tools (R6): every act goes through the gateway ----------
    @app.get("/tools")
    def tools() -> list[dict]:
        return [d.model_dump(mode="json") for d in service.tool_definitions()]

    @app.get("/gateway/policy-pack")
    def gateway_policy_pack() -> dict:
        """The gateway policy pack as data (R11): the policies the
        gateway evaluates in front of every invocation, in
        evaluation order."""
        return service.get_gateway_policy_pack().model_dump(mode="json")

    # -- model gateway + cost ledger (R12) -------------------------------
    @app.get("/model/config")
    def model_config() -> dict:
        """The model gateway's configuration (R12): the active
        profile + model id, the price pack version, the per-case
        call budget, and the profile's rate card."""
        return service.get_model_config()

    @app.get("/model/cost-ledger")
    def cost_ledger(request: Request) -> dict:
        """The whole cost ledger (R12): every gateway call of
        record, the totals computed from them, and the result of
        re-performing every entry's arithmetic. Under a principal
        the ledger is a guarded surface (R16): supervisor, auditor
        and system tier."""
        principal = _principal(request, service, surface=SURFACE_COST_LEDGER, target="cost_ledger")
        if principal is None:
            entries = service.list_all_cost_entries()
        else:
            try:
                entries = service.list_cost_entries_as(principal)
            except AccessDeniedError as exc:
                _guard(exc)
        return {
            "entries": [e.model_dump(mode="json") for e in entries],
            "totals": service.cost_totals(),
            "verify_problems": service.verify_cost_ledger(),
        }

    @app.get("/cases/{case_id}/cost-ledger")
    def case_cost_ledger(case_id: str, request: Request) -> list[dict]:
        principal = _principal(request, service, surface=SURFACE_COST_LEDGER, target=case_id)
        if principal is None:
            if service.get_case(case_id) is None:
                raise HTTPException(status_code=404, detail="case not found")
            entries = service.list_cost_entries(case_id)
        else:
            try:
                entries = service.list_cost_entries_as(principal, case_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="case not found")
            if entries is None:
                raise HTTPException(status_code=404, detail="case not found")
        return [e.model_dump(mode="json") for e in entries]

    @app.post("/tools/{tool}/invoke")
    def invoke_tool(tool: str, body: ToolInvokeRequest, request: Request):
        """The generic gateway invocation (R11): any tool, with an
        explicit actor. A policy denial is NOT an HTTP error — it
        returns 200 with the AgentCore-shaped JSON-RPC -32002
        envelope, exactly as AgentCore Policy delivers it. With a
        principal (R16) the invocation's actor must be the gateway
        actor the principal's role maps to — a customer principal
        cannot invoke as staff."""
        principal = _principal(request, service, surface=SURFACE_GATEWAY, target=body.case_id)
        actor = body.actor
        if principal is not None:
            try:
                service.require_surface(principal, SURFACE_GATEWAY, target=body.case_id)
                expected = GATEWAY_ACTOR_FOR_ROLE[principal.role]
                if body.actor != expected:
                    service.deny_principal_mismatch(
                        principal, surface=SURFACE_GATEWAY, target=body.case_id,
                        field="actor", claimed=body.actor, on=body.called_on,
                    )
                actor = expected
            except AccessDeniedError as exc:
                _guard(exc)
        arguments = dict(body.arguments)
        for key in ("issued_on", "voided_on", "as_of", "scheduled_on", "graded_on"):
            if isinstance(arguments.get(key), str):
                arguments[key] = date.fromisoformat(arguments[key])
        try:
            result = service.invoke_tool(
                tool,
                arguments,
                case_id=body.case_id,
                called_on=body.called_on,
                actor=actor,
            )
        except PolicyDeniedError as exc:
            return JSONResponse(status_code=200, content=exc.envelope())
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (CarrierError, FraudHoldError, GradingError, PaymentsError, ToolError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FigureVerificationError as exc:
            raise HTTPException(status_code=422, detail={"violations": exc.violations}) from exc
        if isinstance(result, list):
            return [r.model_dump(mode="json") for r in result]
        return result.model_dump(mode="json")

    @app.get("/cases/{case_id}/tool-calls")
    def tool_calls(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [c.model_dump(mode="json") for c in service.list_tool_calls(case_id)]

    @app.post("/cases/{case_id}/labels", status_code=201)
    def create_label(case_id: str, body: LabelRequest, request: Request) -> dict:
        actor = _gateway_actor(request, service, case_id)
        try:
            label = service.invoke_tool(
                "create_return_label",
                {"case_id": case_id, "issued_on": body.issued_on},
                case_id=case_id,
                called_on=body.issued_on,
                **({"actor": actor} if actor else {}),
            )
        except PolicyDeniedError as exc:
            # A gateway policy denial is not an HTTP error: 200 with
            # the JSON-RPC -32002 envelope (R11).
            return JSONResponse(status_code=200, content=exc.envelope())
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
    def void_label(case_id: str, label_id: str, body: LabelVoidRequest, request: Request) -> dict:
        actor = _gateway_actor(request, service, case_id)
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
                **({"actor": actor} if actor else {}),
            )
        except PolicyDeniedError as exc:
            return JSONResponse(status_code=200, content=exc.envelope())
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (CarrierError, ToolError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return void.model_dump(mode="json")

    @app.get("/cases/{case_id}/labels/{label_id}/events")
    def carrier_events(case_id: str, label_id: str, as_of: date, request: Request) -> list[dict]:
        actor = _gateway_actor(request, service, case_id)
        try:
            events = service.invoke_tool(
                "get_carrier_events",
                {"case_id": case_id, "label_id": label_id, "as_of": as_of},
                case_id=case_id,
                called_on=as_of,
                **({"actor": actor} if actor else {}),
            )
        except PolicyDeniedError as exc:
            return JSONResponse(status_code=200, content=exc.envelope())
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ToolError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return [e.model_dump(mode="json") for e in events]

    @app.post("/cases/{case_id}/pickups", status_code=201)
    def book_pickup(case_id: str, body: PickupRequest, request: Request) -> dict:
        actor = _gateway_actor(request, service, case_id)
        try:
            booking = service.invoke_tool(
                "book_pickup",
                {"case_id": case_id, "scheduled_on": body.scheduled_on, "label_id": body.label_id},
                case_id=case_id,
                called_on=body.scheduled_on,
                **({"actor": actor} if actor else {}),
            )
        except PolicyDeniedError as exc:
            return JSONResponse(status_code=200, content=exc.envelope())
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
    def record_warehouse_receipt(case_id: str, body: WarehouseReceiptRequest, request: Request) -> dict:
        _manage_case(request, service, case_id)
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
    def record_grading(case_id: str, body: GradingRequest, request: Request) -> dict:
        _manage_case(request, service, case_id)
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
                photo_metadata=body.photo_metadata,
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
    def execute_top_up(case_id: str, body: ExecutionRequest, request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_MONEY, target=case_id)
        try:
            if principal is None:
                receipt = service.execute_top_up(
                    case_id, idempotency_key=body.idempotency_key, as_of=body.as_of
                )
            else:
                receipt = service.execute_top_up_as(
                    principal, case_id, idempotency_key=body.idempotency_key, as_of=body.as_of
                )
        except AccessDeniedError as exc:
            _guard(exc)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ApprovalError, DuplicateRefundError, FraudHoldError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PaymentsError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return receipt.model_dump(mode="json")

    # -- customer messaging (R8): draft, verify, queue, send -------------
    @app.get("/messaging/disclosure-pack")
    def disclosure_pack() -> dict:
        return service.get_disclosure_pack().model_dump(mode="json")

    @app.get("/messages")
    def all_messages() -> list[dict]:
        return [m.model_dump(mode="json") for m in service.list_all_messages()]

    @app.post("/cases/{case_id}/messages", status_code=201)
    def draft_message(case_id: str, body: MessageDraftRequest, request: Request) -> dict:
        _manage_case(request, service, case_id)
        try:
            message = service.draft_message(
                case_id,
                body.message_type,
                as_of=body.as_of,
                model_note=body.model_note,
                customer_note=body.customer_note,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except MessagingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ModelBudgetError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except ModelGatewayError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return message.model_dump(mode="json")

    @app.get("/cases/{case_id}/messages")
    def messages(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [m.model_dump(mode="json") for m in service.list_messages(case_id)]

    @app.get("/cases/{case_id}/messages/refused")
    def refused_messages(case_id: str) -> list[dict]:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return [m.model_dump(mode="json") for m in service.list_refused_messages(case_id)]

    @app.get("/cases/{case_id}/messages/{message_id}")
    def message(case_id: str, message_id: str) -> dict:
        found = service.get_message(message_id)
        if found is None or found.case_id != case_id:
            raise HTTPException(status_code=404, detail="message not found")
        return found.model_dump(mode="json")

    @app.post("/cases/{case_id}/messages/{message_id}/verify")
    def verify_message(case_id: str, message_id: str, body: MessageActRequest, request: Request) -> dict:
        _manage_case(request, service, case_id)
        found = service.get_message(message_id)
        if found is None or found.case_id != case_id:
            raise HTTPException(status_code=404, detail="message not found")
        try:
            verified = service.verify_message(message_id, as_of=body.as_of)
        except MessagingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return verified.model_dump(mode="json")

    @app.post("/cases/{case_id}/messages/{message_id}/queue")
    def queue_message(case_id: str, message_id: str, body: MessageActRequest, request: Request) -> dict:
        _manage_case(request, service, case_id)
        found = service.get_message(message_id)
        if found is None or found.case_id != case_id:
            raise HTTPException(status_code=404, detail="message not found")
        try:
            queued = service.queue_message(message_id, as_of=body.as_of)
        except MessagingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return queued.model_dump(mode="json")

    @app.post("/cases/{case_id}/messages/{message_id}/send")
    def send_message(case_id: str, message_id: str, body: MessageActRequest, request: Request) -> dict:
        _manage_case(request, service, case_id)
        found = service.get_message(message_id)
        if found is None or found.case_id != case_id:
            raise HTTPException(status_code=404, detail="message not found")
        try:
            sent = service.send_message(message_id, as_of=body.as_of)
        except MessagingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return sent.model_dump(mode="json")

    # -- injection quarantine (R15): submit, read, release ---------------
    @app.get("/quarantine/screening-pack")
    def screening_pack() -> dict:
        """The screening pack as data (R15): the rules customer
        text is judged under, in the pack's own statement."""
        return service.get_screening_pack().model_dump(mode="json")

    @app.get("/cases/{case_id}/quarantine")
    def quarantine_for_case(case_id: str, request: Request) -> dict:
        """The case's quarantine record — the staff surface. Items
        carry their original text HERE (the operations API is where
        a release decision is made from); the console's read model
        never receives it. Views and acts stand beside the items.
        A customer principal (R16) receives only the STATES of
        their own case's items — never contents, never findings."""
        principal = _principal(request, service, surface=SURFACE_QUARANTINE, target=case_id)
        if principal is not None and principal.role == "customer":
            try:
                states = service.quarantine_states_as(principal, case_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="case not found")
            if states is None:
                raise HTTPException(status_code=404, detail="case not found")
            return {"items": states, "acts": []}
        if principal is not None:
            try:
                service.require_surface(principal, SURFACE_QUARANTINE, target=case_id)
                service.get_case_as(principal, case_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="case not found")
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        items = service.list_quarantine_items(case_id)
        views = {v.quarantine_id: v for v in service.list_quarantine_views(case_id)}
        return {
            "items": [
                {
                    **item.model_dump(mode="json"),
                    "state": service.quarantine_state(item),
                    "view": (
                        views[item.quarantine_id].model_dump(mode="json")
                        if item.quarantine_id in views
                        else None
                    ),
                }
                for item in items
            ],
            "acts": [a.model_dump(mode="json") for a in service.list_quarantine_acts(case_id)],
        }

    @app.post("/cases/{case_id}/quarantine", status_code=201)
    def submit_quarantine_text(case_id: str, body: QuarantineSubmitRequest, request: Request) -> dict:
        """Land a customer text sent against an open case in
        quarantine. The response is the item: findings, risk, and
        the released view where one was minted automatically."""
        principal = _principal(request, service, surface=SURFACE_QUARANTINE, target=case_id)
        try:
            if principal is None:
                item = service.submit_customer_text(
                    case_id,
                    source_surface=body.source_surface,
                    field_name=body.field_name,
                    text=body.text,
                    received_on=body.received_on,
                )
            else:
                item = service.submit_customer_text_as(
                    principal,
                    case_id,
                    source_surface=body.source_surface,
                    field_name=body.field_name,
                    text=body.text,
                    received_on=body.received_on,
                )
        except AccessDeniedError as exc:
            _guard(exc, not_found="case not found")
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except QuarantineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        view = service.quarantine_view_for(item.quarantine_id)
        return {
            **item.model_dump(mode="json"),
            "state": service.quarantine_state(item),
            "view": view.model_dump(mode="json") if view else None,
        }

    @app.post("/cases/{case_id}/quarantine/{quarantine_id}/release", status_code=201)
    def release_quarantine_text(
        case_id: str, quarantine_id: str, body: QuarantineReleaseRequest, request: Request
    ) -> dict:
        """The named staff release act for a held text. The view it
        returns is the escaped, data-only form — the act opens the
        text to reading, never to obedience. Under a principal
        (R16) releasing held text is a supervisor act, performed
        by the principal themselves."""
        principal = _principal(request, service, surface=SURFACE_QUARANTINE, target=case_id)
        item = next(
            (i for i in service.list_quarantine_items(case_id) if i.quarantine_id == quarantine_id),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="quarantine item not found")
        try:
            if principal is None:
                view = service.release_quarantined_text(
                    quarantine_id,
                    released_by=body.released_by,
                    reason=body.reason,
                    released_on=body.released_on,
                )
            else:
                if body.released_by != principal.id:
                    service.deny_principal_mismatch(
                        principal, surface=SURFACE_QUARANTINE, target=case_id,
                        field="released_by", claimed=body.released_by, on=body.released_on,
                    )
                view = service.release_quarantined_text_as(
                    principal,
                    quarantine_id,
                    reason=body.reason,
                    released_on=body.released_on,
                    case_id=case_id,
                )
        except AccessDeniedError as exc:
            _guard(exc)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except QuarantineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return view.model_dump(mode="json")

    # -- customer memory (R9): scoped reads, retention, access log --------
    @app.get("/memory/config")
    def memory_config() -> dict:
        return service.get_memory_pack().model_dump(mode="json")

    @app.post("/memory/sync")
    def memory_sync(request: Request) -> dict:
        principal = _principal(request, service, surface=SURFACE_MEMORY, target="memory")
        if principal is not None:
            try:
                service.require_manage_case(principal, "memory")
            except AccessDeniedError as exc:
                _guard(exc)
        return {"records_written": service.sync_memory()}

    @app.get("/memory/access-log")
    def memory_access_log(request: Request) -> list[dict]:
        principal = _principal(request, service, surface=SURFACE_MEMORY, target="memory_access_log")
        if principal is None:
            return [e.model_dump(mode="json") for e in service.list_memory_access_log()]
        try:
            entries = service.list_memory_access_log_as(principal)
        except AccessDeniedError as exc:
            _guard(exc, not_found="customer not found")
        return [e.model_dump(mode="json") for e in entries]

    @app.get("/customers/{customer_id}/memory")
    def customer_memory(customer_id: str, request: Request, as_of: date | None = None) -> list[dict]:
        principal = _principal(request, service, surface=SURFACE_MEMORY, target=customer_id)
        if principal is not None:
            try:
                records = service.retrieve_memory_as(
                    principal, customer_id, as_of=as_of, purpose="api_read", actor="api"
                )
            except AccessDeniedError as exc:
                _guard(exc, not_found="customer not found")
            if principal.role != "customer" and not service.has_customer(customer_id):
                raise HTTPException(status_code=404, detail="customer not found")
            return [r.model_dump(mode="json") for r in records]
        if not service.has_customer(customer_id):
            raise HTTPException(status_code=404, detail="customer not found")
        records = service.retrieve_memory(
            customer_id, as_of=as_of, purpose="api_read", actor="api"
        )
        return [r.model_dump(mode="json") for r in records]

    @app.get("/customers/{customer_id}/memory/retention")
    def customer_memory_retention(customer_id: str, request: Request, as_of: date | None = None) -> dict:
        principal = _principal(request, service, surface=SURFACE_MEMORY, target=customer_id)
        if principal is not None:
            if principal.role != "customer" and not service.has_customer(customer_id):
                raise HTTPException(status_code=404, detail="customer not found")
            try:
                return service.memory_retention_status_as(
                    principal, customer_id, as_of=as_of, actor="api"
                )
            except AccessDeniedError as exc:
                _guard(exc, not_found="customer not found")
        if not service.has_customer(customer_id):
            raise HTTPException(status_code=404, detail="customer not found")
        return service.memory_retention_status(customer_id, as_of=as_of, actor="api")

    @app.get("/customers/{customer_id}/memory/access-log")
    def customer_memory_access_log(customer_id: str, request: Request) -> list[dict]:
        principal = _principal(request, service, surface=SURFACE_MEMORY, target=customer_id)
        if principal is not None:
            try:
                entries = service.list_memory_access_log_as(principal, customer_id)
            except AccessDeniedError as exc:
                _guard(exc, not_found="customer not found")
            return [e.model_dump(mode="json") for e in entries]
        if not service.has_customer(customer_id):
            raise HTTPException(status_code=404, detail="customer not found")
        return [
            e.model_dump(mode="json")
            for e in service.list_memory_access_log(customer_id)
        ]

    @app.get("/cases/{case_id}/fraud-score/from-memory")
    def fraud_score_from_memory(case_id: str) -> dict:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        score = service.score_case_from_memory(case_id)
        if score is None:
            raise HTTPException(
                status_code=404,
                detail="memory holds no decision record for this case (sync first)",
            )
        return score.model_dump(mode="json")

    @app.post("/cases/{case_id}/evidence")
    async def evidence(case_id: str, request: Request, kind: str = "photo", filename: str = "evidence.bin") -> dict:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        content = await request.body()
        return service.attach_evidence(case_id, kind, filename, content)

    return app
