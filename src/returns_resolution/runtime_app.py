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
from .model_gateway import ModelGatewayError, load_model_gateway
from .modelport import OfflineComposer
from .models import ReturnRequest
from .gateway import PolicyDeniedError, ToolError
from .service import (
    ApprovalError,
    CarrierError,
    CaseService,
    FigureVerificationError,
    FraudHoldError,
    FraudReviewError,
    GradingError,
    MessagingError,
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
    # The model gateway (R12) is the one model path. The offline
    # profile wraps the deterministic composer; the hosted
    # profiles (bedrock / nim) are built fail-closed — no transport
    # is bound in this build, so selecting one refuses loudly at
    # the first drafting call rather than falling back.
    gateway = load_model_gateway(
        config.model_profile,
        bedrock_model_id=config.bedrock_model_id,
        nim_model_id=config.nim_model_id,
    )
    evidence = LocalEvidenceStore(root=config.evidence_dir, bucket=config.evidence_bucket)
    service = CaseService(store, OfflineComposer(), model_gateway=gateway, evidence_store=evidence)
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
    get_receipt {case_id} | list_receipts | list_executions {case_id} |
    get_tool_definitions | invoke_tool {tool, case_id, arguments, called_on, actor?} |
    get_gateway_policy_pack |
    get_model_config | get_case_cost_ledger {case_id} | get_cost_ledger |
    create_return_label {case_id, issued_on} |
    void_return_label {case_id, label_id, voided_by, reason, voided_on} |
    get_carrier_events {case_id, label_id, as_of} |
    book_pickup {case_id, scheduled_on, label_id?} |
    get_label {case_id} | list_labels {case_id} | list_pickups {case_id} |
    list_tool_calls {case_id} | get_timeline {case_id, as_of} |
    record_warehouse_receipt {case_id, received_by, received_on} |
    list_warehouse_receipts {case_id} |
    record_grading {case_id, grader, graded_condition, graded_on,
    functional_test_result?, data_wipe_status?, damage_notes?,
    received_weight_grams?, wear_evidence?} |
    list_grading_reports {case_id} | list_adjustments {case_id} |
    execute_top_up {case_id, idempotency_key, as_of} |
    draft_message {case_id, message_type, as_of, model_note?} |
    verify_message {message_id, as_of} | queue_message {message_id, as_of} |
    send_message {message_id, as_of} | get_message {message_id} |
    list_messages {case_id} | list_refused_messages {case_id} |
    get_disclosure_pack |
    get_memory_config | sync_memory |
    get_customer_memory {customer_id, as_of?} |
    get_memory_retention {customer_id, as_of?} |
    list_memory_access_log {customer_id?} |
    score_case_from_memory {case_id}.
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
            except ModelGatewayError as exc:
                results.append({"request_id": request.request_id, "status": "model_gateway_failed", "error": str(exc)})
        return {"ingested": results}
    if action == "intake":
        request = ReturnRequest.model_validate(payload["request"])
        try:
            case = service.ingest(request)
            return {"case": case.model_dump(mode="json")}
        except FigureVerificationError as exc:
            return {"status": "summary_failed", "violations": exc.violations}
        except ModelGatewayError as exc:
            return {"error": str(exc)}
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
    if action == "get_tool_definitions":
        return {"tools": [d.model_dump(mode="json") for d in service.tool_definitions()]}
    if action == "get_gateway_policy_pack":
        return {"gateway_policy_pack": service.get_gateway_policy_pack().model_dump(mode="json")}
    if action == "get_model_config":
        return {"model_config": service.get_model_config()}
    if action == "get_case_cost_ledger":
        return {
            "cost_entries": [
                e.model_dump(mode="json") for e in service.list_cost_entries(payload["case_id"])
            ]
        }
    if action == "get_cost_ledger":
        return {
            "cost_entries": [
                e.model_dump(mode="json") for e in service.list_all_cost_entries()
            ],
            "cost_totals": service.cost_totals(),
            "verify_problems": service.verify_cost_ledger(),
        }
    if action == "invoke_tool":
        arguments = dict(payload.get("arguments") or {})
        for key in ("issued_on", "voided_on", "as_of", "scheduled_on", "graded_on"):
            if isinstance(arguments.get(key), str):
                arguments[key] = date.fromisoformat(arguments[key])
        try:
            result = service.invoke_tool(
                payload["tool"],
                arguments,
                case_id=payload["case_id"],
                called_on=date.fromisoformat(payload["called_on"]),
                actor=payload.get("actor", "staff"),
            )
        except PolicyDeniedError as exc:
            # A gateway policy denial rides the transport as the
            # JSON-RPC -32002 envelope — it is not an action error.
            return {"gateway_denial": exc.envelope()}
        except (LookupError, CarrierError, FraudHoldError, ToolError, FigureVerificationError) as exc:
            return {"error": str(exc)}
        if isinstance(result, list):
            return {"result": [r.model_dump(mode="json") for r in result]}
        return {"result": result.model_dump(mode="json")}
    if action == "create_return_label":
        try:
            label = service.invoke_tool(
                "create_return_label",
                {"case_id": payload["case_id"], "issued_on": date.fromisoformat(payload["issued_on"])},
                case_id=payload["case_id"],
                called_on=date.fromisoformat(payload["issued_on"]),
            )
            return {"label": label.model_dump(mode="json")}
        except (LookupError, CarrierError, FraudHoldError, ToolError, FigureVerificationError) as exc:
            return {"error": str(exc)}
    if action == "void_return_label":
        try:
            void = service.invoke_tool(
                "void_return_label",
                {
                    "case_id": payload["case_id"],
                    "label_id": payload["label_id"],
                    "voided_by": payload.get("voided_by", ""),
                    "reason": payload.get("reason", ""),
                    "voided_on": date.fromisoformat(payload["voided_on"]),
                },
                case_id=payload["case_id"],
                called_on=date.fromisoformat(payload["voided_on"]),
            )
            return {"label_void": void.model_dump(mode="json")}
        except (LookupError, CarrierError, ToolError) as exc:
            return {"error": str(exc)}
    if action == "get_carrier_events":
        try:
            events = service.invoke_tool(
                "get_carrier_events",
                {
                    "case_id": payload["case_id"],
                    "label_id": payload["label_id"],
                    "as_of": date.fromisoformat(payload["as_of"]),
                },
                case_id=payload["case_id"],
                called_on=date.fromisoformat(payload["as_of"]),
            )
            return {"events": [e.model_dump(mode="json") for e in events]}
        except (LookupError, ToolError) as exc:
            return {"error": str(exc)}
    if action == "book_pickup":
        try:
            booking = service.invoke_tool(
                "book_pickup",
                {
                    "case_id": payload["case_id"],
                    "scheduled_on": date.fromisoformat(payload["scheduled_on"]),
                    "label_id": payload.get("label_id"),
                },
                case_id=payload["case_id"],
                called_on=date.fromisoformat(payload["scheduled_on"]),
            )
            return {"pickup": booking.model_dump(mode="json")}
        except (LookupError, CarrierError, ToolError) as exc:
            return {"error": str(exc)}
    if action == "get_label":
        label = service.get_active_label(payload["case_id"])
        return {"label": label.model_dump(mode="json") if label else None}
    if action == "list_labels":
        return {"labels": [label.model_dump(mode="json") for label in service.list_labels(payload["case_id"])]}
    if action == "list_pickups":
        return {"pickups": [p.model_dump(mode="json") for p in service.list_pickups(payload["case_id"])]}
    if action == "list_tool_calls":
        return {"tool_calls": [c.model_dump(mode="json") for c in service.list_tool_calls(payload["case_id"])]}
    if action == "get_timeline":
        try:
            view = service.get_timeline(payload["case_id"], date.fromisoformat(payload["as_of"]))
            return {"timeline": view.model_dump(mode="json")}
        except LookupError as exc:
            return {"error": str(exc)}
    if action == "record_warehouse_receipt":
        try:
            receipt = service.record_warehouse_receipt(
                payload["case_id"],
                received_by=payload.get("received_by", ""),
                received_on=date.fromisoformat(payload["received_on"]),
            )
            return {"warehouse_receipt": receipt.model_dump(mode="json")}
        except (LookupError, GradingError) as exc:
            return {"error": str(exc)}
    if action == "list_warehouse_receipts":
        return {
            "warehouse_receipts": [
                r.model_dump(mode="json")
                for r in service.list_warehouse_receipts(payload["case_id"])
            ]
        }
    if action == "record_grading":
        try:
            report = service.record_grading(
                payload["case_id"],
                grader=payload.get("grader", ""),
                graded_condition=payload.get("graded_condition", ""),
                graded_on=date.fromisoformat(payload["graded_on"]),
                functional_test_result=payload.get("functional_test_result"),
                data_wipe_status=payload.get("data_wipe_status"),
                damage_notes=payload.get("damage_notes", ""),
                received_weight_grams=payload.get("received_weight_grams"),
                wear_evidence=bool(payload.get("wear_evidence", False)),
            )
            return {"grading_report": report.model_dump(mode="json")}
        except (LookupError, GradingError, FigureVerificationError) as exc:
            return {"error": str(exc)}
    if action == "list_grading_reports":
        return {
            "grading_reports": [
                r.model_dump(mode="json")
                for r in service.list_grading_reports(payload["case_id"])
            ]
        }
    if action == "list_adjustments":
        return {
            "adjustments": [
                a.model_dump(mode="json") for a in service.list_adjustments(payload["case_id"])
            ]
        }
    if action == "execute_top_up":
        try:
            receipt = service.execute_top_up(
                payload["case_id"],
                idempotency_key=payload.get("idempotency_key", ""),
                as_of=date.fromisoformat(payload["as_of"]),
            )
            return {"receipt": receipt.model_dump(mode="json")}
        except (LookupError, ApprovalError, FraudHoldError, PaymentsError) as exc:
            return {"error": str(exc)}
    if action == "draft_message":
        try:
            message = service.draft_message(
                payload["case_id"],
                payload.get("message_type", ""),
                as_of=date.fromisoformat(payload["as_of"]),
                model_note=payload.get("model_note"),
            )
            return {"message": message.model_dump(mode="json")}
        except (LookupError, MessagingError, ModelGatewayError) as exc:
            return {"error": str(exc)}
    if action == "verify_message":
        try:
            message = service.verify_message(
                payload["message_id"], as_of=date.fromisoformat(payload["as_of"])
            )
            return {"message": message.model_dump(mode="json")}
        except (LookupError, MessagingError) as exc:
            return {"error": str(exc)}
    if action == "queue_message":
        try:
            message = service.queue_message(
                payload["message_id"], as_of=date.fromisoformat(payload["as_of"])
            )
            return {"message": message.model_dump(mode="json")}
        except (LookupError, MessagingError) as exc:
            return {"error": str(exc)}
    if action == "send_message":
        try:
            message = service.send_message(
                payload["message_id"], as_of=date.fromisoformat(payload["as_of"])
            )
            return {"message": message.model_dump(mode="json")}
        except (LookupError, MessagingError) as exc:
            return {"error": str(exc)}
    if action == "get_message":
        message = service.get_message(payload["message_id"])
        return {"message": message.model_dump(mode="json") if message else None}
    if action == "list_messages":
        return {
            "messages": [
                m.model_dump(mode="json") for m in service.list_messages(payload["case_id"])
            ]
        }
    if action == "list_refused_messages":
        return {
            "messages": [
                m.model_dump(mode="json")
                for m in service.list_refused_messages(payload["case_id"])
            ]
        }
    if action == "get_disclosure_pack":
        return {"disclosure_pack": service.get_disclosure_pack().model_dump(mode="json")}
    if action == "get_memory_config":
        return {"memory_pack": service.get_memory_pack().model_dump(mode="json")}
    if action == "sync_memory":
        return {"records_written": service.sync_memory()}
    if action == "get_customer_memory":
        as_of = payload.get("as_of")
        records = service.retrieve_memory(
            payload["customer_id"],
            as_of=date.fromisoformat(as_of) if as_of else None,
            purpose="runtime_read",
            actor="runtime dispatch",
        )
        return {"records": [r.model_dump(mode="json") for r in records]}
    if action == "get_memory_retention":
        as_of = payload.get("as_of")
        return {
            "retention": service.memory_retention_status(
                payload["customer_id"],
                as_of=date.fromisoformat(as_of) if as_of else None,
                actor="runtime dispatch",
            )
        }
    if action == "list_memory_access_log":
        entries = service.list_memory_access_log(payload.get("customer_id"))
        return {"access_log": [e.model_dump(mode="json") for e in entries]}
    if action == "score_case_from_memory":
        score = service.score_case_from_memory(payload["case_id"])
        return {"fraud_score": score.model_dump(mode="json") if score else None}
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
