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


# --- T-M6: a top-up can never pay past the graded total ------------------------
# Hand-derived: apparel jacket declared used — $189.00 − 20% fee −
# $0.00 shipping (free-return category) = $151.20, executed. Graded
# like_new — $189.00 − 10% = $170.10. The top-up is the delta,
# $18.90, and after it settles the case has paid out EXACTLY the
# graded total: $151.20 + $18.90 = $170.10, not a cent more, and a
# second top-up under a fresh key is a duplicate.

def _apparel_case(service: CaseService, *, rid: str, oid: str, customer: str, delivered: date):
    return _priced_case(
        service, rid=rid, oid=oid, customer=customer, price="189.00",
        sku="JKT-ALPINE", condition="used",
        delivered=delivered, requested=date(2026, 10, 26),
    )


def test_trap_top_up_settles_exactly_the_delta_and_no_more():
    payments = LocalPaymentsDouble()
    service = _service(payments=payments)
    case = _apparel_case(service, rid="REQ-T06", oid="ORD-T06", customer="CUST-T06",
                         delivered=date(2026, 9, 26))  # age exactly 30 at request
    comp_a = service.get_computation(case.case_id)
    assert comp_a.refund_total == Decimal("151.20")
    for approver in ("d.okafor", "m.haddad"):  # $151.20 is a dual tier
        service.record_approval(case.case_id, approver=approver, role="returns_lead", as_of=DAY)
    service.execute_refund(case.case_id, idempotency_key="trap-t06-orig", as_of=DAY)
    _label(service, case.case_id)
    service.record_warehouse_receipt(case.case_id, received_by="w.chen", received_on=RECEIVED)
    report = service.record_grading(
        case.case_id, grader="p.novak", graded_condition="like_new", graded_on=GRADED
    )
    assert report.refund_delta == Decimal("18.90")
    adjustments = service.list_adjustments(case.case_id)
    assert len(adjustments) == 1
    adjustment = adjustments[0]
    assert adjustment.kind == "top_up_due" and adjustment.amount == Decimal("18.90")
    assert adjustment.status == "open"
    comp_b = service.get_computation(case.case_id)
    assert comp_b.refund_total == Decimal("170.10")

    for approver in ("d.okafor", "m.haddad"):  # approvals re-checked against B
        service.record_approval(case.case_id, approver=approver, role="returns_lead", as_of=GRADED)
    top_up = service.execute_top_up(case.case_id, idempotency_key="trap-t06-top", as_of=GRADED)
    assert top_up.amount == Decimal("18.90")
    paid = sum((r.amount for r in service.list_receipts()), Decimal("0.00"))
    assert paid == comp_b.refund_total == Decimal("170.10")
    assert service.get_adjustment(adjustment.adjustment_id).status == "executed"
    with pytest.raises(DuplicateRefundError):
        service.execute_top_up(case.case_id, idempotency_key="trap-t06-top-again", as_of=GRADED)
    assert sum((r.amount for r in service.list_receipts()), Decimal("0.00")) == Decimal("170.10")


# --- T-M7: a recovery is exactly the over-refund, and it is not a top-up -------
# Hand-derived: boots like_new executed at $130.50; graded used ->
# $116.00. The recovery position is $14.50 — the over-refund to the
# cent, never more. It cannot be settled through the top-up path,
# and no clawback moves: the money out stays $130.50 and the
# position stands open on the record.

def test_trap_recovery_is_exactly_the_over_refund_and_never_clawed_back():
    payments = LocalPaymentsDouble()
    service = _planted(_service(payments=payments))
    report = _grade_boots_2003(service, execute_first=True)
    assert report.refund_delta == Decimal("-14.50")
    adjustments = service.list_adjustments("CASE-2003")
    assert len(adjustments) == 1
    adjustment = adjustments[0]
    receipt = service.get_receipt("CASE-2003")
    comp_b = service.get_computation("CASE-2003")
    assert adjustment.kind == "recovery_due"
    assert adjustment.amount == Decimal("14.50")
    assert adjustment.amount == receipt.amount - comp_b.refund_total
    assert adjustment.status == "open"
    with pytest.raises(PaymentsError, match="no open top-up"):
        service.execute_top_up("CASE-2003", idempotency_key="trap-t07", as_of=GRADED)
    assert sum((r.amount for r in service.list_receipts()), Decimal("0.00")) == Decimal("130.50")
    assert service.get_adjustment(adjustment.adjustment_id).status == "open"


# --- T-M8: a one-cent label variance stands on the record -----------------------
# Hand-derived: the computation deducts the pack's $6.95 shipping
# fee. A carrier schedule that prices the same label at $6.96 makes
# the reconciliation a variance of exactly $0.01 — stated, never
# rounded away, never called "matched".

def test_trap_one_cent_label_variance_is_stated_not_absorbed():
    variant = load_policy_pack().model_copy(deep=True)
    variant.label_costs["ground"] = "6.96"
    service = _service(pack=variant)
    case = _priced_case(
        service, rid="REQ-T08", oid="ORD-T08", customer="CUST-T08",
        price="59.95", sku="STOVE-PKT", condition="used",
    )
    label = _label(service, case.case_id)
    assert label.cost == Decimal("6.96")
    reconciliation = label.reconciliation
    assert reconciliation.status == "variance"
    assert reconciliation.deducted_amount == Decimal("6.95")
    assert reconciliation.label_cost == Decimal("6.96")
    assert reconciliation.variance == Decimal("0.01")

    control = _service()
    case2 = _priced_case(
        control, rid="REQ-T08", oid="ORD-T08", customer="CUST-T08",
        price="59.95", sku="STOVE-PKT", condition="used",
    )
    label2 = _label(control, case2.case_id)
    assert label2.reconciliation.status == "matched"
    assert label2.reconciliation.variance == Decimal("0.00")


# --- T-F1: the planted serial returner, at exactly 95.00 ------------------------
# Hand-derived in seed.py: velocity 40 (7 priors, allowance 2, cap)
# + share 35 (8/8 orders) + churn 20 (4 tenders) + wardrobing 0 +
# weight not computable = 95.00 -> FRAUD_REVIEW. The wardrobing
# pattern: 36.00 (3 matches at the cap) -> MONITOR.

def _fraud_service() -> CaseService:
    service = _service()
    service.seed_history()
    for request in FRAUD_FEED_REQUESTS:
        service.ingest(request)
    return service


def _signals(score) -> dict[str, Decimal]:
    return {s.signal_id: s.contribution for s in score.signals}


def test_trap_planted_serial_returner_scores_exactly_95():
    service = _fraud_service()
    score = service.get_fraud_score("CASE-3008")
    assert score.total_score == Decimal("95.00")
    assert score.band == "FRAUD_REVIEW"
    assert _signals(score) == {
        "return_velocity": Decimal("40.00"),
        "serial_returner_share": Decimal("35.00"),
        "wardrobing_indicator": Decimal("0.00"),
        "weight_mismatch": Decimal("0.00"),
        "card_churn": Decimal("20.00"),
    }
    ward = service.get_fraud_score("CASE-3103")
    assert ward.total_score == Decimal("36.00") and ward.band == "MONITOR"
    assert _signals(ward)["wardrobing_indicator"] == Decimal("36.00")


# --- T-F2: the 94.99 probe and the true band cliff -------------------------------
# The brief's probe asked for a case at exactly 94.99. Hand
# derivation says that figure is UNREACHABLE: every contribution in
# the scoring pack is a whole number of points, so every total the
# engine can produce is integral — there is no fractional gap under
# 95.00 for a case to slip through. The trap therefore grades (a)
# the band function either side of the planted figure, (b) the
# integrality invariant over every score the suite produces, and
# (c) the pack's TRUE cliff, engineered through the engine below:
# 60.00 is FRAUD_REVIEW, 59.00 is MONITOR.

def test_trap_band_function_and_score_integrality():
    pack = load_scoring_pack()
    assert pack.band_for(Decimal("94.99")) == "FRAUD_REVIEW"
    assert pack.band_for(Decimal("95.00")) == "FRAUD_REVIEW"
    assert pack.band_for(Decimal("60.00")) == "FRAUD_REVIEW"
    assert pack.band_for(Decimal("59.99")) == "MONITOR"
    service = _fraud_service()
    for score in service.list_fraud_scores():
        assert score.total_score == score.total_score.to_integral_value()


def _seed_scored_customer(
    service: CaseService,
    *,
    customer: str,
    prefix: str,
    n_orders: int,
    priors: list[tuple[str, str, date]],
    live_sku: str,
    live_condition: str,
    tender: str | None,
    live_on: date = date(2026, 10, 24),
) -> str:
    """Programmatic history, gold by construction: closed cases and
    their orders, then the live request ingested (and scored)
    through the service. Orders for cases are delivered 40 days
    before the case's request; filler orders carry the same tender
    posture and are never returned."""
    for i, (sku, condition, requested_on) in enumerate(priors, start=1):
        oid = f"ORD-{prefix}-{i:02d}"
        line = OrderLine(sku=sku, qty=1, unit_price=CATALOGUE[sku].unit_price)
        order = Order(
            order_id=oid, customer_id=customer,
            placed_on=requested_on - timedelta(days=43),
            delivered_on=requested_on - timedelta(days=40),
            lines=[line], shipping_paid=Decimal("0.00"),
            tenders=[TenderPortion(method=tender, amount=line.line_total)] if tender else None,
        )
        service._store.put_order(order.model_dump(mode="json"))
        service._store.put_case(
            ReturnCase(
                case_id=f"CASE-{prefix}-{i:02d}",
                request=ReturnRequest(
                    request_id=f"REQ-{prefix}-{i:02d}", order_id=oid, sku=sku, qty=1,
                    reason_code="changed_mind", condition_declared=condition,
                    requested_on=requested_on,
                ),
                order_id=oid, customer_id=customer, status="closed",
            ).model_dump(mode="json")
        )
    for j in range(n_orders - len(priors) - 1):
        oid = f"ORD-{prefix}-X{j:02d}"
        line = OrderLine(sku="STOVE-PKT", qty=1, unit_price=CATALOGUE["STOVE-PKT"].unit_price)
        service._store.put_order(
            Order(
                order_id=oid, customer_id=customer,
                placed_on=date(2026, 7, 29) + timedelta(days=j * 5),
                delivered_on=date(2026, 8, 1) + timedelta(days=j * 5),
                lines=[line], shipping_paid=Decimal("0.00"),
                tenders=[TenderPortion(method=tender, amount=line.line_total)] if tender else None,
            ).model_dump(mode="json")
        )
    live_oid = f"ORD-{prefix}-LIVE"
    line = OrderLine(sku=live_sku, qty=1, unit_price=CATALOGUE[live_sku].unit_price)
    service._store.put_order(
        Order(
            order_id=live_oid, customer_id=customer,
            placed_on=live_on - timedelta(days=43),
            delivered_on=live_on - timedelta(days=40),
            lines=[line], shipping_paid=Decimal("0.00"),
            tenders=[TenderPortion(method=tender, amount=line.line_total)] if tender else None,
        ).model_dump(mode="json")
    )
    case = service.ingest(
        ReturnRequest(
            request_id=f"REQ-{prefix}-LIVE", order_id=live_oid, sku=live_sku, qty=1,
            reason_code="changed_mind", condition_declared=live_condition,
            requested_on=live_on,
        )
    )
    return case.case_id


def test_trap_engineered_score_exactly_at_the_fraud_band_boundary():
    # velocity 40 (7 priors in the window) + share 20 (8 of 14
    # orders returned) + churn 0 (a single tender) = 60.00 exactly.
    service = _service()
    priors = [("BOOT-TRL", "used", date(2026, 8, 2) + timedelta(days=i * 11)) for i in range(7)]
    case_id = _seed_scored_customer(
        service, customer="CUST-EDGE60", prefix="E60", n_orders=14,
        priors=priors, live_sku="BOOT-TRL", live_condition="used", tender="card_visa_4411",
    )
    score = service.get_fraud_score(case_id)
    assert score.total_score == Decimal("60.00")
    assert score.band == "FRAUD_REVIEW"
    assert _signals(score)["return_velocity"] == Decimal("40.00")
    assert _signals(score)["serial_returner_share"] == Decimal("20.00")
    assert service.refund_path_blocked(case_id) is True
    with pytest.raises(FraudHoldError):
        service.record_approval(case_id, approver="d.okafor", role="returns_lead", as_of=DAY)


def test_trap_engineered_score_one_reachable_point_below_the_boundary():
    # velocity 24 (5 priors) + share 35 (6 of 8 orders) = 59.00 —
    # the nearest reachable total below the boundary. MONITOR does
    # not block the refund path.
    service = _service()
    priors = [("BOOT-TRL", "used", date(2026, 8, 6) + timedelta(days=i * 13)) for i in range(5)]
    case_id = _seed_scored_customer(
        service, customer="CUST-EDGE59", prefix="E59", n_orders=8,
        priors=priors, live_sku="BOOT-TRL", live_condition="used", tender="card_visa_4411",
    )
    score = service.get_fraud_score(case_id)
    assert score.total_score == Decimal("59.00")
    assert score.band == "MONITOR"
    assert service.refund_path_blocked(case_id) is False
    service.record_approval(case_id, approver="d.okafor", role="returns_lead", as_of=DAY)


# --- T-F3: wardrobing split across two accounts ---------------------------------
# Hand-derived: each account carries 4 priors (2 of them wardrobing
# matches) plus a live wardrobing match — velocity 16 + wardrobing
# 36 (3 matches) + share 0 (5 of 16 orders = 0.3125, below the
# 0.34 tier) = 52.00, MONITOR. IF the two accounts' histories were
# merged — the same household, the link unprovable from anything
# the store holds — the merged history would score velocity 40
# (8 priors, capped) + wardrobing 36 (capped) = 76.00,
# FRAUD_REVIEW. The engine scores only what it can prove: each
# customer's own cases, named on the artefact. The gap is named
# here and in docs/trap-suite.md: cross-account identity
# resolution is not a signal the pack scores.

def test_trap_split_household_is_scored_per_customer_never_merged():
    service = _service()
    priors = [
        ("JKT-ALPINE", "new_with_tags", date(2026, 9, 12)),
        ("BOOT-TRL", "new_with_tags", date(2026, 9, 26)),
        ("JKT-ALPINE", "used", date(2026, 10, 3)),
        ("BOOT-TRL", "used", date(2026, 10, 10)),
    ]
    case_ids = {}
    for customer, prefix in (("CUST-HHA", "HHA"), ("CUST-HHB", "HHB")):
        case_ids[customer] = _seed_scored_customer(
            service, customer=customer, prefix=prefix, n_orders=16,
            priors=priors, live_sku="JKT-ALPINE", live_condition="new_with_tags",
            tender=None,
        )
    for customer, case_id in case_ids.items():
        score = service.get_fraud_score(case_id)
        assert score.total_score == Decimal("52.00")
        assert score.band == "MONITOR"
        assert _signals(score)["wardrobing_indicator"] == Decimal("36.00")
        assert _signals(score)["return_velocity"] == Decimal("16.00")
        assert all(cid.startswith(f"CASE-{customer[-3:]}") for cid in score.input_case_ids)
    all_inputs = [set(s.input_case_ids) for s in service.list_fraud_scores()]
    hha = next(s for s in service.list_fraud_scores() if s.case_id == case_ids["CUST-HHA"])
    hhb = next(s for s in service.list_fraud_scores() if s.case_id == case_ids["CUST-HHB"])
    assert not (set(hha.input_case_ids) & set(hhb.input_case_ids))
    assert all(len(inputs) == 5 for inputs in all_inputs if inputs)


# --- T-F4: the review act's scope -------------------------------------------------
# The pack read (docs/trap-suite.md): the scoring pack governs the
# review's outcome/reason vocabulary and nothing about WHO reviews
# — there is no opener in the machinery for a reviewer to be
# separated from. What the pack DOES scope is the decision itself:
# it decides ONE score. (a) A clearance on CASE-3008's score does
# not clear the same customer's next case when its own score lands
# in FRAUD_REVIEW. (b) A confirmed_fraud decision is sticky — no
# later artefact un-happens it. (c) Off-vocabulary pairings and
# decisions on cases not in review are refused.

def test_trap_clearance_decides_one_score_not_the_customer():
    service = _fraud_service()
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez",
        outcome="cleared_with_reason", reason_code="legitimate_pattern_explained",
    )
    assert service.refund_path_blocked("CASE-3008") is False
    line = OrderLine(sku="BOOT-TRL", qty=1, unit_price=CATALOGUE["BOOT-TRL"].unit_price)
    service._store.put_order(
        Order(
            order_id="ORD-3009", customer_id="CUST-SER",
            placed_on=date(2026, 10, 12), delivered_on=date(2026, 10, 15),
            lines=[line], shipping_paid=Decimal("0.00"),
            tenders=[TenderPortion(method="card_visa_4411", amount=line.line_total)],
        ).model_dump(mode="json")
    )
    case = service.ingest(
        ReturnRequest(
            request_id="REQ-3009", order_id="ORD-3009", sku="BOOT-TRL", qty=1,
            reason_code="changed_mind", condition_declared="used",
            requested_on=date(2026, 10, 25),
        )
    )
    score = service.get_fraud_score(case.case_id)
    # 8 priors in window -> velocity capped 40; 9/9 orders -> share
    # 35; 4 tenders -> churn 20: 95.00 again, a NEW artefact.
    assert score.total_score == Decimal("95.00") and score.band == "FRAUD_REVIEW"
    assert service.refund_path_blocked(case.case_id) is True
    service.record_fraud_review(
        case.case_id, reviewer="r.alvarez",
        outcome="cleared_with_reason", reason_code="legitimate_pattern_explained",
    )
    assert service.refund_path_blocked(case.case_id) is False
    assert service.refund_path_blocked("CASE-3008") is False


def test_trap_confirmed_fraud_is_sticky():
    service = _fraud_service()
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez",
        outcome="confirmed_fraud", reason_code="pattern_verified",
    )
    case = service.get_case("CASE-3008")
    assert case.fraud_state == "fraud_confirmed"
    assert service.refund_path_blocked("CASE-3008") is True
    with pytest.raises(FraudHoldError):
        service.record_approval("CASE-3008", approver="d.okafor", role="returns_lead", as_of=DAY)
    with pytest.raises(FraudHoldError):
        service.execute_refund("CASE-3008", idempotency_key="trap-f4b", as_of=DAY)
    with pytest.raises(FraudReviewError, match="already has a review decision"):
        service.record_fraud_review(
            "CASE-3008", reviewer="m.haddad",
            outcome="cleared_with_reason", reason_code="legitimate_pattern_explained",
        )
    service.rescore_case("CASE-3008")
    assert service.get_case("CASE-3008").fraud_state == "fraud_confirmed"
    assert service.refund_path_blocked("CASE-3008") is True


def test_trap_review_vocabulary_and_state_are_enforced():
    service = _fraud_service()
    with pytest.raises(FraudReviewError, match="not a coded reason"):
        service.record_fraud_review(
            "CASE-3008", reviewer="r.alvarez",
            outcome="cleared_with_reason", reason_code="pattern_verified",
        )
    with pytest.raises(FraudReviewError, match="not in fraud review"):
        service.record_fraud_review(
            "CASE-3103", reviewer="r.alvarez",
            outcome="cleared_with_reason", reason_code="legitimate_pattern_explained",
        )
    with pytest.raises(FraudReviewError, match="named reviewer"):
        service.record_fraud_review(
            "CASE-3008", reviewer="  ",
            outcome="cleared_with_reason", reason_code="legitimate_pattern_explained",
        )
    assert service.refund_path_blocked("CASE-3008") is True  # nothing landed


# --- T-G1 / T-P3: grading before the parcel exists ------------------------------
# Hand-derived from the carrier script: a label issued on DAY is
# tendered that day, in transit DAY+1, received DAY+3. A warehouse
# receipt on DAY+1 refuses (the carrier still shows the parcel in
# transit); a grading callback before any receipt refuses in the
# HANDLER — the gateway record stands at "error" with no policy
# id, which is a different thing from a policy denial and is
# graded as such.

def test_trap_grading_before_the_parcel_is_received():
    service = _planted(_service())
    _label(service, "CASE-2003")
    with pytest.raises(GradingError, match="has not recorded receipt"):
        service.record_warehouse_receipt(
            "CASE-2003", received_by="w.chen", received_on=date(2026, 10, 28)
        )
    with pytest.raises(GradingError, match="no warehouse receipt of record"):
        service.record_grading(
            "CASE-2003", grader="p.novak", graded_condition="used", graded_on=date(2026, 10, 28)
        )
    calls = [
        c for c in service.list_tool_calls("CASE-2003") if c.tool == "submit_grading_report"
    ]
    assert len(calls) == 1
    assert calls[0].status == "error" and calls[0].policy_id is None

    # T-P3: a case with no label at all — the callback is a handler
    # error (envelope outcome "error", no -32002), never a denial.
    envelope = service.invoke_tool_envelope(
        "submit_grading_report",
        {
            "case_id": "CASE-2001", "grader": "p.novak", "graded_condition": "used",
            "graded_on": DAY, "functional_test_result": None, "data_wipe_status": None,
            "damage_notes": "", "received_weight_grams": None, "wear_evidence": False,
        },
        case_id="CASE-2001", called_on=DAY, actor="warehouse",
    )
    assert envelope.outcome == "error"
    assert envelope.error["code"] != -32002
    calls_2001 = service.list_tool_calls("CASE-2001")
    assert calls_2001 and all(c.status != "denied" for c in calls_2001)
    assert service.list_grading_reports("CASE-2001") == []

    # Control: on the carrier's received day, the same acts land.
    service.record_warehouse_receipt("CASE-2003", received_by="w.chen", received_on=RECEIVED)
    report = service.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="used", graded_on=GRADED
    )
    assert report.graded_condition == "used"


# --- T-G2: a second grading after the first finalised ---------------------------
# Grading is a single act of record. A second callback — different
# grader, different condition — refuses, and every stored artefact
# of the first cascade stands byte-identical.

def test_trap_second_grading_is_refused_and_the_first_stands():
    service = _planted(_service())
    _grade_boots_2003(service)
    before = sorted(
        (c["computation_id"], str(c)) for c in service._store.list_computations()
    )
    with pytest.raises(GradingError, match="already has a finalised grading report"):
        service.record_grading(
            "CASE-2003", grader="g.osei", graded_condition="like_new", graded_on=GRADED
        )
    assert len(service.list_grading_reports("CASE-2003")) == 1
    after = sorted(
        (c["computation_id"], str(c)) for c in service._store.list_computations()
    )
    assert before == after
    assert service.get_computation("CASE-2003").refund_total == Decimal("116.00")


# --- T-G3: graded better than claimed, at the matrix's exact age edge ------------
# Hand-derived from the matrix: apparel like_new is RESTOCK in the
# fresh band (age 0-30) and OUTLET in the season band (31-90). Two
# identical jackets, requested the same day, delivered one day
# apart — age exactly 30 and exactly 31 — graded up from used to
# like_new must land on opposite sides of the edge. (The age-30
# case's money side is T-M6.)

def test_trap_graded_disposition_at_the_exact_age_band_edge():
    service = _service()
    fresh = _apparel_case(service, rid="REQ-T09", oid="ORD-T09", customer="CUST-T09",
                          delivered=date(2026, 9, 26))  # age 30
    season = _apparel_case(service, rid="REQ-T10", oid="ORD-T10", customer="CUST-T10",
                           delivered=date(2026, 9, 25))  # age 31
    for case in (fresh, season):
        assert service.get_disposition(case.case_id).age_days in (30, 31)
        _label(service, case.case_id)
        service.record_warehouse_receipt(case.case_id, received_by="w.chen", received_on=RECEIVED)
        service.record_grading(
            case.case_id, grader="p.novak", graded_condition="like_new", graded_on=GRADED
        )
    fresh_disp = service.get_disposition(fresh.case_id)
    assert fresh_disp.basis == "graded"
    assert fresh_disp.age_band == "fresh" and fresh_disp.age_days == 30
    assert fresh_disp.cell_id == "M-APP-LN-FRESH" and fresh_disp.disposition == "RESTOCK"
    season_disp = service.get_disposition(season.case_id)
    assert season_disp.age_band == "season" and season_disp.age_days == 31
    assert season_disp.cell_id == "M-APP-LN-SEASON" and season_disp.disposition == "OUTLET"


# --- T-MS1: a queued summary is withdrawn when grading supersedes it -------------
# The summary stands verified and QUEUED against computation A.
# Grading supersedes A; the re-issued summary withdraws the queued
# one in the lifecycle. A send attempt on the queued message
# refuses, nothing is delivered, and the replacement stands
# verified against the current artefacts.

def test_trap_queued_message_withdrawn_by_grading_supersession():
    messaging = LocalMessagingDouble()
    service = _planted(_service(messaging=messaging))
    queued = next(
        m for m in service.list_messages("CASE-2003")
        if m.message_type == "return_summary" and m.status == "verified"
    )
    service.queue_message(queued.message_id, as_of=DAY)
    _grade_boots_2003(service)
    assert service.get_message(queued.message_id).status == "withdrawn"
    with pytest.raises(MessagingError):
        service.send_message(queued.message_id, as_of=GRADED)
    assert messaging.delivery_count == 0
    assert queued.message_id not in messaging.deliveries
    replacement = [
        m for m in service.list_messages("CASE-2003")
        if m.message_type == "return_summary" and m.status == "verified"
    ]
    assert len(replacement) == 1
    assert replacement[0].artefact_fingerprint != queued.artefact_fingerprint


# --- T-MS2: send-time staleness — the label is voided under a queued message -----
# Label instructions are queued against the active label; the
# label is then voided. At send time the fingerprint no longer
# matches: the message is withdrawn unsent, the send refuses, and
# no replacement can be drafted (there is no active label to give
# instructions for).

def test_trap_send_time_staleness_after_label_void():
    messaging = LocalMessagingDouble()
    service = _planted(_service(messaging=messaging))
    label = _label(service, "CASE-2003")
    drafted = service.draft_message("CASE-2003", "label_instructions", as_of=DAY)
    assert service.verify_message(drafted.message_id, as_of=DAY).status == "verified"
    service.queue_message(drafted.message_id, as_of=DAY)
    service.invoke_tool(
        "void_return_label",
        {
            "case_id": "CASE-2003", "label_id": label.label_id,
            "voided_by": "d.okafor", "reason": "customer kept the item",
            "voided_on": DAY,
        },
        case_id="CASE-2003", called_on=DAY,
    )
    with pytest.raises(MessagingError, match="stale"):
        service.send_message(drafted.message_id, as_of=DAY)
    assert service.get_message(drafted.message_id).status == "withdrawn"
    assert messaging.delivery_count == 0
    assert not [
        m for m in service.list_messages("CASE-2003")
        if m.message_type == "label_instructions" and m.status == "verified"
    ]


# --- T-MS3: one note, two independent violations ----------------------------------
# A model note carrying a forbidden internal term AND a wrong
# figure (and a score stated in points for good measure) must be
# refused with BOTH families on the record — the verifier does not
# stop at the first violation.

def test_trap_note_with_forbidden_term_and_wrong_figure_records_both():
    messaging = LocalMessagingDouble()
    service = _planted(_service(messaging=messaging))
    drafted = service.draft_message(
        "CASE-2001", "return_summary", as_of=DAY,
        model_note=(
            "Your return_velocity pattern scored 95 points, over the "
            "review threshold. Your refund is $999.99."
        ),
    )
    checked = service.verify_message(drafted.message_id, as_of=DAY)
    assert checked.status == "refused"
    assert any("forbidden" in v and "return_velocity" in v for v in checked.violations)
    assert any("amount $999.99" in v for v in checked.violations)
    assert any("points" in v for v in checked.violations)
    with pytest.raises(MessagingError):
        service.queue_message(drafted.message_id, as_of=DAY)
    assert messaging.delivery_count == 0


# --- T-MEM1: a lookalike customer id retrieves nothing, on the log -----------------
# The memory port's predicate is the exact customer id. "CUST-CIE"
# differs from "CUST-CID" by one character: the retrieval returns
# nothing, the stranger is not a customer of record, and the probe
# itself stands in the access log with an empty returned set.

def test_trap_lookalike_customer_id_retrieves_nothing():
    service = _planted(_service())
    _label(service, "CASE-2003")
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY)
    service.execute_refund("CASE-2003", idempotency_key="trap-mem1", as_of=DAY)
    assert service.retrieve_memory("CUST-CID", as_of=DAY)  # control: records exist
    assert service.retrieve_memory("CUST-CIE", as_of=DAY) == []
    assert service.has_customer("CUST-CIE") is False
    probes = [
        e for e in service.list_memory_access_log("CUST-CIE")
    ]
    assert probes and probes[-1].returned_record_ids == []


# --- T-MEM2: an expired record's figure is refused under the real pack -------------
# Under the shipped 730-day retention, CID's October 2026 records
# expire in October 2028. The $130.50 figure verifies in CID's own
# message in November 2026 (memory live) and is REFUSED in January
# 2029 (memory expired) — same customer, same figure, the only
# thing that changed is the record's age.

def test_trap_expired_memory_figure_refused_under_the_shipped_pack():
    service = _planted(_service())
    _label(service, "CASE-2003")
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY)
    service.execute_refund("CASE-2003", idempotency_key="trap-mem2", as_of=DAY)
    cid_records = service.retrieve_memory("CUST-CID", as_of=DAY)
    cid_2026_ids = {r.record_id for r in cid_records}
    assert cid_2026_ids

    def _later_boots(rid: str, oid: str, delivered: date, requested: date) -> str:
        service._store.put_order(
            Order(
                order_id=oid, customer_id="CUST-CID",
                placed_on=delivered - timedelta(days=3), delivered_on=delivered,
                lines=[OrderLine(sku="BOOT-TRL", qty=1, unit_price=CATALOGUE["BOOT-TRL"].unit_price)],
                shipping_paid=Decimal("0.00"),
            ).model_dump(mode="json")
        )
        return service.ingest(
            ReturnRequest(
                request_id=rid, order_id=oid, sku="BOOT-TRL", qty=1,
                reason_code="changed_mind", condition_declared="new_with_tags",
                requested_on=requested,
            )
        ).case_id

    note = "As with your earlier return, the refund of $130.50 was sent to your original payment method."
    live_case = _later_boots("REQ-2009", "ORD-1009", date(2026, 10, 28), date(2026, 11, 2))
    drafted = service.draft_message(live_case, "return_summary", as_of=date(2026, 11, 2), model_note=note)
    assert service.verify_message(drafted.message_id, as_of=date(2026, 11, 2)).status == "verified"

    expired_case = _later_boots("REQ-1099", "ORD-1099", date(2028, 12, 20), date(2029, 1, 15))
    as_of_2029 = date(2029, 1, 15)
    returned = service.retrieve_memory("CUST-CID", as_of=as_of_2029)
    assert not (cid_2026_ids & {r.record_id for r in returned})
    status = service.memory_retention_status("CUST-CID", as_of=as_of_2029)
    assert status["expired_count"] >= len(cid_2026_ids)
    drafted = service.draft_message(expired_case, "return_summary", as_of=as_of_2029, model_note=note)
    checked = service.verify_message(drafted.message_id, as_of=as_of_2029)
    assert checked.status == "refused"
    assert any("amount $130.50" in v for v in checked.violations)
