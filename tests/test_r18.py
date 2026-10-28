"""R18 gates: carrier exception flows — when the journey breaks.

Graded: detection raised in code from the carrier event record
(the pack's windows per kind; a healthy journey raises nothing;
re-detection returns the standing artefact; the verifier
re-performs the raising and catches a tampered exception); the
handling acts (only the pack's paths for the kind, a resolved
exception takes no further act, an unnamed actor is refused);
the refund guard (an open or escalated exception bars refund
execution, a customer-kept resolution bars it permanently, a
refund-as-received resolution lifts the bar); transit damage
attribution (a carrier damage event + a materially worse grade
attributes the damage to transit: no re-price, no adjustment, no
fraud re-score, the disposition follows the pack's transit row
and verifies on its own branch — and the same grade jump without
the carrier event is R7's cascade, unchanged); the carrier claim
(assembled only from artefacts of record at the pack's formula,
gated on the exception's state, one claim per exception, an
insert-only acknowledged/settled/denied lifecycle with the pack's
deny vocabulary, a whole-record verifier that catches tampering,
and settlement recorded as its own ledger fact — the money
summary gains the claims line and the customer's figures do not
move); access (the supervisor tier for settlements above the
pack threshold, the customer's narrowed status shape, existence
hiding byte-identical to a missing case, claims refused to
customers in the hiding shape, the auditor read-only); messaging
(the exception notice in the pack's wording, verified and
sendable; a transit grading notice that never renders the
customer-blame phrase); reporting (transit-attributed grading
never counts as misrepresentation in the fraud truth); and
migration 0014 as the ledger tail. Parity across service, API
and Runtime dispatch, and persistence on the SQL store.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.access import AccessDeniedError, Principal
from returns_resolution.api import create_app
from returns_resolution.carrier import LocalCarrierDouble
from returns_resolution.carrier_exceptions import (
    CarrierExceptionError,
    build_exception,
    load_exception_pack,
)
from returns_resolution.intake import FRAUD_FEED_REQUESTS, PLANTED_REQUESTS, ReferenceFeed
from returns_resolution.modelport import OfflineComposer
from returns_resolution.runtime_app import handle_payload
from returns_resolution.service import CaseService, PaymentsError
from returns_resolution.store import MIGRATIONS, InMemoryStore

DAY = date(2026, 10, 27)  # label issue day
STAFF = "s.reyes"
DAY_LOST = date(2026, 11, 6)  # DAY + 10: the lost-in-transit window

AGENT = Principal(id="j.agent", role="staff_agent")
SUPERVISOR = Principal(id="s.super", role="staff_supervisor")
AUDITOR = Principal(id="a.auditor", role="auditor")


def _service(carrier=None, store=None) -> CaseService:
    service = CaseService(
        store or InMemoryStore(),
        OfflineComposer(),
        carrier_port=carrier or LocalCarrierDouble(),
    )
    service.seed_orders()
    return service


def _ingest(service, *request_ids):
    by_id = {r.request_id: r for r in PLANTED_REQUESTS}
    for request_id in request_ids:
        service.ingest(by_id[request_id])


def _issue(service, case_id, on=DAY):
    service.invoke_tool(
        "create_return_label", {"case_id": case_id, "issued_on": on},
        case_id=case_id, called_on=on,
    )
    return service.get_label_for_case(case_id)


def _scripted_case(carrier, script, case_id="CASE-2003", request_id="REQ-2003"):
    service = _service(carrier)
    _ingest(service, request_id)
    label = _issue(service, case_id)
    carrier.set_script(carrier_ref=label.carrier_ref, script=script)
    return service, label


def _lost_case(carrier=None):
    return _scripted_case(carrier or LocalCarrierDouble(), "stalled_in_transit")


def _approve(service, case_id, *approvers):
    for approver in approvers:
        service.record_approval(case_id, approver=approver, role="returns_lead", as_of=DAY)


# -- the pack & detection ---------------------------------------------------


def test_exception_pack_is_versioned_data():
    pack = load_exception_pack()
    assert pack.version == "2026.10.1"
    assert pack.never_tendered_days == 5
    assert pack.lost_in_transit_days == 10
    assert pack.claim_settlement_supervisor_threshold == Decimal("250.00")
    assert pack.transit_rule_id == "EX-TRANSIT-DAMAGE"
    assert pack.transit_disposition == "RETURN_TO_VENDOR"
    assert pack.paths_for("lost_in_transit") == ["refund_as_received", "escalate"]


def test_detection_windows_and_kinds():
    carrier = LocalCarrierDouble()
    service, _ = _lost_case(carrier)
    # One day short of the window: nothing.
    assert service.detect_carrier_exceptions("CASE-2003", as_of=date(2026, 11, 5)) == []
    found = service.detect_carrier_exceptions("CASE-2003", as_of=DAY_LOST)
    assert len(found) == 1
    exc = found[0]
    assert exc.kind == "lost_in_transit" and exc.window_days == 10
    assert exc.event_kinds == ["tendered", "in_transit"]
    view = service.exception_view(exc)
    assert view["state"] == "open" and view["resolution_path"] is None
    assert service.verify_exception(exc.exception_id) == []


def test_never_tendered_window():
    carrier = LocalCarrierDouble()
    service, _ = _scripted_case(carrier, "never_tendered", "CASE-2001", "REQ-2001")
    assert service.detect_carrier_exceptions("CASE-2001", as_of=date(2026, 10, 31)) == []
    found = service.detect_carrier_exceptions("CASE-2001", as_of=date(2026, 11, 1))
    assert len(found) == 1
    assert found[0].kind == "label_never_tendered"
    assert found[0].event_ids == [] and found[0].event_kinds == []


def test_healthy_journey_raises_nothing_and_redetection_is_idempotent():
    carrier = LocalCarrierDouble()
    service = _service(carrier)
    _ingest(service, "REQ-2003")
    _issue(service, "CASE-2003")
    assert service.detect_carrier_exceptions("CASE-2003", as_of=date(2026, 12, 1)) == []
    service2, _ = _lost_case(LocalCarrierDouble())
    first = service2.detect_carrier_exceptions("CASE-2003", as_of=DAY_LOST)
    second = service2.detect_carrier_exceptions("CASE-2003", as_of=date(2026, 11, 20))
    assert [e.exception_id for e in second] == [e.exception_id for e in first]
    assert len(service2.list_exceptions("CASE-2003")) == 1


def test_damaged_event_raises_immediately():
    carrier = LocalCarrierDouble()
    service, _ = _scripted_case(carrier, "damaged_in_transit")
    found = service.detect_carrier_exceptions("CASE-2003", as_of=date(2026, 10, 29))
    assert len(found) == 1
    assert found[0].kind == "damaged_in_transit"
    assert found[0].event_kinds[-1] == "damaged"


def test_verify_exception_catches_a_premature_raising():
    # An exception asserted before the pack's window ran out —
    # written straight to the store, as a tampered or buggy
    # raising would — fails its own re-performance: exceptions
    # are raised from the events, on the window, or not at all.
    carrier = LocalCarrierDouble()
    service = _service(carrier)
    _ingest(service, "REQ-2003")
    label = _issue(service, "CASE-2003")
    carrier.set_script(carrier_ref=label.carrier_ref, script="stalled_in_transit")
    forged = build_exception(
        case_id="CASE-2003", label_id=label.label_id, kind="lost_in_transit",
        raised_on=date(2026, 10, 30), issued_on=DAY, window_days=10,
        event_ids=["evt-a", "evt-b"], event_kinds=["tendered", "in_transit"],
        pack=load_exception_pack(),
    )
    service._store.put_carrier_exception(forged.model_dump(mode="json"))
    problems = service.verify_exception(forged.exception_id)
    assert problems and "window" in " ".join(problems)


# -- handling acts & the refund guard ----------------------------------------


def test_handling_acts_choose_only_pack_paths():
    carrier = LocalCarrierDouble()
    service, _ = _lost_case(carrier)
    exc = service.detect_carrier_exceptions("CASE-2003", as_of=DAY_LOST)[0]
    with pytest.raises(CarrierExceptionError, match="is not offered for a"):
        service.record_exception_act(
            exc.exception_id, action="something_improvised", actor=STAFF,
            acted_on=DAY_LOST,
        )
    with pytest.raises(CarrierExceptionError, match="requires a named actor"):
        service.record_exception_act(
            exc.exception_id, action="escalate", actor=" ", acted_on=DAY_LOST
        )
    service.record_exception_act(
        exc.exception_id, action="escalate", actor=STAFF, acted_on=DAY_LOST
    )
    with pytest.raises(CarrierExceptionError, match="already escalated"):
        service.record_exception_act(
            exc.exception_id, action="escalate", actor=STAFF, acted_on=DAY_LOST
        )
    view = service.exception_view(service.get_exception(exc.exception_id))
    assert view["state"] == "escalated"
    assert set(view["path_consequences"]) == {"refund_as_received", "escalate"}


def test_refund_blocked_while_exception_open_and_lifted_on_resolution():
    carrier = LocalCarrierDouble()
    service, _ = _lost_case(carrier)
    _approve(service, "CASE-2003", "d.okafor", "m.reyes")  # 130.50 refund: approvals on record (one would do)
    exc = service.detect_carrier_exceptions("CASE-2003", as_of=DAY_LOST)[0]
    with pytest.raises(PaymentsError, match="carrier exception"):
        service.execute_refund("CASE-2003", idempotency_key="r18-lost", as_of=DAY_LOST)
    service.record_exception_act(
        exc.exception_id, action="escalate", actor=STAFF, acted_on=DAY_LOST
    )
    with pytest.raises(PaymentsError, match="escalated"):
        service.execute_refund("CASE-2003", idempotency_key="r18-lost", as_of=DAY_LOST)
    service.record_exception_act(
        exc.exception_id, action="refund_as_received", actor=STAFF,
        acted_on=DAY_LOST,
    )
    receipt = service.execute_refund("CASE-2003", idempotency_key="r18-lost", as_of=DAY_LOST)
    computation = service.get_computation("CASE-2003")
    assert receipt.amount == computation.refund_total == Decimal("130.50")
    with pytest.raises(CarrierExceptionError, match="already resolved"):
        service.record_exception_act(
            exc.exception_id, action="escalate", actor=STAFF, acted_on=DAY_LOST
        )


def test_customer_kept_resolution_bars_the_refund_permanently():
    carrier = LocalCarrierDouble()
    service, _ = _scripted_case(carrier, "never_tendered", "CASE-2001", "REQ-2001")
    exc = service.detect_carrier_exceptions("CASE-2001", as_of=date(2026, 11, 1))[0]
    service.record_exception_act(
        exc.exception_id, action="customer_kept", actor=STAFF,
        acted_on=date(2026, 11, 1),
    )
    _approve(service, "CASE-2001", "d.okafor")
    with pytest.raises(PaymentsError, match="customer-kept"):
        service.execute_refund(
            "CASE-2001", idempotency_key="r18-kept", as_of=date(2026, 11, 2)
        )
    assert service.list_receipts() == []


# -- transit damage attribution ----------------------------------------------


def _damaged_graded(carrier=None, wear=True):
    carrier = carrier or LocalCarrierDouble()
    service, _ = _scripted_case(carrier, "damaged_in_transit")
    service.record_warehouse_receipt(
        "CASE-2003", received_by="w.chen", received_on=date(2026, 10, 30)
    )
    report = service.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="damaged_by_customer",
        graded_on=date(2026, 10, 31), wear_evidence=wear,
        damage_notes="box crushed, seams torn",
    )
    service.detect_carrier_exceptions("CASE-2003", as_of=date(2026, 10, 31))
    return service, report


def test_transit_damage_is_attributed_not_scored_not_repriced():
    service, report = _damaged_graded()
    assert report.damage_attribution == "transit"
    assert report.damage_event_id and report.damage_event_id.startswith("evt-")
    # The refund stands on the declared condition: no re-run, no delta.
    assert report.new_computation_id is None and report.refund_delta is None
    assert report.adjustment_id is None
    assert service.list_adjustments("CASE-2003") == []
    # No fraud re-score: the trigger never fired.
    assert report.fraud_rescore_triggered is False and report.new_fraud_score_id is None
    # The disposition follows the pack's transit row, and verifies
    # on its own branch.
    decision = service.get_disposition("CASE-2003")
    assert decision.disposition == "RETURN_TO_VENDOR"
    assert decision.cell_id == "EX-TRANSIT-DAMAGE"
    assert service.verify_stored_disposition("CASE-2003") == []
    assert service.verify_exception(
        service.list_exceptions("CASE-2003")[0].exception_id
    ) == []
    view = service.exception_view(service.list_exceptions("CASE-2003")[0])
    assert view["state"] == "resolved" and view["resolution_path"] == "grading_attribution"


def test_same_grade_jump_without_a_carrier_event_is_r7s_cascade():
    carrier = LocalCarrierDouble()
    service = _service(carrier)
    _ingest(service, "REQ-2003")
    _issue(service, "CASE-2003")  # standard script: no damage event
    service.record_warehouse_receipt(
        "CASE-2003", received_by="w.chen", received_on=date(2026, 10, 30)
    )
    report = service.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="damaged_by_customer",
        graded_on=date(2026, 10, 31), wear_evidence=True,
        damage_notes="torn seams",
    )
    assert report.damage_attribution is None
    assert report.new_computation_id is not None  # re-priced, R7-style
    assert report.fraud_rescore_triggered is True  # re-scored, R7-style


# -- the carrier claim --------------------------------------------------------


def _lost_claim_ready():
    carrier = LocalCarrierDouble()
    service, _ = _lost_case(carrier)
    exc = service.detect_carrier_exceptions("CASE-2003", as_of=DAY_LOST)[0]
    return service, exc


def test_claim_gates_amount_and_lifecycle():
    service, exc = _lost_claim_ready()
    with pytest.raises(CarrierExceptionError, match="still stands open"):
        service.file_carrier_claim(exc.exception_id, filed_by=STAFF, filed_on=DAY_LOST)
    service.record_exception_act(
        exc.exception_id, action="escalate", actor=STAFF, acted_on=DAY_LOST
    )
    claim = service.file_carrier_claim(exc.exception_id, filed_by=STAFF, filed_on=DAY_LOST)
    # lost: item 145.00 + label 6.95, at the pack's formula.
    assert claim.claimed_amount == Decimal("151.95")
    assert claim.item_amount == Decimal("145.00") and claim.label_cost == Decimal("6.95")
    again = service.file_carrier_claim(exc.exception_id, filed_by=STAFF, filed_on=DAY_LOST)
    assert again.claim_id == claim.claim_id  # one claim per exception
    assert service.verify_carrier_claim(claim.claim_id) == []
    with pytest.raises(CarrierExceptionError, match="only from acknowledged"):
        service.record_claim_act(
            claim.claim_id, action="settled", actor=STAFF,
            acted_on=DAY_LOST, settled_amount=Decimal("151.95"),
        )
    service.record_claim_act(
        claim.claim_id, action="acknowledged", actor=STAFF, acted_on=DAY_LOST
    )
    with pytest.raises(CarrierExceptionError, match="exceeds the claimed"):
        service.record_claim_act(
            claim.claim_id, action="settled", actor=STAFF,
            acted_on=DAY_LOST, settled_amount=Decimal("300.00"),
        )
    act = service.record_claim_act(
        claim.claim_id, action="settled", actor=STAFF,
        acted_on=date(2026, 11, 12), settled_amount=Decimal("151.95"),
    )
    assert act.settled_amount == Decimal("151.95")
    view = service.claim_view(service.get_claim(claim.claim_id))
    assert view["state"] == "settled" and view["settled_amount"] == "151.95"
    assert service.verify_carrier_claim(claim.claim_id) == []


def test_claim_denial_needs_a_pack_reason():
    service, exc = _lost_claim_ready()
    service.record_exception_act(
        exc.exception_id, action="escalate", actor=STAFF, acted_on=DAY_LOST
    )
    claim = service.file_carrier_claim(exc.exception_id, filed_by=STAFF, filed_on=DAY_LOST)
    service.record_claim_act(
        claim.claim_id, action="acknowledged", actor=STAFF, acted_on=DAY_LOST
    )
    with pytest.raises(CarrierExceptionError, match="vocabulary"):
        service.record_claim_act(
            claim.claim_id, action="denied", actor=STAFF, acted_on=DAY_LOST
        )
    with pytest.raises(CarrierExceptionError, match="vocabulary"):
        service.record_claim_act(
            claim.claim_id, action="denied", actor=STAFF,
            acted_on=DAY_LOST, reason_code="not_our_problem",
        )
    service.record_claim_act(
        claim.claim_id, action="denied", actor=STAFF,
        acted_on=DAY_LOST, reason_code="carrier_liability_refused",
    )
    assert service.claim_view(service.get_claim(claim.claim_id))["state"] == "denied"
    with pytest.raises(CarrierExceptionError, match="stands denied"):
        service.record_claim_act(
            claim.claim_id, action="settled", actor=STAFF,
            acted_on=DAY_LOST, settled_amount=Decimal("151.95"),
        )


def test_damaged_claim_needs_the_attributed_grading():
    carrier = LocalCarrierDouble()
    service, _ = _scripted_case(carrier, "damaged_in_transit")
    exc = service.detect_carrier_exceptions("CASE-2003", as_of=date(2026, 10, 29))[0]
    with pytest.raises(CarrierExceptionError, match="no such report stands"):
        service.file_carrier_claim(exc.exception_id, filed_by=STAFF, filed_on=date(2026, 10, 30))
    service.record_warehouse_receipt(
        "CASE-2003", received_by="w.chen", received_on=date(2026, 10, 30)
    )
    service.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="damaged_by_customer",
        graded_on=date(2026, 10, 31), wear_evidence=True, damage_notes="crushed",
    )
    claim = service.file_carrier_claim(
        exc.exception_id, filed_by=STAFF, filed_on=date(2026, 11, 1)
    )
    # damaged: label 6.95 + 50% of item 145.00, at the pack's rate.
    assert claim.claimed_amount == Decimal("79.45")


def test_settlement_is_its_own_money_line_and_moves_no_customer_figure():
    service, exc = _lost_claim_ready()
    service.record_exception_act(
        exc.exception_id, action="escalate", actor=STAFF, acted_on=DAY_LOST
    )
    claim = service.file_carrier_claim(exc.exception_id, filed_by=STAFF, filed_on=DAY_LOST)
    service.record_claim_act(claim.claim_id, action="acknowledged", actor=STAFF, acted_on=DAY_LOST)
    service.record_claim_act(
        claim.claim_id, action="settled", actor=STAFF,
        acted_on=date(2026, 11, 12), settled_amount=Decimal("151.95"),
    )
    report = service.compute_report("money_summary", as_of=date(2026, 11, 15))
    assert report.figures["carrier_claims_settled"] == "151.95"
    assert report.figures["executed_refunds"] == "0.00"  # the customer's money, unmoved
    assert report.population["settled_claims"] == 1
    assert service.verify_management_report(report) == []


def test_verify_claim_catches_a_tampered_claim():
    service, exc = _lost_claim_ready()
    service.record_exception_act(
        exc.exception_id, action="escalate", actor=STAFF, acted_on=DAY_LOST
    )
    claim = service.file_carrier_claim(exc.exception_id, filed_by=STAFF, filed_on=DAY_LOST)
    from returns_resolution.carrier_exceptions import CarrierClaim

    # A claim asserting an amount the record does not hold —
    # written straight to the store under its own id, as a forged
    # or buggy assembly would — fails re-assembly on the amount,
    # the fingerprint and the id itself.
    forged = CarrierClaim(
        **{
            **claim.model_dump(),
            "claim_id": "CLM-forged000000000",
            "claimed_amount": Decimal("999.99"),
        }
    )
    service._store.put_carrier_claim(forged.model_dump(mode="json"))
    problems = service.verify_carrier_claim(forged.claim_id)
    assert problems and "claimed_amount" in " ".join(problems)


# -- access -------------------------------------------------------------------


def _threshold_claim():
    """The tiering rule, tested against a test pack whose
    supervisor threshold (100.00) sits below CASE-2003's lost
    claim (151.95) — the shipped pack's threshold is 250.00 and
    no planted claim reaches it, so the rule is exercised here
    against the same code with the threshold as data, exactly
    as the pack intends."""
    pack = load_exception_pack().model_copy(
        update={"claim_settlement_supervisor_threshold": Decimal("100.00")}
    )
    carrier = LocalCarrierDouble()
    service = CaseService(
        InMemoryStore(), OfflineComposer(),
        carrier_port=carrier, exception_pack=pack,
    )
    service.seed_orders()
    _ingest(service, "REQ-2003")
    label = _issue(service, "CASE-2003")
    carrier.set_script(carrier_ref=label.carrier_ref, script="stalled_in_transit")
    exc = service.detect_carrier_exceptions("CASE-2003", as_of=DAY_LOST)[0]
    service.record_exception_act(
        exc.exception_id, action="escalate", actor=STAFF, acted_on=DAY_LOST
    )
    claim = service.file_carrier_claim(exc.exception_id, filed_by=STAFF, filed_on=DAY_LOST)
    assert claim.claimed_amount == Decimal("151.95")
    service.record_claim_act(claim.claim_id, action="acknowledged", actor=STAFF, acted_on=DAY_LOST)
    return service, claim


def test_settlement_above_the_threshold_is_a_supervisor_act():
    service, claim = _threshold_claim()
    with pytest.raises(AccessDeniedError) as caught:
        service.record_claim_act_as(
            AGENT, claim.claim_id, action="settled",
            acted_on=date(2026, 11, 12), settled_amount=Decimal("151.95"),
        )
    assert "supervisor" in str(caught.value)
    act = service.record_claim_act_as(
        SUPERVISOR, claim.claim_id, action="settled",
        acted_on=date(2026, 11, 12), settled_amount=Decimal("151.95"),
    )
    assert act.actor == SUPERVISOR.id
    # The refusal is on the record, like every R16 denial.
    denials = [d for d in service.list_access_denials() if d.target == claim.claim_id]
    assert denials and denials[-1].reason == "act_not_permitted"


def test_customer_status_shape_and_claim_hiding():
    carrier = LocalCarrierDouble()
    service, _ = _lost_case(carrier)
    service.detect_carrier_exceptions("CASE-2003", as_of=DAY_LOST)
    customer = Principal(id=service.get_case("CASE-2003").customer_id, role="customer")
    status = service.get_exception_status_as(customer, "CASE-2003")
    assert len(status) == 1
    assert set(status[0]) == {"exception_id", "kind", "state", "raised_on"}
    assert status[0]["kind"] == "lost_in_transit"
    stranger = Principal(id="cust-someone-else", role="customer")
    with pytest.raises(AccessDeniedError) as caught:
        service.get_exception_status_as(stranger, "CASE-2003")
    assert caught.value.reason == "case_not_visible"
    with pytest.raises(AccessDeniedError) as caught:
        service.list_claims_as(customer, "CASE-2003")
    assert caught.value.reason == "case_not_visible"
    with pytest.raises(AccessDeniedError) as caught:
        service.record_exception_act_as(
            AUDITOR, service.list_exceptions("CASE-2003")[0].exception_id,
            action="escalate", acted_on=DAY_LOST,
        )
    assert caught.value.reason == "read_only_role"


def test_api_existence_hiding_is_byte_identical():
    carrier = LocalCarrierDouble()
    service, _ = _lost_case(carrier)
    service.detect_carrier_exceptions("CASE-2003", as_of=DAY_LOST)
    client = TestClient(create_app(service))
    customer_id = service.get_case("CASE-2003").customer_id
    headers = {"X-Principal-Id": customer_id, "X-Principal-Role": "customer"}
    own = client.get("/cases/CASE-2003/exception-status", headers=headers)
    assert own.status_code == 200 and own.json()[0]["kind"] == "lost_in_transit"
    hidden = client.get(
        "/cases/CASE-2003/exception-status",
        headers={"X-Principal-Id": "cust-other", "X-Principal-Role": "customer"},
    )
    missing = client.get("/cases/CASE-9999/exception-status", headers=headers)
    assert hidden.status_code == missing.status_code == 404
    assert hidden.content == missing.content
    claims = client.get("/cases/CASE-2003/claims", headers=headers)
    claims_missing = client.get("/cases/CASE-9999/claims", headers=headers)
    assert claims.status_code == claims_missing.status_code == 404
    assert claims.content == claims_missing.content


# -- messaging ------------------------------------------------------------------


def test_exception_notice_verifies_and_sends_without_blame_or_promises():
    carrier = LocalCarrierDouble()
    service, _ = _lost_case(carrier)
    service.detect_carrier_exceptions("CASE-2003", as_of=DAY_LOST)
    message = service.draft_message(
        "CASE-2003", "carrier_exception_notice", as_of=DAY_LOST
    )
    verified = service.verify_message(message.message_id, as_of=DAY_LOST)
    assert verified.status == "verified"
    text = verified.text.lower()
    assert "lost in transit" in text
    for term in load_exception_pack().blame_terms:
        assert term not in text
    service.queue_message(message.message_id, as_of=DAY_LOST)
    sent = service.send_message(message.message_id, as_of=DAY_LOST)
    assert sent.status == "sent"


def test_transit_grading_notice_never_blames_the_customer():
    service, _ = _damaged_graded()
    message = service.draft_message(
        "CASE-2003", "grading_outcome_notice", as_of=date(2026, 10, 31)
    )
    verified = service.verify_message(message.message_id, as_of=date(2026, 10, 31))
    assert verified.status == "verified"
    text = verified.text.lower()
    assert "damaged in transit" in text
    assert "damaged by the customer" not in text
    assert "$130.50" in verified.text  # the refund stands, stated


# -- reporting interplay ---------------------------------------------------------


def test_transit_grading_never_enters_the_fraud_truth():
    carrier = LocalCarrierDouble()
    service = _service(carrier)
    service.seed_history()
    for request in list(ReferenceFeed().fetch_requests()) + list(FRAUD_FEED_REQUESTS):
        service.ingest(request)
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez", outcome="confirmed_fraud",
        reason_code="pattern_verified",
    )
    # The R17 fixture's false negative: graded worse, with wear.
    label = _issue(service, "CASE-2001")
    service.record_warehouse_receipt(
        "CASE-2001", received_by="w.chen", received_on=date(2026, 10, 30)
    )
    service.record_grading(
        "CASE-2001", grader="p.novak", graded_condition="used",
        graded_on=date(2026, 10, 31), wear_evidence=True, received_weight_grams=100,
    )
    # The transit case: the same evidentiary shape (worse grade,
    # wear evidence) but the carrier's record carries the damage.
    label3 = _issue(service, "CASE-2003")
    carrier.set_script(carrier_ref=label3.carrier_ref, script="damaged_in_transit")
    service.record_warehouse_receipt(
        "CASE-2003", received_by="w.chen", received_on=date(2026, 10, 30)
    )
    report3 = service.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="damaged_by_customer",
        graded_on=date(2026, 10, 31), wear_evidence=True, damage_notes="crushed",
    )
    assert report3.damage_attribution == "transit"
    report = service.compute_report("fraud_outcomes", as_of=date(2026, 11, 5))
    # Exactly the R17 fixture's population: the transit case is
    # neither a truth nor a miss — it is not the customer's act.
    assert report.population["known_outcome_cases"] == 2
    assert report.figures["precision"] == "1.0000"
    assert report.figures["recall"] == "0.5000"
    assert service.verify_management_report(report) == []


# -- surfaces & the ledger -------------------------------------------------------


def test_surfaces_carry_the_round_at_parity():
    carrier = LocalCarrierDouble()
    service, _ = _lost_case(carrier)
    client = TestClient(create_app(service))
    pack = client.get("/exceptions/pack")
    assert pack.status_code == 200 and pack.json()["version"] == "2026.10.1"
    detected = client.post(
        "/cases/CASE-2003/exceptions/detect", json={"as_of": "2026-11-06"}
    )
    assert detected.status_code == 201
    exception_id = detected.json()[0]["exception_id"]
    acted = client.post(
        f"/exceptions/{exception_id}/acts",
        json={"action": "escalate", "actor": STAFF, "acted_on": "2026-11-06"},
    )
    assert acted.status_code == 201
    verified = client.get(f"/exceptions/{exception_id}/verify")
    assert verified.json()["verified"] is True
    runtime = handle_payload(
        service,
        {"action": "record_exception_act", "exception_id": exception_id,
         "exception_action": "refund_as_received", "actor": STAFF,
         "acted_on": "2026-11-07", "note": ""},
    )
    assert runtime["exception_act"]["action"] == "refund_as_received"
    runtime = handle_payload(
        service,
        {"action": "file_carrier_claim", "exception_id": exception_id,
         "filed_by": STAFF, "filed_on": "2026-11-07"},
    )
    assert runtime["claim"]["claimed_amount"] == "151.95"
    status = handle_payload(
        service,
        {"action": "get_exception_status", "case_id": "CASE-2003",
         "principal": {"id": service.get_case("CASE-2003").customer_id, "role": "customer"}},
    )
    assert set(status["exception_status"][0]) == {
        "exception_id", "kind", "state", "raised_on",
    }


def test_migration_0014_is_the_ledger_tail():
    assert MIGRATIONS[-1][0] == "0014"
    assert InMemoryStore().migrate()[-1] == "0014"
