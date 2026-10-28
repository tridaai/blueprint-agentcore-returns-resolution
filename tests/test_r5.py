"""R5 gates: approval gates + the idempotent payments port.

The planted ladder, against the pack caps (auto $50 / dual $150):
CASE-2006 refunds $29.05 (auto — no acts needed), CASE-2003 refunds
$130.50 (one named approver), CASE-2001 refunds $189.00 (a second,
different approver). Graded: tier boundaries on both sides of both
caps; the approval act's refusals (unnamed, self-approval by the
customer, stale computation, same approver twice, fraud-blocked,
not-final, nothing-due); execution preconditions each naming their
gap; idempotency (same key replays the receipt and moves money
once, a different key on an executed computation is refused as
duplicate_refund); the failure path (injected provider failure
leaves no receipt, records a failed execution, and the case stays
executable); tender allocations carried onto receipts; persistence
on the SQL store; and parity across API and Runtime dispatch.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.api import create_app
from returns_resolution.intake import FRAUD_FEED_REQUESTS, PLANTED_REQUESTS
from returns_resolution.modelport import OfflineComposer
from returns_resolution.models import RefundApproval
from returns_resolution.payments import (
    DOUBLE_PROVIDER,
    LocalPaymentsDouble,
    PaymentsProviderError,
    approval_tier,
)
from returns_resolution.policy import load_policy_pack
from returns_resolution.refunds import compute_refund
from returns_resolution.runtime_app import handle_payload
from returns_resolution.seed import CATALOGUE, ORDERS
from returns_resolution.service import (
    ApprovalError,
    CaseService,
    DuplicateRefundError,
    FraudHoldError,
    PaymentsError,
)
from returns_resolution.store import InMemoryStore, SqlStore

DAY = date(2026, 10, 27)
PACK = load_policy_pack()


def _service(store=None, payments=None) -> CaseService:
    service = CaseService(store or InMemoryStore(), OfflineComposer(), payments_port=payments)
    service.seed_orders()
    for request in PLANTED_REQUESTS:
        service.ingest(request)
    return service


def _approve(service, case_id, approver="d.okafor", role="returns_lead", **kw):
    return service.record_approval(case_id, approver=approver, role=role, as_of=DAY, **kw)


# --- tiers -----------------------------------------------------------------

def test_tier_boundaries_both_sides_of_both_caps():
    assert approval_tier(PACK, Decimal("50.00")) == "auto"
    assert approval_tier(PACK, Decimal("50.01")) == "single"
    assert approval_tier(PACK, Decimal("150.00")) == "single"
    assert approval_tier(PACK, Decimal("150.01")) == "dual"


def test_planted_cases_land_on_their_tiers():
    service = _service()
    assert service.approval_status("CASE-2006")["tier"] == "auto"
    assert service.approval_status("CASE-2003")["tier"] == "single"
    assert service.approval_status("CASE-2001")["tier"] == "dual"
    assert service.approval_status("CASE-2006")["satisfied"] is True
    assert service.approval_status("CASE-2003")["satisfied"] is False


# --- the approval act ---------------------------------------------------------

def test_approval_act_records_the_pinned_computation_and_total():
    service = _service()
    approval = _approve(service, "CASE-2003")
    computation = service.get_computation("CASE-2003")
    assert approval.computation_id == computation.computation_id
    assert approval.amount == Decimal("130.50")
    assert approval.tier == "single"
    assert approval.policy_pack_version == PACK.version
    assert approval.approver == "d.okafor" and approval.role == "returns_lead"
    assert approval.as_of == DAY
    assert service.approval_status("CASE-2003")["satisfied"] is True


def test_approval_refusals_name_their_reasons():
    service = _service()
    with pytest.raises(ApprovalError, match="named approver"):
        service.record_approval("CASE-2003", approver="  ", role="returns_lead", as_of=DAY)
    with pytest.raises(ApprovalError, match="role"):
        service.record_approval("CASE-2003", approver="d.okafor", role="", as_of=DAY)
    # The customer who submitted the return cannot approve it.
    with pytest.raises(ApprovalError, match="cannot approve"):
        service.record_approval("CASE-2003", approver="CUST-CID", role="customer", as_of=DAY)
    # A denied case has nothing to approve.
    with pytest.raises(ApprovalError, match="nothing to approve"):
        _approve(service, "CASE-2004")


def test_stale_computation_approval_is_refused():
    service = _service()
    case = service.get_case("CASE-2003")
    # A computation over a corrected order (qty 2) — a real artefact
    # with a different id, exactly what a superseding computation
    # would look like. It is not the case's current computation.
    corrected = ORDERS["ORD-1003"].model_copy(deep=True)
    corrected.lines[0].qty = 2
    request = case.request.model_copy(deep=True)
    request.qty = 2
    other = compute_refund(PACK, corrected, CATALOGUE["BOOT-TRL"], request, case.decision)
    assert other.computation_id != case.computation.computation_id
    with pytest.raises(ApprovalError, match="stale computation"):
        _approve(service, "CASE-2003", computation_id=other.computation_id)
    # Pinning the current computation explicitly is accepted.
    _approve(service, "CASE-2003", computation_id=case.computation.computation_id)


def test_approval_pinned_to_a_superseded_computation_does_not_count():
    service = _service()
    case = service.get_case("CASE-2001")
    forged = RefundApproval(
        approval_id="RA-forged00000000",
        case_id="CASE-2001",
        computation_id="RC-not-the-current-one",
        approver="m.osei",
        role="finance_manager",
        as_of=DAY,
        amount=Decimal("189.00"),
        tier="dual",
        policy_pack_version=PACK.version,
    )
    service._store.put_approval(forged.model_dump(mode="json"))
    _approve(service, "CASE-2001")  # one genuine approval
    status = service.approval_status("CASE-2001")
    assert status["approvers"] == ["d.okafor"]  # the forged pin is not counted
    assert status["satisfied"] is False
    with pytest.raises(ApprovalError, match="second, different approver"):
        service.execute_refund("CASE-2001", idempotency_key="k-forged", as_of=DAY)


def test_same_approver_cannot_supply_the_second_approval():
    service = _service()
    _approve(service, "CASE-2001")
    with pytest.raises(ApprovalError, match="different approver"):
        _approve(service, "CASE-2001", approver="d.okafor", role="finance_manager")
    _approve(service, "CASE-2001", approver="m.osei", role="finance_manager")
    assert service.approval_status("CASE-2001")["satisfied"] is True


def test_approval_on_a_not_final_computation_is_refused():
    service = _service()
    from returns_resolution.models import ReturnRequest

    service.ingest(
        ReturnRequest(
            request_id="REQ-2098", order_id="ORD-1007", sku="STOVE-PKT", qty=1,
            reason_code="changed_mind", condition_declared="waterlogged",
            requested_on=date(2026, 10, 25),
        )
    )
    assert service.get_computation("CASE-2098").is_final is False
    with pytest.raises(ApprovalError, match="not final"):
        _approve(service, "CASE-2098")


def test_fraud_blocked_case_refuses_approval_and_execution():
    service = _service()
    service.seed_history()
    service.ingest(FRAUD_FEED_REQUESTS[0])  # CASE-3008 -> FRAUD_REVIEW
    with pytest.raises(FraudHoldError):
        _approve(service, "CASE-3008", approver="r.alvarez", role="fraud_analyst")
    with pytest.raises(FraudHoldError):
        service.execute_refund("CASE-3008", idempotency_key="k-fraud", as_of=DAY)
    # After the named review act clears it, the ordinary path works.
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez", outcome="cleared_with_reason",
        reason_code="legitimate_pattern_explained",
    )
    _approve(service, "CASE-3008")
    receipt = service.execute_refund("CASE-3008", idempotency_key="k-fraud", as_of=DAY)
    assert receipt.amount == Decimal("116.00")


# --- execution: the ladder ---------------------------------------------------

def test_auto_tier_executes_with_no_approvals():
    double = LocalPaymentsDouble()
    service = _service(payments=double)
    receipt = service.execute_refund("CASE-2006", idempotency_key="k-2006", as_of=DAY)
    assert receipt.amount == Decimal("29.05")
    assert receipt.provider == DOUBLE_PROVIDER
    assert receipt.status == "executed" and receipt.executed_on == DAY
    assert double.movement_count == 1


def test_single_tier_needs_one_named_approver():
    service = _service()
    with pytest.raises(ApprovalError, match="none recorded"):
        service.execute_refund("CASE-2003", idempotency_key="k-2003", as_of=DAY)
    _approve(service, "CASE-2003")
    receipt = service.execute_refund("CASE-2003", idempotency_key="k-2003", as_of=DAY)
    assert receipt.amount == Decimal("130.50")


def test_dual_tier_needs_two_distinct_approvers():
    service = _service()
    _approve(service, "CASE-2001")
    with pytest.raises(ApprovalError, match="second, different approver"):
        service.execute_refund("CASE-2001", idempotency_key="k-2001", as_of=DAY)
    _approve(service, "CASE-2001", approver="m.osei", role="finance_manager")
    receipt = service.execute_refund("CASE-2001", idempotency_key="k-2001", as_of=DAY)
    assert receipt.amount == Decimal("189.00")


def test_execution_preconditions_name_their_gaps():
    service = _service()
    with pytest.raises(PaymentsError, match="idempotency key is mandatory"):
        service.execute_refund("CASE-2006", idempotency_key=" ", as_of=DAY)
    # Denied case: nothing due (and no disposition stands behind it).
    with pytest.raises(PaymentsError, match="nothing to execute"):
        service.execute_refund("CASE-2004", idempotency_key="k-2004", as_of=DAY)
    # Not-final computation cannot execute.
    from returns_resolution.models import ReturnRequest

    service.ingest(
        ReturnRequest(
            request_id="REQ-2098", order_id="ORD-1007", sku="STOVE-PKT", qty=1,
            reason_code="changed_mind", condition_declared="waterlogged",
            requested_on=date(2026, 10, 25),
        )
    )
    with pytest.raises(PaymentsError, match="not final"):
        service.execute_refund("CASE-2098", idempotency_key="k-2098", as_of=DAY)


def test_receipt_carries_the_computations_tender_allocation():
    service = _service()
    service.seed_history()
    service.ingest(FRAUD_FEED_REQUESTS[0])  # CASE-3008: ORD-3008 paid by card_amex_1005
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez", outcome="cleared_with_reason",
        reason_code="legitimate_pattern_explained",
    )
    _approve(service, "CASE-3008")
    receipt = service.execute_refund("CASE-3008", idempotency_key="k-3008", as_of=DAY)
    computation = service.get_computation("CASE-3008")
    assert receipt.amount == computation.refund_total == Decimal("116.00")
    assert receipt.tender_allocations == computation.tender_allocations
    assert [(a.method, a.amount) for a in receipt.tender_allocations] == [
        ("card_amex_1005", Decimal("116.00"))
    ]
    # A case whose order carries no tender breakdown executes with an
    # honestly empty allocation — the computation named the missing
    # input rather than inventing a split, and the receipt inherits it.
    _approve(service, "CASE-2001", approver="d.okafor")
    _approve(service, "CASE-2001", approver="m.osei", role="finance_manager")
    receipt = service.execute_refund("CASE-2001", idempotency_key="k-2001-t", as_of=DAY)
    assert receipt.tender_allocations == []
    named = {c.component for c in service.get_computation("CASE-2001").not_computable}
    assert "tender_allocation" in named


# --- idempotency --------------------------------------------------------------

def test_same_key_replays_the_receipt_and_moves_money_once():
    double = LocalPaymentsDouble()
    service = _service(payments=double)
    first = service.execute_refund("CASE-2006", idempotency_key="k-once", as_of=DAY)
    for _ in range(2):
        again = service.execute_refund("CASE-2006", idempotency_key="k-once", as_of=DAY)
        assert again.receipt_id == first.receipt_id
        assert again.provider_ref == first.provider_ref
    assert double.movement_count == 1
    assert len(service.list_receipts()) == 1
    execution = service.get_execution_by_key("k-once")
    assert [t.state for t in execution.transitions] == ["requested", "executing", "executed"]


def test_different_key_on_an_executed_computation_is_duplicate_refund():
    service = _service()
    service.execute_refund("CASE-2006", idempotency_key="k-first", as_of=DAY)
    with pytest.raises(DuplicateRefundError, match="duplicate_refund"):
        service.execute_refund("CASE-2006", idempotency_key="k-second", as_of=DAY)
    assert len(service.list_receipts()) == 1


def test_a_key_names_one_execution():
    service = _service()
    service.execute_refund("CASE-2006", idempotency_key="k-shared", as_of=DAY)
    _approve(service, "CASE-2003")
    with pytest.raises(PaymentsError, match="already used for case"):
        service.execute_refund("CASE-2003", idempotency_key="k-shared", as_of=DAY)


def test_the_double_itself_is_idempotent_and_key_mandatory():
    double = LocalPaymentsDouble()
    kwargs = dict(
        case_id="CASE-X", computation_id="RC-X", amount=Decimal("10.00"),
        tender_allocations=[], idempotency_key="k-double",
    )
    first = double.execute_refund(**kwargs)
    assert double.execute_refund(**kwargs).provider_ref == first.provider_ref
    assert double.movement_count == 1
    with pytest.raises(PaymentsProviderError, match="mandatory"):
        double.execute_refund(**{**kwargs, "idempotency_key": ""})


def test_approval_after_execution_is_refused():
    service = _service()
    service.execute_refund("CASE-2006", idempotency_key="k-done", as_of=DAY)
    with pytest.raises(ApprovalError, match="already been executed"):
        _approve(service, "CASE-2006")


# --- failure handling -----------------------------------------------------------

def test_provider_failure_leaves_no_receipt_and_the_case_stays_executable():
    double = LocalPaymentsDouble(fail_times={"k-flaky": 1})
    service = _service(payments=double)
    with pytest.raises(PaymentsError, match="provider failed"):
        service.execute_refund("CASE-2006", idempotency_key="k-flaky", as_of=DAY)
    # No partial state: no receipt anywhere, the movement never happened.
    assert service.get_receipt("CASE-2006") is None
    assert service.list_receipts() == []
    assert double.movement_count == 0
    execution = service.get_execution_by_key("k-flaky")
    assert execution.status == "failed"
    assert execution.receipt_id is None
    assert execution.failure_reason
    assert [t.state for t in execution.transitions] == ["requested", "executing", "failed"]
    # Retry with the same key continues the same execution record.
    receipt = service.execute_refund("CASE-2006", idempotency_key="k-flaky", as_of=DAY)
    assert receipt.amount == Decimal("29.05")
    assert double.movement_count == 1
    execution = service.get_execution_by_key("k-flaky")
    assert execution.status == "executed" and execution.receipt_id == receipt.receipt_id
    assert [t.state for t in execution.transitions] == [
        "requested", "executing", "failed", "executing", "executed",
    ]


def test_failed_execution_can_be_retried_under_a_new_key():
    double = LocalPaymentsDouble(fail_times={"k-bad": 1})
    service = _service(payments=double)
    with pytest.raises(PaymentsError):
        service.execute_refund("CASE-2006", idempotency_key="k-bad", as_of=DAY)
    receipt = service.execute_refund("CASE-2006", idempotency_key="k-good", as_of=DAY)
    assert receipt.idempotency_key == "k-good"
    assert len(service.list_executions("CASE-2006")) == 2


# --- persistence (SQL store) -----------------------------------------------------

def test_sql_store_round_trips_approvals_executions_and_receipts(tmp_path):
    store = SqlStore(sqlite_path=tmp_path / "r5.db")
    applied = store.migrate()
    assert "0005" in applied
    assert store.migrate() == []
    service = _service(store=store)
    _approve(service, "CASE-2003")
    receipt = service.execute_refund("CASE-2003", idempotency_key="k-sql", as_of=DAY)

    reopened_store = SqlStore(sqlite_path=tmp_path / "r5.db")
    assert reopened_store.migrate() == []
    reopened = CaseService(reopened_store, OfflineComposer())
    assert [a.approver for a in reopened.list_approvals("CASE-2003")] == ["d.okafor"]
    assert reopened.get_receipt("CASE-2003").receipt_id == receipt.receipt_id
    assert reopened.approval_status("CASE-2003")["satisfied"] is True
    execution = reopened.get_execution_by_key("k-sql")
    assert execution.status == "executed" and execution.receipt_id == receipt.receipt_id


# --- surfaces ---------------------------------------------------------------------

def test_api_surfaces_and_refusal_codes():
    service = _service()
    client = TestClient(create_app(service))
    # Unapproved execution -> 409 naming the gate.
    resp = client.post(
        "/cases/CASE-2003/refund-executions",
        json={"idempotency_key": "k-api", "as_of": "2026-10-27"},
    )
    assert resp.status_code == 409 and "none recorded" in resp.json()["detail"]
    resp = client.post(
        "/cases/CASE-2003/approvals",
        json={"approver": "d.okafor", "role": "returns_lead", "as_of": "2026-10-27"},
    )
    assert resp.status_code == 201
    assert resp.json()["amount"] == "130.50" and resp.json()["tier"] == "single"
    # Unnamed approver -> 422 at the request model.
    resp = client.post(
        "/cases/CASE-2003/approvals",
        json={"approver": " ", "role": "returns_lead", "as_of": "2026-10-27"},
    )
    assert resp.status_code == 422
    status = client.get("/cases/CASE-2003/approval-status").json()
    assert status["satisfied"] is True and status["approvers"] == ["d.okafor"]
    resp = client.post(
        "/cases/CASE-2003/refund-executions",
        json={"idempotency_key": "k-api", "as_of": "2026-10-27"},
    )
    assert resp.status_code == 201
    receipt = resp.json()
    assert receipt["amount"] == "130.50" and receipt["provider"] == DOUBLE_PROVIDER
    assert client.get("/cases/CASE-2003/receipt").json()["receipt_id"] == receipt["receipt_id"]
    assert client.get("/receipts").json()[0]["receipt_id"] == receipt["receipt_id"]
    executions = client.get("/cases/CASE-2003/executions").json()
    assert executions[0]["status"] == "executed"
    # A second key on the executed computation -> 409 duplicate_refund.
    resp = client.post(
        "/cases/CASE-2003/refund-executions",
        json={"idempotency_key": "k-api-2", "as_of": "2026-10-27"},
    )
    assert resp.status_code == 409 and "duplicate_refund" in resp.json()["detail"]


def test_runtime_dispatch_parity():
    service = _service()
    out = handle_payload(
        service,
        {"action": "execute_refund", "case_id": "CASE-2003",
         "idempotency_key": "k-rt", "as_of": "2026-10-27"},
    )
    assert "error" in out and "none recorded" in out["error"]
    out = handle_payload(
        service,
        {"action": "record_approval", "case_id": "CASE-2003", "approver": "d.okafor",
         "role": "returns_lead", "as_of": "2026-10-27"},
    )
    assert out["approval"]["tier"] == "single"
    out = handle_payload(
        service,
        {"action": "execute_refund", "case_id": "CASE-2003",
         "idempotency_key": "k-rt", "as_of": "2026-10-27"},
    )
    assert out["receipt"]["amount"] == "130.50"
    out = handle_payload(service, {"action": "get_receipt", "case_id": "CASE-2003"})
    assert out["receipt"]["receipt_id"]
    out = handle_payload(service, {"action": "approval_status", "case_id": "CASE-2003"})
    assert out["approval_status"]["satisfied"] is True
    out = handle_payload(service, {"action": "list_approvals", "case_id": "CASE-2003"})
    assert [a["approver"] for a in out["approvals"]] == ["d.okafor"]
    out = handle_payload(service, {"action": "list_executions", "case_id": "CASE-2003"})
    assert out["executions"][0]["status"] == "executed"
    out = handle_payload(service, {"action": "list_receipts"})
    assert len(out["receipts"]) == 1
