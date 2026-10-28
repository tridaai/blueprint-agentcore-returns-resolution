"""R11 gates: the generalized gateway port + the policy pack, and
the PG harness hermeticity fix.

Graded: ALL tool-shaped operations flow through the gateway —
carrier tools (R6), refund execution against a prepared execution
record only, and the warehouse grading callback; the gateway
policy pack as versioned data (fail-closed loader, first-match
deny in pack order) evaluated BEFORE any handler runs; every
forbidden invocation attempted — denied by the named policy,
recorded as a denied invocation with the policy's id and the
actor, and executing nothing; the denial envelope's AgentCore
shape (a successful transport carrying JSON-RPC -32002, not an
HTTP error); the permitted flows standing in the invocation log
with result digests; parity across service, API and Runtime; and
migrate() as a safe no-op on an already-migrated schema.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.api import create_app
from returns_resolution.gateway import POLICY_DENIAL_CODE, PolicyDeniedError
from returns_resolution.gateway_policy import (
    GatewayPolicyPack,
    ToolFacts,
    evaluate_gateway_policy,
    load_gateway_policy_pack,
)
from returns_resolution.intake import FRAUD_FEED_REQUESTS, PLANTED_REQUESTS
from returns_resolution.modelport import OfflineComposer
from returns_resolution.payments import LocalPaymentsDouble
from returns_resolution.runtime_app import handle_payload
from returns_resolution.service import CaseService, PaymentsError
from returns_resolution.store import InMemoryStore, SqlStore

DAY = date(2026, 10, 27)
RECEIVED = date(2026, 10, 30)
GRADED = date(2026, 10, 31)

PG_URL = os.environ.get("RETURNS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pg_only = pytest.mark.skipif(
    not PG_URL, reason="Postgres profile runs when RETURNS_TEST_DATABASE_URL is set (R11)"
)


def _service(store=None, payments=None, pack=None) -> CaseService:
    service = CaseService(
        store or InMemoryStore(),
        OfflineComposer(),
        payments_port=payments,
        gateway_policy_pack=pack,
    )
    service.seed_orders()
    for request in PLANTED_REQUESTS:
        service.ingest(request)
    return service


def _denied_call(service, case_id, tool):
    denied = [
        c for c in service.list_tool_calls(case_id) if c.tool == tool and c.status == "denied"
    ]
    assert len(denied) == 1, f"expected exactly one denied {tool} record for {case_id}"
    return denied[0]


def _pay_args(service, case_id, *, amount=None, key="r11-probe"):
    computation = service.get_computation(case_id)
    return {
        "case_id": case_id,
        "computation_id": computation.computation_id,
        "amount": str(amount if amount is not None else computation.refund_total),
        "tender_allocations": [],
        "idempotency_key": key,
    }


# -- the pack as data -------------------------------------------------------


def test_gateway_policy_pack_loads_and_fails_closed():
    pack = load_gateway_policy_pack()
    assert pack.version == "2026.10.1"
    ids = [p.policy_id for p in pack.policies]
    assert len(ids) == len(set(ids)), "policy ids are unique"
    assert ids[0] == "GP-CUSTOMER-NO-TOOLS", "the customer block stands first"
    with pytest.raises(ValueError, match="unknown gateway policy pack version"):
        load_gateway_policy_pack("1999.01.1")


def test_first_match_deny_wins_in_pack_order():
    pack = load_gateway_policy_pack()
    # A customer invoking the money tool matches GP-CUSTOMER-NO-TOOLS
    # (stated first) — not the payment policies below it.
    denial = evaluate_gateway_policy(
        pack, tool="execute_refund", actor="customer", facts=ToolFacts(case_exists=True)
    )
    assert denial is not None and denial.policy_id == "GP-CUSTOMER-NO-TOOLS"
    assert denial.pack_version == pack.version


def test_role_cap_is_a_pack_row_evaluated_in_code():
    pack = load_gateway_policy_pack()
    facts = ToolFacts(
        case_exists=True,
        fraud_blocked=False,
        approval_satisfied=True,
        computation_executed=False,
        amount=Decimal("900.00"),
        computed_amount=Decimal("1000.00"),
    )
    denial = evaluate_gateway_policy(pack, tool="execute_refund", actor="staff", facts=facts)
    assert denial is not None and denial.policy_id == "GP-PAYMENT-ROLE-CAP"
    # The same call at $499 is permitted by every row.
    facts.amount = Decimal("499.00")
    assert evaluate_gateway_policy(pack, tool="execute_refund", actor="staff", facts=facts) is None


def test_amount_above_computed_denies_before_the_cap():
    pack = load_gateway_policy_pack()
    facts = ToolFacts(
        case_exists=True,
        fraud_blocked=False,
        approval_satisfied=True,
        computation_executed=False,
        amount=Decimal("200.00"),
        computed_amount=Decimal("100.00"),
    )
    denial = evaluate_gateway_policy(pack, tool="execute_refund", actor="staff", facts=facts)
    assert denial is not None and denial.policy_id == "GP-PAYMENT-AMOUNT"


# -- forbidden invocations: denied, recorded, nothing executes --------------


def test_customer_label_invocation_denied_and_recorded():
    service = _service()
    with pytest.raises(PolicyDeniedError) as caught:
        service.invoke_tool(
            "create_return_label", {"case_id": "CASE-2003", "issued_on": DAY},
            case_id="CASE-2003", called_on=DAY, actor="customer",
        )
    denial = caught.value.denial
    assert denial.policy_id == "GP-CUSTOMER-NO-TOOLS"
    envelope = caught.value.envelope()
    assert envelope["jsonrpc"] == "2.0"
    assert envelope["id"] == caught.value.invocation_id
    assert envelope["error"]["code"] == POLICY_DENIAL_CODE == -32002
    assert envelope["error"]["data"]["policy_id"] == "GP-CUSTOMER-NO-TOOLS"
    record = _denied_call(service, "CASE-2003", "create_return_label")
    assert record.actor == "customer"
    assert record.policy_id == "GP-CUSTOMER-NO-TOOLS"
    assert record.result_hash is None
    assert service.list_labels("CASE-2003") == [], "a denial executes nothing"


def test_auditor_cannot_book_a_pickup():
    service = _service()
    with pytest.raises(PolicyDeniedError) as caught:
        service.invoke_tool(
            "book_pickup",
            {"case_id": "CASE-2003", "scheduled_on": DAY, "label_id": None},
            case_id="CASE-2003", called_on=DAY, actor="auditor",
        )
    assert caught.value.denial.policy_id == "GP-CARRIER-WRITE-ACTORS"
    assert service.list_pickups("CASE-2003") == []


def test_warehouse_cannot_execute_a_refund():
    double = LocalPaymentsDouble()
    service = _service(payments=double)
    with pytest.raises(PolicyDeniedError) as caught:
        service.invoke_tool(
            "execute_refund", _pay_args(service, "CASE-2006"),
            case_id="CASE-2006", called_on=DAY, actor="warehouse",
        )
    assert caught.value.denial.policy_id == "GP-PAYMENT-ACTORS"
    assert double.movement_count == 0


def test_fraud_blocked_payment_denied_at_the_gateway():
    service = _service()
    service.seed_history()
    service.ingest(FRAUD_FEED_REQUESTS[0])  # CASE-3008, FRAUD_REVIEW
    with pytest.raises(PolicyDeniedError) as caught:
        service.invoke_tool(
            "execute_refund", _pay_args(service, "CASE-3008"),
            case_id="CASE-3008", called_on=DAY, actor="staff",
        )
    assert caught.value.denial.policy_id == "GP-PAYMENT-FRAUD-HOLD"


def test_unapproved_payment_denied_at_the_gateway():
    double = LocalPaymentsDouble()
    service = _service(payments=double)
    with pytest.raises(PolicyDeniedError) as caught:
        service.invoke_tool(
            "execute_refund", _pay_args(service, "CASE-2003"),
            case_id="CASE-2003", called_on=DAY, actor="staff",
        )
    assert caught.value.denial.policy_id == "GP-PAYMENT-APPROVAL"
    assert double.movement_count == 0


def test_inflated_amount_denied_at_the_gateway():
    double = LocalPaymentsDouble()
    service = _service(payments=double)
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY)
    with pytest.raises(PolicyDeniedError) as caught:
        service.invoke_tool(
            "execute_refund", _pay_args(service, "CASE-2003", amount=Decimal("999.99")),
            case_id="CASE-2003", called_on=DAY, actor="staff",
        )
    assert caught.value.denial.policy_id == "GP-PAYMENT-AMOUNT"
    assert double.movement_count == 0


def test_second_execution_under_a_fresh_key_denied_at_the_gateway():
    double = LocalPaymentsDouble()
    service = _service(payments=double)
    service.execute_refund("CASE-2006", idempotency_key="r11-once", as_of=DAY)
    assert double.movement_count == 1
    with pytest.raises(PolicyDeniedError) as caught:
        service.invoke_tool(
            "execute_refund", _pay_args(service, "CASE-2006", key="r11-twice"),
            case_id="CASE-2006", called_on=DAY, actor="staff",
        )
    assert caught.value.denial.policy_id == "GP-PAYMENT-UNEXECUTED"
    assert double.movement_count == 1, "the denial moved no money"


def test_grading_callback_belongs_to_the_warehouse():
    service = _service()
    with pytest.raises(PolicyDeniedError) as caught:
        service.invoke_tool(
            "submit_grading_report",
            {
                "case_id": "CASE-2003", "grader": "p.novak",
                "graded_condition": "used", "graded_on": GRADED,
            },
            case_id="CASE-2003", called_on=GRADED, actor="auditor",
        )
    assert caught.value.denial.policy_id == "GP-GRADING-ACTORS"
    assert service.list_grading_reports("CASE-2003") == []


def test_tightened_role_cap_denies_a_prepared_execution():
    data = load_gateway_policy_pack().model_dump()
    for policy in data["policies"]:
        if policy["policy_id"] == "GP-PAYMENT-ROLE-CAP":
            policy["caps"]["staff"] = "100.00"
    tight = GatewayPolicyPack.model_validate(data)
    double = LocalPaymentsDouble()
    service = _service(payments=double, pack=tight)
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY)
    # Direct invocation: the cap denies before the handler runs.
    with pytest.raises(PolicyDeniedError) as caught:
        service.invoke_tool(
            "execute_refund", _pay_args(service, "CASE-2003"),
            case_id="CASE-2003", called_on=DAY, actor="staff",
        )
    assert caught.value.denial.policy_id == "GP-PAYMENT-ROLE-CAP"
    # The governed flow: the service prepares the execution, the
    # gateway denies it, and the execution stands failed with no
    # receipt and no movement.
    with pytest.raises(PaymentsError, match="gateway policy denied"):
        service.execute_refund("CASE-2003", idempotency_key="r11-capped", as_of=DAY)
    assert double.movement_count == 0
    assert service.get_receipt("CASE-2003") is None
    execution = service.get_execution_by_key("r11-capped")
    assert execution is not None and execution.status == "failed"


# -- the handler's own line: no prepared execution, no port call -------------


def test_bare_tool_call_without_a_prepared_execution_moves_nothing():
    double = LocalPaymentsDouble()
    service = _service(payments=double)
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY)
    # Policy permits this call (staff, approved, unexecuted, amount
    # exact) — the handler still refuses it: money moves only
    # against an execution the governed flow prepared.
    with pytest.raises(PaymentsError, match="no prepared execution"):
        service.invoke_tool(
            "execute_refund", _pay_args(service, "CASE-2003"),
            case_id="CASE-2003", called_on=DAY, actor="staff",
        )
    assert double.movement_count == 0
    calls = [
        c for c in service.list_tool_calls("CASE-2003") if c.tool == "execute_refund"
    ]
    assert len(calls) == 1 and calls[0].status == "error"


# -- permitted flows stand in the invocation log ------------------------------


def test_permitted_flows_are_recorded_with_result_digests():
    double = LocalPaymentsDouble()
    service = _service(payments=double)
    label = service.invoke_tool(
        "create_return_label", {"case_id": "CASE-2003", "issued_on": DAY},
        case_id="CASE-2003", called_on=DAY,
    )
    receipt = service.execute_refund("CASE-2006", idempotency_key="r11-log", as_of=DAY)
    service.record_warehouse_receipt("CASE-2003", received_by="w.chen", received_on=RECEIVED)
    report = service.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="used", graded_on=GRADED
    )
    calls = {c.tool: c for c in service.list_tool_calls("CASE-2003")}
    label_call = calls["create_return_label"]
    assert label_call.status == "ok" and label_call.actor == "staff"
    assert label_call.result_ref == label.label_id
    assert label_call.result_hash and len(label_call.result_hash) == 64
    grading_call = calls["submit_grading_report"]
    assert grading_call.status == "ok" and grading_call.actor == "warehouse"
    assert grading_call.result_ref == report.report_id
    exec_call = next(
        c for c in service.list_tool_calls("CASE-2006") if c.tool == "execute_refund"
    )
    assert exec_call.status == "ok"
    assert exec_call.result_ref == receipt.provider_ref
    assert double.movement_count == 1


def test_envelope_carries_results_and_denials_without_raising():
    service = _service()
    ok = service.invoke_tool_envelope(
        "create_return_label", {"case_id": "CASE-2003", "issued_on": DAY},
        case_id="CASE-2003", called_on=DAY, actor="staff",
    )
    assert ok.outcome == "result"
    assert ok.result["label_id"] == service.get_active_label("CASE-2003").label_id
    denied = service.invoke_tool_envelope(
        "execute_refund", _pay_args(service, "CASE-2003"),
        case_id="CASE-2003", called_on=DAY, actor="customer",
    )
    assert denied.outcome == "denied"
    assert denied.error["code"] == -32002
    assert denied.error["data"]["policy_id"] == "GP-CUSTOMER-NO-TOOLS"


# -- surfaces -------------------------------------------------------------------


def test_api_policy_pack_and_denial_envelope():
    client = TestClient(create_app(_service()), raise_server_exceptions=False)
    pack = client.get("/gateway/policy-pack")
    assert pack.status_code == 200
    assert pack.json()["version"] == "2026.10.1"
    assert pack.json()["policies"][0]["policy_id"] == "GP-CUSTOMER-NO-TOOLS"
    denied = client.post(
        "/tools/create_return_label/invoke",
        json={
            "case_id": "CASE-2003",
            "arguments": {"case_id": "CASE-2003", "issued_on": "2026-10-27"},
            "actor": "customer",
            "called_on": "2026-10-27",
        },
    )
    assert denied.status_code == 200, "a policy denial is not an HTTP error"
    body = denied.json()
    assert body["error"]["code"] == -32002
    assert body["error"]["data"]["policy_id"] == "GP-CUSTOMER-NO-TOOLS"
    allowed = client.post(
        "/tools/create_return_label/invoke",
        json={
            "case_id": "CASE-2003",
            "arguments": {"case_id": "CASE-2003", "issued_on": "2026-10-27"},
            "actor": "staff",
            "called_on": "2026-10-27",
        },
    )
    assert allowed.status_code == 200 and allowed.json()["label_id"]
    calls = client.get("/cases/CASE-2003/tool-calls").json()
    statuses = {(c["tool"], c["status"]) for c in calls}
    assert ("create_return_label", "denied") in statuses
    assert ("create_return_label", "ok") in statuses


def test_runtime_policy_pack_and_denial_envelope():
    service = _service()
    out = handle_payload(service, {"action": "get_gateway_policy_pack"})
    assert out["gateway_policy_pack"]["version"] == "2026.10.1"
    out = handle_payload(
        service,
        {
            "action": "invoke_tool",
            "tool": "create_return_label",
            "case_id": "CASE-2003",
            "arguments": {"case_id": "CASE-2003", "issued_on": "2026-10-27"},
            "called_on": "2026-10-27",
            "actor": "customer",
        },
    )
    assert "gateway_denial" in out
    assert out["gateway_denial"]["error"]["code"] == -32002
    assert out["gateway_denial"]["error"]["data"]["policy_id"] == "GP-CUSTOMER-NO-TOOLS"


# -- hermeticity: migrate() is a safe no-op once applied ------------------------


def test_sqlite_migrate_is_idempotent_across_stores(tmp_path):
    path = tmp_path / "twice.db"
    first = SqlStore(sqlite_path=path)
    assert first.migrate() == [f"000{i}" for i in range(1, 10)] + ["0010", "0011", "0012", "0013", "0014"]
    assert first.migrate() == []
    second = SqlStore(sqlite_path=path)
    assert second.migrate() == [], "an already-migrated schema applies nothing, raises nothing"


@pg_only
def test_postgres_migrate_is_idempotent_across_stores(tmp_path):
    import psycopg

    schema = f"t_r11mig_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in PG_URL else "?"
    url = f"{PG_URL}{sep}options=-csearch_path%3D{schema}"
    first = SqlStore(database_url=url)
    assert first.migrate() == [f"000{i}" for i in range(1, 10)] + ["0010", "0011", "0012", "0013", "0014"]
    second = SqlStore(database_url=url)
    assert second.migrate() == [], "no unique violation, nothing re-applied"
    assert first.migrate() == []
    digest = hashlib.sha256(schema.encode()).hexdigest()[:8]
    assert digest  # the schema name itself is run-unique by construction
