"""R14 gates: the trap suite — adversarial fixtures across every law.

Every trap in this file was derived by hand BEFORE the code path
ran: the expected outcome is written in the trap's comment (and in
docs/trap-suite.md) from the packs and the ledger, and the test
then holds the system to it. Two traps exposed real defects when
first probed; both were fixed in this round, in their own commits,
and the traps stand here as the regressions:

- T-MS4: a "20% discount" promise in a message note verified clean
  (the percent happened to be a computed restocking rate and no
  check looked at the promise). The disclosure pack now names
  promotion vocabulary as forbidden content.
- T-M5/T-P4: a gateway execute_refund naming a SUPERSEDED but
  approved computation passed the policy layer and died at the
  handler, while GP-PAYMENT-APPROVAL's own reason claims it denies
  computations that are "not current". The gateway's approval fact
  now requires currency, and the call is denied at the gateway.

Trap families: money (T-M), fraud (T-F), grading (T-G), messages
(T-MS), gateway policy (T-P), memory (T-MEM).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from returns_resolution.fraud import load_scoring_pack
from returns_resolution.gateway import PolicyDeniedError
from returns_resolution.intake import FRAUD_FEED_REQUESTS, PLANTED_REQUESTS
from returns_resolution.messaging import LocalMessagingDouble
from returns_resolution.modelport import OfflineComposer
from returns_resolution.models import Order, OrderLine, ReturnCase, ReturnRequest, TenderPortion
from returns_resolution.payments import LocalPaymentsDouble, approval_tier
from returns_resolution.policy import load_policy_pack
from returns_resolution.seed import CATALOGUE
from returns_resolution.service import (
    ApprovalError,
    CaseService,
    DuplicateRefundError,
    FraudHoldError,
    FraudReviewError,
    GradingError,
    MessagingError,
    PaymentsError,
)
from returns_resolution.store import InMemoryStore

DAY = date(2026, 10, 27)
RECEIVED = date(2026, 10, 30)  # label issued DAY -> carrier received DAY+3
GRADED = date(2026, 10, 31)
PACK = load_policy_pack()


def _service(store=None, payments=None, messaging=None, **kw) -> CaseService:
    service = CaseService(
        store or InMemoryStore(),
        OfflineComposer(),
        payments_port=payments,
        messaging_port=messaging,
        **kw,
    )
    service.seed_orders()
    return service


def _planted(service: CaseService) -> CaseService:
    for request in PLANTED_REQUESTS:
        service.ingest(request)
    return service


def _label(service: CaseService, case_id: str, on: date = DAY):
    return service.invoke_tool(
        "create_return_label", {"case_id": case_id, "issued_on": on},
        case_id=case_id, called_on=on,
    )


def _grade_boots_2003(service: CaseService, *, execute_first: bool = False):
    """The CASE-2003 walk to a graded re-pricing: declared like_new
    $130.50; graded used -> $116.00 (delta -$14.50)."""
    _label(service, "CASE-2003")
    if execute_first:
        service.record_approval(
            "CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY
        )
        service.execute_refund("CASE-2003", idempotency_key="trap-2003-orig", as_of=DAY)
    service.record_warehouse_receipt("CASE-2003", received_by="w.chen", received_on=RECEIVED)
    return service.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="used", graded_on=GRADED
    )


def _priced_case(
    service: CaseService,
    *,
    rid: str,
    oid: str,
    customer: str,
    price: str,
    sku: str = "BOOT-TRL",
    condition: str = "new_with_tags",
    delivered: date = date(2026, 10, 15),
    requested: date = date(2026, 10, 25),
):
    """A one-line order at an exact price, ingested. Footwear and
    apparel are free-return categories and new_with_tags carries a
    0% restocking rate, so the refund total is the price itself —
    the hand-derived figure every cap trap below is stated in."""
    product = CATALOGUE[sku]
    service._store.put_order(
        Order(
            order_id=oid, customer_id=customer,
            placed_on=delivered - timedelta(days=3), delivered_on=delivered,
            lines=[OrderLine(sku=sku, qty=1, unit_price=Decimal(price))],
            shipping_paid=Decimal("0.00"),
        ).model_dump(mode="json")
    )
    assert product is not None
    return service.ingest(
        ReturnRequest(
            request_id=rid, order_id=oid, sku=sku, qty=1,
            reason_code="changed_mind", condition_declared=condition,
            requested_on=requested,
        )
    )


# --- T-M1..T-M4: the approval caps, on the cent ------------------------------
# Hand-derived from the pack: at or below $50.00 the tier is auto;
# at or below $150.00 single; above $150.00 dual. "At the cap" and
# "one cent over" are different tiers on both caps.

def test_trap_refund_exactly_at_auto_cap_executes_with_no_approval():
    payments = LocalPaymentsDouble()
    service = _service(payments=payments)
    case = _priced_case(service, rid="REQ-T01", oid="ORD-T01", customer="CUST-T01", price="50.00")
    status = service.approval_status(case.case_id)
    assert status["tier"] == "auto" and status["satisfied"] is True
    receipt = service.execute_refund(case.case_id, idempotency_key="trap-t01", as_of=DAY)
    assert receipt.amount == Decimal("50.00")
    assert payments.movement_count == 1


def test_trap_refund_one_cent_over_auto_cap_needs_one_approver():
    service = _service()
    case = _priced_case(service, rid="REQ-T02", oid="ORD-T02", customer="CUST-T02", price="50.01")
    assert service.approval_status(case.case_id)["tier"] == "single"
    with pytest.raises(ApprovalError):
        service.execute_refund(case.case_id, idempotency_key="trap-t02", as_of=DAY)
    service.record_approval(case.case_id, approver="d.okafor", role="returns_lead", as_of=DAY)
    receipt = service.execute_refund(case.case_id, idempotency_key="trap-t02", as_of=DAY)
    assert receipt.amount == Decimal("50.01")


def test_trap_refund_exactly_at_dual_cap_is_still_single():
    service = _service()
    case = _priced_case(service, rid="REQ-T03", oid="ORD-T03", customer="CUST-T03", price="150.00")
    assert service.approval_status(case.case_id)["tier"] == "single"
    service.record_approval(case.case_id, approver="d.okafor", role="returns_lead", as_of=DAY)
    receipt = service.execute_refund(case.case_id, idempotency_key="trap-t03", as_of=DAY)
    assert receipt.amount == Decimal("150.00")


def test_trap_refund_one_cent_over_dual_cap_needs_two_different_approvers():
    service = _service()
    case = _priced_case(service, rid="REQ-T04", oid="ORD-T04", customer="CUST-T04", price="150.01")
    assert service.approval_status(case.case_id)["tier"] == "dual"
    service.record_approval(case.case_id, approver="d.okafor", role="returns_lead", as_of=DAY)
    with pytest.raises(ApprovalError):
        service.execute_refund(case.case_id, idempotency_key="trap-t04", as_of=DAY)
    # The dual tier's second act must be a DIFFERENT person: the
    # same approver's second act is refused at record time.
    with pytest.raises(ApprovalError, match="different approver"):
        service.record_approval(case.case_id, approver="d.okafor", role="returns_lead", as_of=DAY)
    service.record_approval(case.case_id, approver="m.haddad", role="returns_lead", as_of=DAY)
    receipt = service.execute_refund(case.case_id, idempotency_key="trap-t04", as_of=DAY)
    assert receipt.amount == Decimal("150.01")


# --- T-M5 / T-P4: execution of a superseded computation ----------------------
# Computation A ($130.50) is approved; grading supersedes it with B
# ($116.00). Hand-derived: A can never execute — an approval pinned
# to it is refused as stale, the service gate counts only approvals
# on B, and the gateway DENIES a direct execute_refund naming A
# under GP-PAYMENT-APPROVAL (the R14 fix: the policy's approval
# fact requires the named computation to be the case's current
# one, exactly as the policy's reason has always stated).

def test_trap_stale_computation_cannot_be_approved_or_executed():
    payments = LocalPaymentsDouble()
    service = _planted(_service(payments=payments))
    comp_a = service.get_computation("CASE-2003")
    assert comp_a.refund_total == Decimal("130.50")
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY)
    _grade_boots_2003(service)
    comp_b = service.get_computation("CASE-2003")
    assert comp_b.refund_total == Decimal("116.00")
    assert comp_b.supersedes == comp_a.computation_id

    with pytest.raises(ApprovalError, match="stale computation"):
        service.record_approval(
            "CASE-2003", approver="m.haddad", role="returns_lead", as_of=GRADED,
            computation_id=comp_a.computation_id,
        )
    status = service.approval_status("CASE-2003")
    assert status["computation_id"] == comp_b.computation_id
    assert status["satisfied"] is False  # A's approval does not count for B
    with pytest.raises(ApprovalError):
        service.execute_refund("CASE-2003", idempotency_key="trap-stale-svc", as_of=GRADED)

    envelope = service.invoke_tool_envelope(
        "execute_refund",
        {
            "case_id": "CASE-2003", "computation_id": comp_a.computation_id,
            "amount": str(comp_a.refund_total), "tender_allocations": [],
            "idempotency_key": "trap-stale-gw",
        },
        case_id="CASE-2003", called_on=GRADED, actor="staff",
    )
    assert envelope.outcome == "denied"
    assert envelope.error["code"] == -32002
    assert envelope.error["data"]["policy_id"] == "GP-PAYMENT-APPROVAL"
    denied = [
        c for c in service.list_tool_calls("CASE-2003")
        if c.tool == "execute_refund" and c.status == "denied"
    ]
    assert len(denied) == 1 and denied[0].policy_id == "GP-PAYMENT-APPROVAL"
    assert payments.movement_count == 0
    assert service.list_receipts() == []

    # The current computation settles normally once ITS gate is met.
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=GRADED)
    receipt = service.execute_refund("CASE-2003", idempotency_key="trap-current", as_of=GRADED)
    assert receipt.amount == Decimal("116.00")
    assert receipt.computation_id == comp_b.computation_id


# --- T-P1: actor strings are exact -------------------------------------------
# Hand-derived from the pack: the actor vocabulary is a closed set
# of exact strings. "Staff", " staff" and "STAFF" are not in it —
# case and whitespace variants are denied by the actor rows, and
# the denial records the actor string verbatim.

def test_trap_actor_case_and_whitespace_variants_are_denied():
    service = _planted(_service())
    with pytest.raises(PolicyDeniedError) as excinfo:
        service.invoke_tool(
            "create_return_label", {"case_id": "CASE-2003", "issued_on": DAY},
            case_id="CASE-2003", called_on=DAY, actor="Staff",
        )
    assert excinfo.value.denial.policy_id == "GP-CARRIER-WRITE-ACTORS"
    assert service.list_labels("CASE-2003") == []
    denied = [c for c in service.list_tool_calls("CASE-2003") if c.status == "denied"]
    assert denied[-1].actor == "Staff"  # recorded verbatim, not normalised

    label = _label(service, "CASE-2003")
    with pytest.raises(PolicyDeniedError) as excinfo:
        service.invoke_tool(
            "get_carrier_events",
            {"case_id": "CASE-2003", "label_id": label.label_id, "as_of": DAY},
            case_id="CASE-2003", called_on=DAY, actor=" staff",
        )
    assert excinfo.value.denial.policy_id == "GP-CARRIER-READ-ACTORS"

    comp = service.get_computation("CASE-2006")
    with pytest.raises(PolicyDeniedError) as excinfo:
        service.invoke_tool(
            "execute_refund",
            {
                "case_id": "CASE-2006", "computation_id": comp.computation_id,
                "amount": str(comp.refund_total), "tender_allocations": [],
                "idempotency_key": "trap-actor-case",
            },
            case_id="CASE-2006", called_on=DAY, actor="STAFF",
        )
    assert excinfo.value.denial.policy_id == "GP-PAYMENT-ACTORS"


# --- T-P2: the role cap, on the cent ------------------------------------------
# Hand-derived from the pack: staff and returns_lead are capped at
# $500.00 per execute_refund call. An amount AT the cap is not a
# cap denial (it dies later, at the handler, for want of a prepared
# execution); one cent above is denied by GP-PAYMENT-ROLE-CAP —
# and so is the case's own $600.00 computed total: a refund above
# the role cap cannot move for a capped role at all in this build.

def test_trap_role_cap_edge_amounts():
    payments = LocalPaymentsDouble()
    service = _service(payments=payments)
    case = _priced_case(
        service, rid="REQ-T05", oid="ORD-T05", customer="CUST-T05",
        price="600.00", sku="JKT-ALPINE",
    )
    comp = service.get_computation(case.case_id)
    assert comp.refund_total == Decimal("600.00")
    assert approval_tier(PACK, comp.refund_total) == "dual"
    service.record_approval(case.case_id, approver="d.okafor", role="returns_lead", as_of=DAY)
    service.record_approval(case.case_id, approver="m.haddad", role="returns_lead", as_of=DAY)

    def _invoke(amount: str, key: str):
        return service.invoke_tool_envelope(
            "execute_refund",
            {
                "case_id": case.case_id, "computation_id": comp.computation_id,
                "amount": amount, "tender_allocations": [], "idempotency_key": key,
            },
            case_id=case.case_id, called_on=DAY, actor="staff",
        )

    at_cap = _invoke("500.00", "trap-cap-at")
    assert at_cap.outcome == "error"  # permitted by policy; the handler refuses (no prepared execution)
    assert not [
        c for c in service.list_tool_calls(case.case_id)
        if c.status == "denied" and c.policy_id == "GP-PAYMENT-ROLE-CAP"
    ]
    over_cap = _invoke("500.01", "trap-cap-over")
    assert over_cap.outcome == "denied"
    assert over_cap.error["data"]["policy_id"] == "GP-PAYMENT-ROLE-CAP"
    full = _invoke("600.00", "trap-cap-full")
    assert full.outcome == "denied"
    assert full.error["data"]["policy_id"] == "GP-PAYMENT-ROLE-CAP"
    with pytest.raises(PaymentsError, match="policy denied"):
        service.execute_refund(case.case_id, idempotency_key="trap-cap-svc", as_of=DAY)
    assert payments.movement_count == 0
    assert service.list_receipts() == []


# --- T-MS4: a discount promise in the note slot --------------------------------
# The trap that exposed defect 1. The cleared serial case's notice
# is drafted with a model note promising a discount. The note's
# percent is chosen to be an ALLOWED percent for this case (20% is
# the used restocking rate), so the figure checks all pass — only
# the promise itself can refuse it. No template offers a discount
# and the policy pack promises none; before the R14 disclosure fix
# this note verified clean. The trap stands as the regression.

def test_trap_discount_promise_in_a_note_is_refused():
    messaging = LocalMessagingDouble()
    service = _service(messaging=messaging)
    service.seed_history()
    for request in FRAUD_FEED_REQUESTS:
        service.ingest(request)
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez",
        outcome="cleared_with_reason", reason_code="legitimate_pattern_explained",
    )
    note = "As a goodwill gesture, a 20% discount will be applied to your next order."
    drafted = service.draft_message("CASE-3008", "fraud_review_notice", as_of=DAY, model_note=note)
    checked = service.verify_message(drafted.message_id, as_of=DAY)
    assert checked.status == "refused"
    assert any("discount" in v for v in checked.violations)
    with pytest.raises(MessagingError):
        service.queue_message(drafted.message_id, as_of=DAY)
    assert messaging.delivery_count == 0

    # The "% off" spelling of the same promise is refused too.
    drafted2 = service.draft_message(
        "CASE-3008", "fraud_review_notice", as_of=DAY,
        model_note="You will also get 20% off your next order.",
    )
    checked2 = service.verify_message(drafted2.message_id, as_of=DAY)
    assert checked2.status == "refused"
    assert any("forbidden" in v for v in checked2.violations)

    # Control: the plain template notice, no note, verifies.
    plain = service.draft_message("CASE-3008", "fraud_review_notice", as_of=DAY)
    assert service.verify_message(plain.message_id, as_of=DAY).status == "verified"
