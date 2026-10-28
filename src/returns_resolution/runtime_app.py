"""AgentCore Runtime entrypoint.

The production shape is the bedrock-agentcore SDK's
`BedrockAgentCoreApp` (pinned in pyproject): it serves the Runtime
contract — POST /invocations and GET /ping on port 8080 — and the
entrypoint below receives the invocation payload plus the request
context (session id, headers).

If the SDK cannot be imported in some environment, the module falls
back to a FastAPI app implementing the *identical* HTTP contract
behind this same module, so the container's external shape never
changes. Which path is live is reported at /ping-adjacent startup logs
and in the README's honest status.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .config import Config
from .evidence import LocalEvidenceStore
from .intake import ReferenceFeed
from .modelport import load_model_port
from .models import ReturnRequest
from .service import (
    ApprovalError,
    CaseService,
    FigureVerificationError,
    FraudHoldError,
    FraudReviewError,
    PaymentsError,
)
from .store import SqlStore

try:  # The pinned SDK is a hard dependency; this is a safety net.
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the SDK
    BedrockAgentCoreApp = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False


def sdk_available() -> bool:
    return _SDK_AVAILABLE


def build_service(config: Config | None = None) -> CaseService:
    config = config or Config.from_env()
    store = SqlStore(database_url=config.database_url, sqlite_path=config.sqlite_path)
    store.migrate()
    model_port = load_model_port(
        config.model_profile, model_arn=config.bedrock_model_arn, region=config.aws_region
    )
    evidence = LocalEvidenceStore(root=config.evidence_dir, bucket=config.evidence_bucket)
    service = CaseService(store, model_port, evidence_store=evidence)
    service.seed_orders()
    service.seed_history()
    return service


def handle_payload(service: CaseService, payload: dict[str, Any]) -> dict[str, Any]:
    """The one dispatch every surface shares. Actions:
    seed | intake_feed | intake {request} | get_case {case_id} |
    get_computation {case_id} | verify_computation {case_id} |
    get_disposition {case_id} | verify_disposition {case_id} |
    list_dispositions | get_matrix | get_matrix_cell {cell_id} |
    get_channels | list_cases | list_orders |
    get_fraud_score {case_id} | get_fraud_signals {case_id} |
    verify_fraud_score {case_id} | rescore_case {case_id} |
    record_fraud_review {case_id, reviewer, outcome, reason_code} |
    get_fraud_review {case_id} | get_scoring_pack | list_fraud_scores |
    record_approval {case_id, approver, role, as_of, computation_id?} |
    list_approvals {case_id} | approval_status {case_id} |
    execute_refund {case_id, idempotency_key, as_of} |
    get_receipt {case_id} | list_receipts | list_executions {case_id}.
    """
    action = payload.get("action", "intake_feed")
    if action == "seed":
        return {
            "seeded_orders": service.seed_orders(),
            "seeded_history_cases": service.seed_history(),
        }
    if action == "seed_history":
        return {"seeded_history_cases": service.seed_history()}
    if action == "intake_feed":
        results = []
        for request in ReferenceFeed().fetch_requests():
            try:
                case = service.ingest(request)
                results.append({"request_id": request.request_id, "case_id": case.case_id, "status": case.status})
            except FigureVerificationError as exc:
                results.append({"request_id": request.request_id, "status": "summary_failed", "violations": exc.violations})
        return {"ingested": results}
    if action == "intake":
        request = ReturnRequest.model_validate(payload["request"])
        try:
            case = service.ingest(request)
            return {"case": case.model_dump(mode="json")}
        except FigureVerificationError as exc:
            return {"status": "summary_failed", "violations": exc.violations}
    if action == "get_case":
        case = service.get_case(payload["case_id"])
        return {"case": case.model_dump(mode="json") if case else None}
    if action == "get_computation":
        computation = service.get_computation(payload["case_id"])
        return {"computation": computation.model_dump(mode="json") if computation else None}
    if action == "verify_computation":
        problems = service.verify_stored_computation(payload["case_id"])
        return {"case_id": payload["case_id"], "ok": not problems, "problems": problems}
    if action == "get_disposition":
        disposition = service.get_disposition(payload["case_id"])
        return {"disposition": disposition.model_dump(mode="json") if disposition else None}
    if action == "verify_disposition":
        problems = service.verify_stored_disposition(payload["case_id"])
        return {"case_id": payload["case_id"], "ok": not problems, "problems": problems}
    if action == "list_dispositions":
        return {"dispositions": [d.model_dump(mode="json") for d in service.list_dispositions()]}
    if action == "get_matrix":
        return {"matrix": service.get_matrix().model_dump(mode="json")}
    if action == "get_matrix_cell":
        cell = service.get_matrix().cell_by_id(payload["cell_id"])
        return {"cell": cell.model_dump(mode="json") if cell else None}
    if action == "get_channels":
        return {"channels": service.get_channels().model_dump(mode="json")}
    if action == "get_fraud_score":
        score = service.get_fraud_score(payload["case_id"])
        return {"fraud_score": score.model_dump(mode="json") if score else None}
    if action == "get_fraud_signals":
        score = service.get_fraud_score(payload["case_id"])
        if score is None:
            return {"signals": None}
        return {
            "case_id": score.case_id,
            "score_id": score.score_id,
            "pack_version": score.pack_version,
            "total_score": str(score.total_score),
            "band": score.band,
            "signals": [s.model_dump(mode="json") for s in score.signals],
        }
    if action == "verify_fraud_score":
        problems = service.verify_stored_fraud_score(payload["case_id"])
        return {"case_id": payload["case_id"], "ok": not problems, "problems": problems}
    if action == "rescore_case":
        score = service.rescore_case(payload["case_id"])
        return {"fraud_score": score.model_dump(mode="json") if score else None}
    if action == "record_fraud_review":
        try:
            decision = service.record_fraud_review(
                payload["case_id"],
                reviewer=payload.get("reviewer", ""),
                outcome=payload.get("outcome", ""),
                reason_code=payload.get("reason_code", ""),
            )
            return {"fraud_review": decision.model_dump(mode="json")}
        except (LookupError, FraudReviewError) as exc:
            return {"error": str(exc)}
    if action == "get_fraud_review":
        review = service.get_fraud_review(payload["case_id"])
        return {"fraud_review": review.model_dump(mode="json") if review else None}
    if action == "get_scoring_pack":
        return {"scoring_pack": service.get_scoring_pack().model_dump(mode="json")}
    if action == "list_fraud_scores":
        return {"fraud_scores": [s.model_dump(mode="json") for s in service.list_fraud_scores()]}
    if action == "record_approval":
        try:
            approval = service.record_approval(
                payload["case_id"],
                approver=payload.get("approver", ""),
                role=payload.get("role", ""),
                as_of=date.fromisoformat(payload["as_of"]),
                computation_id=payload.get("computation_id"),
            )
            return {"approval": approval.model_dump(mode="json")}
        except (LookupError, ApprovalError, FraudHoldError) as exc:
            return {"error": str(exc)}
    if action == "list_approvals":
        return {"approvals": [a.model_dump(mode="json") for a in service.list_approvals(payload["case_id"])]}
    if action == "approval_status":
        try:
            return {"approval_status": service.approval_status(payload["case_id"])}
        except LookupError as exc:
            return {"error": str(exc)}
    if action == "execute_refund":
        try:
            receipt = service.execute_refund(
                payload["case_id"],
                idempotency_key=payload.get("idempotency_key", ""),
                as_of=date.fromisoformat(payload["as_of"]),
            )
            return {"receipt": receipt.model_dump(mode="json")}
        except (LookupError, ApprovalError, FraudHoldError, PaymentsError) as exc:
            return {"error": str(exc)}
    if action == "get_receipt":
        receipt = service.get_receipt(payload["case_id"])
        return {"receipt": receipt.model_dump(mode="json") if receipt else None}
    if action == "list_receipts":
        return {"receipts": [r.model_dump(mode="json") for r in service.list_receipts()]}
    if action == "list_executions":
        return {"executions": [e.model_dump(mode="json") for e in service.list_executions(payload["case_id"])]}
    if action == "list_cases":
        return {"cases": [c.model_dump(mode="json") for c in service.list_cases()]}
    if action == "list_orders":
        return {"orders": [o.model_dump(mode="json") for o in service.list_orders()]}
    return {"error": f"unknown action: {action}"}


if _SDK_AVAILABLE:
    app = BedrockAgentCoreApp()
    _service: CaseService | None = None

    def _svc() -> CaseService:
        global _service
        if _service is None:
            _service = build_service()
        return _service

    @app.entrypoint
    def agentcore_entrypoint(payload: dict[str, Any], context: Any) -> dict[str, Any]:
        return handle_payload(_svc(), payload or {})

else:  # pragma: no cover - fallback contract implementation
    from fastapi import FastAPI

    app = FastAPI(title="Returns Resolution Agent (AgentCore contract fallback)")
    _service = None

    def _svc() -> CaseService:
        global _service
        if _service is None:
            _service = build_service()
        return _service

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "Healthy"}

    @app.post("/invocations")
    def invocations(payload: dict[str, Any]) -> dict[str, Any]:
        return handle_payload(_svc(), payload or {})


def main() -> None:
    if _SDK_AVAILABLE:
        app.run(port=8080, host="0.0.0.0")
    else:  # pragma: no cover
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
