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
from decimal import Decimal
from typing import Any

from .access import (
    GATEWAY_ACTOR_FOR_ROLE,
    HIDING_REASONS,
    SURFACE_EXCEPTIONS,
    SURFACE_FRAUD,
    SURFACE_GATEWAY,
    SURFACE_MONEY,
    SURFACE_QUARANTINE,
    SURFACE_REPORTS,
    AccessDeniedError,
)
from .carrier_exceptions import CarrierExceptionError
from .config import Config
from .evidence import LocalEvidenceStore
from .intake import ReferenceFeed
from .model_gateway import ModelGatewayError, load_model_gateway
from .modelport import OfflineComposer
from .models import ReturnRequest
from .gateway import PolicyDeniedError, ToolError
from .quarantine import QuarantineError
from .reporting import ReportError
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
    received_weight_grams?, wear_evidence?, photo_metadata?} |
    list_grading_reports {case_id} | list_adjustments {case_id} |
    execute_top_up {case_id, idempotency_key, as_of} |
    draft_message {case_id, message_type, as_of, model_note?, customer_note?} |
    verify_message {message_id, as_of} | queue_message {message_id, as_of} |
    send_message {message_id, as_of} | get_message {message_id} |
    list_messages {case_id} | list_refused_messages {case_id} |
    get_disclosure_pack |
    get_screening_pack |
    submit_customer_text {case_id, source_surface, field_name, text, received_on} |
    list_quarantine {case_id} |
    release_quarantined_text {quarantine_id, released_by, reason, released_on} |
    get_memory_config | sync_memory |
    get_customer_memory {customer_id, as_of?} |
    get_memory_retention {customer_id, as_of?} |
    list_memory_access_log {customer_id?} |
    score_case_from_memory {case_id} |
    get_access_pack | list_access_denials |
    get_report_pack |
    compute_report {kind, window_start?, window_end?, as_of} |
    get_report {report_id} | list_reports {kind?} |
    verify_report {report_id} |
    get_exception_pack |
    detect_exceptions {case_id, as_of} |
    list_exceptions {case_id} |
    get_exception_status {case_id} |
    record_exception_act {exception_id, exception_action, acted_on,
        note?} |
    file_carrier_claim {exception_id, filed_on} |
    list_carrier_claims {case_id} |
    record_claim_act {claim_id, claim_action, acted_on,
        settled_amount?, reason_code?, note?} |
    verify_exception {exception_id} |
    verify_carrier_claim {claim_id}.

    Any action may carry a principal (R16): payload["principal"]
    = {"id": ..., "role": ...}. A carried principal is
    authenticated against the access pack and the action runs
    under the same rules as the API — scoped reads, gated acts,
    recorded denials; the hiding refusals render as the action's
    ordinary empty result. A payload without a principal takes
    the pre-R16 trusted-local path, unchanged.
    """
    action = payload.get("action", "intake_feed")
    principal = None
    if payload.get("principal") is not None:
        try:
            principal = service.authenticate_principal(
                payload.get("principal"), surface="runtime", target=action
            )
        except AccessDeniedError as exc:
            return {"error": str(exc), "access_reason": exc.reason}

    def _denied(exc: AccessDeniedError, hidden: dict) -> dict:
        """A guard refusal on the Runtime surface: the hiding
        reasons render as the action's ordinary empty result;
        every other refusal is the named error."""
        if exc.reason in HIDING_REASONS:
            return hidden
        return {"error": str(exc), "access_reason": exc.reason}

    def _shortcut_actor() -> str:
        """The gateway actor for the tool shortcut actions: a
        carried principal invokes as the actor its role maps to;
        the legacy path invokes as staff, as it always has."""
        if principal is None:
            return "staff"
        return GATEWAY_ACTOR_FOR_ROLE[principal.role]
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
        if principal is not None:
            try:
                service.require_intake(principal, request.order_id)
            except AccessDeniedError as exc:
                return {"error": str(exc), "access_reason": exc.reason}
        try:
            case = service.ingest(request)
            return {"case": case.model_dump(mode="json")}
        except FigureVerificationError as exc:
            return {"status": "summary_failed", "violations": exc.violations}
        except ModelGatewayError as exc:
            return {"error": str(exc)}
    if action == "get_case":
        if principal is not None:
            try:
                view = service.get_case_view_as(principal, payload["case_id"])
            except AccessDeniedError as exc:
                return _denied(exc, {"case": None})
            return {"case": view}
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
        if principal is not None:
            try:
                score = service.get_fraud_score_as(principal, payload["case_id"])
            except AccessDeniedError as exc:
                return _denied(exc, {"fraud_score": None})
            return {"fraud_score": score.model_dump(mode="json") if score else None}
        score = service.get_fraud_score(payload["case_id"])
        return {"fraud_score": score.model_dump(mode="json") if score else None}
    if action == "get_fraud_signals":
        if principal is not None:
            try:
                score = service.get_fraud_score_as(principal, payload["case_id"])
            except AccessDeniedError as exc:
                return _denied(exc, {"signals": None})
        else:
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
        if principal is not None:
            try:
                service.require_manage_case(principal, payload["case_id"])
            except AccessDeniedError as exc:
                return {"error": str(exc), "access_reason": exc.reason}
        score = service.rescore_case(payload["case_id"])
        return {"fraud_score": score.model_dump(mode="json") if score else None}
    if action == "record_fraud_review":
        try:
            if principal is not None:
                if payload.get("reviewer", "") != principal.id:
                    service.deny_principal_mismatch(
                        principal, surface=SURFACE_FRAUD, target=payload["case_id"],
                        field="reviewer", claimed=payload.get("reviewer", ""),
                    )
                decision = service.record_fraud_review_as(
                    principal,
                    payload["case_id"],
                    outcome=payload.get("outcome", ""),
                    reason_code=payload.get("reason_code", ""),
                )
            else:
                decision = service.record_fraud_review(
                    payload["case_id"],
                    reviewer=payload.get("reviewer", ""),
                    outcome=payload.get("outcome", ""),
                    reason_code=payload.get("reason_code", ""),
                )
            return {"fraud_review": decision.model_dump(mode="json")}
        except AccessDeniedError as exc:
            return {"error": str(exc), "access_reason": exc.reason}
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
            if principal is not None:
                if payload.get("approver", "") != principal.id:
                    service.deny_principal_mismatch(
                        principal, surface=SURFACE_MONEY, target=payload["case_id"],
                        field="approver", claimed=payload.get("approver", ""),
                        on=date.fromisoformat(payload["as_of"]),
                    )
                approval = service.record_approval_as(
                    principal,
                    payload["case_id"],
                    as_of=date.fromisoformat(payload["as_of"]),
                    computation_id=payload.get("computation_id"),
                )
            else:
                approval = service.record_approval(
                    payload["case_id"],
                    approver=payload.get("approver", ""),
                    role=payload.get("role", ""),
                    as_of=date.fromisoformat(payload["as_of"]),
                    computation_id=payload.get("computation_id"),
                )
            return {"approval": approval.model_dump(mode="json")}
        except AccessDeniedError as exc:
            return {"error": str(exc), "access_reason": exc.reason}
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
            if principal is not None:
                receipt = service.execute_refund_as(
                    principal,
                    payload["case_id"],
                    idempotency_key=payload.get("idempotency_key", ""),
                    as_of=date.fromisoformat(payload["as_of"]),
                )
            else:
                receipt = service.execute_refund(
                    payload["case_id"],
                    idempotency_key=payload.get("idempotency_key", ""),
                    as_of=date.fromisoformat(payload["as_of"]),
                )
            return {"receipt": receipt.model_dump(mode="json")}
        except AccessDeniedError as exc:
            return {"error": str(exc), "access_reason": exc.reason}
        except (LookupError, ApprovalError, FraudHoldError, PaymentsError) as exc:
            return {"error": str(exc)}
    if action == "get_receipt":
        if principal is not None:
            try:
                service.get_case_as(principal, payload["case_id"])
            except AccessDeniedError as exc:
                return _denied(exc, {"receipt": None})
        receipt = service.get_receipt(payload["case_id"])
        return {"receipt": receipt.model_dump(mode="json") if receipt else None}
    if action == "list_receipts":
        rows = service.list_receipts()
        if principal is not None:
            try:
                service.require_surface(principal, SURFACE_MONEY, target="receipts")
                if principal.role == "customer":
                    own = {c.case_id for c in service.list_cases_as(principal)}
                    rows = [r for r in rows if r.case_id in own]
            except AccessDeniedError as exc:
                return {"error": str(exc), "access_reason": exc.reason}
        return {"receipts": [r.model_dump(mode="json") for r in rows]}
    if action == "list_executions":
        return {"executions": [e.model_dump(mode="json") for e in service.list_executions(payload["case_id"])]}
    if action == "get_tool_definitions":
        return {"tools": [d.model_dump(mode="json") for d in service.tool_definitions()]}
    if action == "get_gateway_policy_pack":
        return {"gateway_policy_pack": service.get_gateway_policy_pack().model_dump(mode="json")}
    if action == "get_model_config":
        return {"model_config": service.get_model_config()}
    if action == "get_case_cost_ledger":
        if principal is not None:
            try:
                entries = service.list_cost_entries_as(principal, payload["case_id"])
            except AccessDeniedError as exc:
                return _denied(exc, {"cost_entries": []})
            return {
                "cost_entries": [
                    e.model_dump(mode="json") for e in (entries or [])
                ]
            }
        return {
            "cost_entries": [
                e.model_dump(mode="json") for e in service.list_cost_entries(payload["case_id"])
            ]
        }
    if action == "get_cost_ledger":
        if principal is not None:
            try:
                entries = service.list_cost_entries_as(principal)
            except AccessDeniedError as exc:
                return {"error": str(exc), "access_reason": exc.reason}
            return {
                "cost_entries": [e.model_dump(mode="json") for e in entries],
                "cost_totals": service.cost_totals(),
                "verify_problems": service.verify_cost_ledger(),
            }
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
        actor = payload.get("actor", "staff")
        if principal is not None:
            try:
                service.require_surface(
                    principal, SURFACE_GATEWAY, target=payload["case_id"]
                )
                expected = GATEWAY_ACTOR_FOR_ROLE[principal.role]
                if payload.get("actor") is not None and payload["actor"] != expected:
                    service.deny_principal_mismatch(
                        principal, surface=SURFACE_GATEWAY, target=payload["case_id"],
                        field="actor", claimed=payload["actor"],
                        on=date.fromisoformat(payload["called_on"]),
                    )
                actor = expected
            except AccessDeniedError as exc:
                return {"error": str(exc), "access_reason": exc.reason}
        try:
            result = service.invoke_tool(
                payload["tool"],
                arguments,
                case_id=payload["case_id"],
                called_on=date.fromisoformat(payload["called_on"]),
                actor=actor,
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
                actor=_shortcut_actor(),
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
                actor=_shortcut_actor(),
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
                actor=_shortcut_actor(),
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
                actor=_shortcut_actor(),
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
                photo_metadata=payload.get("photo_metadata"),
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
            if principal is not None:
                receipt = service.execute_top_up_as(
                    principal,
                    payload["case_id"],
                    idempotency_key=payload.get("idempotency_key", ""),
                    as_of=date.fromisoformat(payload["as_of"]),
                )
            else:
                receipt = service.execute_top_up(
                    payload["case_id"],
                    idempotency_key=payload.get("idempotency_key", ""),
                    as_of=date.fromisoformat(payload["as_of"]),
                )
            return {"receipt": receipt.model_dump(mode="json")}
        except AccessDeniedError as exc:
            return {"error": str(exc), "access_reason": exc.reason}
        except (LookupError, ApprovalError, FraudHoldError, PaymentsError) as exc:
            return {"error": str(exc)}
    if action == "draft_message":
        try:
            message = service.draft_message(
                payload["case_id"],
                payload.get("message_type", ""),
                as_of=date.fromisoformat(payload["as_of"]),
                model_note=payload.get("model_note"),
                customer_note=payload.get("customer_note"),
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
    if action == "get_screening_pack":
        return {"screening_pack": service.get_screening_pack().model_dump(mode="json")}
    if action == "submit_customer_text":
        try:
            if principal is not None:
                item = service.submit_customer_text_as(
                    principal,
                    payload["case_id"],
                    source_surface=payload.get("source_surface", ""),
                    field_name=payload.get("field_name", ""),
                    text=payload.get("text", ""),
                    received_on=date.fromisoformat(payload["received_on"]),
                )
            else:
                item = service.submit_customer_text(
                    payload["case_id"],
                    source_surface=payload.get("source_surface", ""),
                    field_name=payload.get("field_name", ""),
                    text=payload.get("text", ""),
                    received_on=date.fromisoformat(payload["received_on"]),
                )
            view = service.quarantine_view_for(item.quarantine_id)
            return {
                "quarantine_item": item.model_dump(mode="json"),
                "state": service.quarantine_state(item),
                "view": view.model_dump(mode="json") if view else None,
            }
        except AccessDeniedError as exc:
            return _denied(exc, {"error": f"unknown case: {payload['case_id']}"})
        except (LookupError, QuarantineError) as exc:
            return {"error": str(exc)}
    if action == "list_quarantine":
        if principal is not None and principal.role == "customer":
            try:
                states = service.quarantine_states_as(principal, payload["case_id"])
            except AccessDeniedError as exc:
                return _denied(exc, {"items": [], "acts": []})
            if states is None:
                return {"items": [], "acts": []}
            return {"items": states, "acts": []}
        items = service.list_quarantine_items(payload["case_id"])
        views = {v.quarantine_id: v for v in service.list_quarantine_views(payload["case_id"])}
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
            "acts": [
                a.model_dump(mode="json")
                for a in service.list_quarantine_acts(payload["case_id"])
            ],
        }
    if action == "release_quarantined_text":
        try:
            if principal is not None:
                if payload.get("released_by", "") != principal.id:
                    service.deny_principal_mismatch(
                        principal, surface=SURFACE_QUARANTINE,
                        target=payload["quarantine_id"],
                        field="released_by", claimed=payload.get("released_by", ""),
                        on=date.fromisoformat(payload["released_on"]),
                    )
                view = service.release_quarantined_text_as(
                    principal,
                    payload["quarantine_id"],
                    reason=payload.get("reason", ""),
                    released_on=date.fromisoformat(payload["released_on"]),
                )
            else:
                view = service.release_quarantined_text(
                    payload["quarantine_id"],
                    released_by=payload.get("released_by", ""),
                    reason=payload.get("reason", ""),
                    released_on=date.fromisoformat(payload["released_on"]),
                )
            return {"view": view.model_dump(mode="json")}
        except AccessDeniedError as exc:
            return {"error": str(exc), "access_reason": exc.reason}
        except (LookupError, QuarantineError) as exc:
            return {"error": str(exc)}
    if action == "get_memory_config":
        return {"memory_pack": service.get_memory_pack().model_dump(mode="json")}
    if action == "sync_memory":
        return {"records_written": service.sync_memory()}
    if action == "get_customer_memory":
        as_of = payload.get("as_of")
        if principal is not None:
            try:
                records = service.retrieve_memory_as(
                    principal,
                    payload["customer_id"],
                    as_of=date.fromisoformat(as_of) if as_of else None,
                    purpose="runtime_read",
                    actor="runtime dispatch",
                )
            except AccessDeniedError as exc:
                return _denied(exc, {"records": []})
            return {"records": [r.model_dump(mode="json") for r in records]}
        records = service.retrieve_memory(
            payload["customer_id"],
            as_of=date.fromisoformat(as_of) if as_of else None,
            purpose="runtime_read",
            actor="runtime dispatch",
        )
        return {"records": [r.model_dump(mode="json") for r in records]}
    if action == "get_memory_retention":
        as_of = payload.get("as_of")
        if principal is not None:
            try:
                retention = service.memory_retention_status_as(
                    principal,
                    payload["customer_id"],
                    as_of=date.fromisoformat(as_of) if as_of else None,
                    actor="runtime dispatch",
                )
            except AccessDeniedError as exc:
                return _denied(exc, {"retention": None})
            return {"retention": retention}
        return {
            "retention": service.memory_retention_status(
                payload["customer_id"],
                as_of=date.fromisoformat(as_of) if as_of else None,
                actor="runtime dispatch",
            )
        }
    if action == "list_memory_access_log":
        if principal is not None:
            try:
                entries = service.list_memory_access_log_as(
                    principal, payload.get("customer_id")
                )
            except AccessDeniedError as exc:
                return _denied(exc, {"access_log": []})
            return {"access_log": [e.model_dump(mode="json") for e in entries]}
        entries = service.list_memory_access_log(payload.get("customer_id"))
        return {"access_log": [e.model_dump(mode="json") for e in entries]}
    if action == "score_case_from_memory":
        score = service.score_case_from_memory(payload["case_id"])
        return {"fraud_score": score.model_dump(mode="json") if score else None}
    if action == "list_cases":
        if principal is not None:
            try:
                if principal.role == "customer":
                    return {
                        "cases": [
                            service.get_case_view_as(principal, c.case_id)
                            for c in service.list_cases_as(principal)
                        ]
                    }
                return {
                    "cases": [
                        c.model_dump(mode="json") for c in service.list_cases_as(principal)
                    ]
                }
            except AccessDeniedError as exc:
                return {"error": str(exc), "access_reason": exc.reason}
        return {"cases": [c.model_dump(mode="json") for c in service.list_cases()]}
    if action == "list_orders":
        if principal is not None:
            try:
                return {
                    "orders": [
                        o.model_dump(mode="json") for o in service.list_orders_as(principal)
                    ]
                }
            except AccessDeniedError as exc:
                return {"error": str(exc), "access_reason": exc.reason}
        return {"orders": [o.model_dump(mode="json") for o in service.list_orders()]}
    if action == "get_access_pack":
        return {"access_pack": service.get_access_pack().model_dump(mode="json")}
    if action == "list_access_denials":
        if principal is not None:
            try:
                entries = service.list_access_denials_as(principal)
            except AccessDeniedError as exc:
                return {"error": str(exc), "access_reason": exc.reason}
        else:
            entries = service.list_access_denials()
        return {"access_denials": [e.model_dump(mode="json") for e in entries]}
    if action == "get_report_pack":
        return {"report_pack": service.get_report_pack().model_dump(mode="json")}
    if action == "compute_report":
        window_start = (
            date.fromisoformat(payload["window_start"]) if payload.get("window_start") else None
        )
        window_end = (
            date.fromisoformat(payload["window_end"]) if payload.get("window_end") else None
        )
        as_of = date.fromisoformat(payload["as_of"])
        try:
            if principal is not None:
                report = service.compute_report_as(
                    principal,
                    payload["kind"],
                    window_start=window_start,
                    window_end=window_end,
                    as_of=as_of,
                )
            else:
                report = service.compute_report(
                    payload["kind"],
                    window_start=window_start,
                    window_end=window_end,
                    as_of=as_of,
                )
        except AccessDeniedError as exc:
            return _denied(exc, {"report": None})
        except ReportError as exc:
            return {"error": str(exc)}
        return {"report": report.model_dump(mode="json")}
    if action == "get_report":
        if principal is not None:
            try:
                found = service.get_report_as(principal, payload["report_id"])
            except AccessDeniedError as exc:
                return _denied(exc, {"report": None})
        else:
            found = service.get_report(payload["report_id"])
        return {"report": found.model_dump(mode="json") if found else None}
    if action == "list_reports":
        if principal is not None:
            try:
                found = service.list_reports_as(principal, kind=payload.get("kind"))
            except AccessDeniedError as exc:
                return _denied(exc, {"reports": []})
        else:
            found = service.list_reports(kind=payload.get("kind"))
        return {"reports": [r.model_dump(mode="json") for r in found]}
    if action == "verify_report":
        if principal is not None:
            try:
                found = service.get_report_as(principal, payload["report_id"])
            except AccessDeniedError as exc:
                return _denied(exc, {"report": None})
        else:
            found = service.get_report(payload["report_id"])
        if found is None:
            return {"report": None}
        problems = service.verify_management_report(found)
        return {
            "report_id": found.report_id,
            "verified": not problems,
            "problems": problems,
        }
    if action == "get_exception_pack":
        return {"exception_pack": service.get_exception_pack().model_dump(mode="json")}
    if action == "detect_exceptions":
        as_of = date.fromisoformat(payload["as_of"])
        try:
            if principal is not None:
                found = service.detect_exceptions_as(principal, payload["case_id"], as_of=as_of)
            else:
                found = service.detect_carrier_exceptions(payload["case_id"], as_of=as_of)
        except AccessDeniedError as exc:
            return _denied(exc, {"exceptions": []})
        except LookupError as exc:
            return {"error": str(exc)}
        return {"exceptions": [service.exception_view(e) for e in found]}
    if action == "list_exceptions":
        if principal is not None:
            try:
                return {"exceptions": service.list_exceptions_as(principal, payload["case_id"])}
            except AccessDeniedError as exc:
                return _denied(exc, {"exceptions": []})
        if service.get_case(payload["case_id"]) is None:
            return {"exceptions": []}
        return {
            "exceptions": [
                service.exception_view(e) for e in service.list_exceptions(payload["case_id"])
            ]
        }
    if action == "get_exception_status":
        if principal is not None:
            try:
                found = service.get_exception_status_as(principal, payload["case_id"])
            except AccessDeniedError as exc:
                return _denied(exc, {"exception_status": None})
            return {"exception_status": found}
        if service.get_case(payload["case_id"]) is None:
            return {"exception_status": None}
        return {
            "exception_status": [
                service.exception_view(e) for e in service.list_exceptions(payload["case_id"])
            ]
        }
    if action == "record_exception_act":
        try:
            if principal is not None:
                act = service.record_exception_act_as(
                    principal,
                    payload["exception_id"],
                    action=payload["exception_action"],
                    acted_on=date.fromisoformat(payload["acted_on"]),
                    note=payload.get("note", ""),
                )
            else:
                act = service.record_exception_act(
                    payload["exception_id"],
                    action=payload["exception_action"],
                    actor=payload.get("actor", ""),
                    acted_on=date.fromisoformat(payload["acted_on"]),
                    note=payload.get("note", ""),
                )
        except AccessDeniedError as exc:
            return _denied(exc, {"exception_act": None})
        except (LookupError, CarrierExceptionError) as exc:
            return {"error": str(exc)}
        return {"exception_act": act.model_dump(mode="json")}
    if action == "file_carrier_claim":
        try:
            if principal is not None:
                claim = service.file_carrier_claim_as(
                    principal,
                    payload["exception_id"],
                    filed_on=date.fromisoformat(payload["filed_on"]),
                )
            else:
                claim = service.file_carrier_claim(
                    payload["exception_id"],
                    filed_by=payload.get("filed_by", ""),
                    filed_on=date.fromisoformat(payload["filed_on"]),
                )
        except AccessDeniedError as exc:
            return _denied(exc, {"claim": None})
        except (LookupError, CarrierExceptionError) as exc:
            return {"error": str(exc)}
        return {"claim": service.claim_view(claim)}
    if action == "list_carrier_claims":
        if principal is not None:
            try:
                return {"claims": service.list_claims_as(principal, payload["case_id"])}
            except AccessDeniedError as exc:
                return _denied(exc, {"claims": None})
        if service.get_case(payload["case_id"]) is None:
            return {"claims": []}
        return {
            "claims": [service.claim_view(c) for c in service.list_claims(payload["case_id"])]
        }
    if action == "record_claim_act":
        settled = (
            Decimal(str(payload["settled_amount"]))
            if payload.get("settled_amount") is not None
            else None
        )
        try:
            if principal is not None:
                act = service.record_claim_act_as(
                    principal,
                    payload["claim_id"],
                    action=payload["claim_action"],
                    acted_on=date.fromisoformat(payload["acted_on"]),
                    settled_amount=settled,
                    reason_code=payload.get("reason_code"),
                    note=payload.get("note", ""),
                )
            else:
                act = service.record_claim_act(
                    payload["claim_id"],
                    action=payload["claim_action"],
                    actor=payload.get("actor", ""),
                    acted_on=date.fromisoformat(payload["acted_on"]),
                    settled_amount=settled,
                    reason_code=payload.get("reason_code"),
                    note=payload.get("note", ""),
                )
        except AccessDeniedError as exc:
            return _denied(exc, {"claim_act": None})
        except (LookupError, CarrierExceptionError) as exc:
            return {"error": str(exc)}
        return {"claim_act": act.model_dump(mode="json")}
    if action == "verify_exception":
        if principal is not None:
            try:
                service.require_surface(
                    principal, SURFACE_EXCEPTIONS, target=payload["exception_id"]
                )
            except AccessDeniedError as exc:
                return _denied(exc, {"exception_id": payload["exception_id"], "verified": False, "problems": []})
        if service.get_exception(payload["exception_id"]) is None:
            return {"exception_id": payload["exception_id"], "verified": False, "problems": ["exception not found"]}
        problems = service.verify_exception(payload["exception_id"])
        return {
            "exception_id": payload["exception_id"],
            "verified": not problems,
            "problems": problems,
        }
    if action == "verify_carrier_claim":
        if principal is not None:
            try:
                service.require_surface(
                    principal, SURFACE_EXCEPTIONS, target=payload["claim_id"]
                )
            except AccessDeniedError as exc:
                return _denied(exc, {"claim_id": payload["claim_id"], "verified": False, "problems": []})
        if service.get_claim(payload["claim_id"]) is None:
            return {"claim_id": payload["claim_id"], "verified": False, "problems": ["claim not found"]}
        problems = service.verify_carrier_claim(payload["claim_id"])
        return {
            "claim_id": payload["claim_id"],
            "verified": not problems,
            "problems": problems,
        }
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
