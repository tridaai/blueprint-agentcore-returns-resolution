"""R7 gates: warehouse grading + the supersession cascade.

Graded: the warehouse receipt act (the carrier's received event
governs, a named receiver, one receipt per parcel); the grading
act's gates (a receipt of record first, separation of duties
against the receiver, the condition vocabulary, one report per
case, and a held case that does not finalise without BOTH of its
inspection facts); the cascade in its stated order — disposition
superseded on the graded basis, the computation re-run over the
graded condition with the delta on the report, the summary
re-issued and verified, the superseded artefacts byte-identical;
the inspection resolution for held electronics (pass+verified
refurbishes, anything else recycles); the executed-then-graded
orderings — an over-refund becomes a recorded recovery position
with no clawback surface, an under-refund settles only through
execute_top_up with approvals re-checked against the NEW
computation and keys in the topup: domain; and the fraud re-score
trigger (wildly worse + wear evidence) with the warehouse weigh-in
as the weight of record. Parity across service, API and Runtime
dispatch, and persistence on the SQL store.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.api import create_app
from returns_resolution.intake import PLANTED_REQUESTS
from returns_resolution.modelport import OfflineComposer
from returns_resolution.models import ReturnRequest
from returns_resolution.payments import LocalPaymentsDouble
from returns_resolution.runtime_app import handle_payload
from returns_resolution.service import (
    ApprovalError,
    CaseService,
    DuplicateRefundError,
    GradingError,
    PaymentsError,
)
from returns_resolution.store import InMemoryStore, SqlStore

DAY = date(2026, 10, 27)  # label issue day; the carrier script receives at +3d
RECEIVED_ON = date(2026, 10, 30)
GRADED_ON = date(2026, 10, 31)
RECEIVER = "w.chen"
GRADER = "p.novak"


def _service(store=None, payments=None) -> CaseService:
    service = CaseService(
        store or InMemoryStore(), OfflineComposer(), payments_port=payments
    )
    service.seed_orders()
    return service


def _ingest(service, *request_ids):
    by_id = {r.request_id: r for r in PLANTED_REQUESTS}
    for request_id in request_ids:
        service.ingest(by_id[request_id])


def _issue(service, case_id, on=DAY):
    return service.invoke_tool(
        "create_return_label", {"case_id": case_id, "issued_on": on},
        case_id=case_id, called_on=on,
    )


def _receive(service, case_id, by=RECEIVER, on=RECEIVED_ON):
    return service.record_warehouse_receipt(case_id, received_by=by, received_on=on)


def _parcel_in(service, case_id):
    """Label issued, parcel received into the warehouse."""
    _issue(service, case_id)
    return _receive(service, case_id)


# ---------------------------------------------------------------------------
# The warehouse receipt act
# ---------------------------------------------------------------------------

def test_warehouse_receipt_requires_the_carriers_received_event():
    service = _service()
    _ingest(service, "REQ-2003")
    with pytest.raises(GradingError, match="no return label of record"):
        _receive(service, "CASE-2003")
    _issue(service, "CASE-2003")
    # +1 day: the script shows tendered + in transit, not received.
    with pytest.raises(GradingError, match="has not recorded receipt"):
        _receive(service, "CASE-2003", on=date(2026, 10, 28))
    receipt = _receive(service, "CASE-2003")
    assert receipt.received_by == RECEIVER and receipt.received_on == RECEIVED_ON
    assert receipt.label_id == service.get_label_for_case("CASE-2003").label_id


def test_warehouse_receipt_gates():
    service = _service()
    _ingest(service, "REQ-2003", "REQ-2004")
    _issue(service, "CASE-2003")
    with pytest.raises(GradingError, match="named receiver"):
        _receive(service, "CASE-2003", by="  ")
    with pytest.raises(GradingError, match="no eligible return"):
        _receive(service, "CASE-2004")
    receipt = _receive(service, "CASE-2003")
    # The same act replays to the same artefact; a different act refuses.
    assert _receive(service, "CASE-2003").warehouse_receipt_id == receipt.warehouse_receipt_id
    with pytest.raises(GradingError, match="already has a warehouse receipt"):
        _receive(service, "CASE-2003", by="g.osei")


# ---------------------------------------------------------------------------
# The grading gates
# ---------------------------------------------------------------------------

def test_grading_requires_a_warehouse_receipt_first():
    service = _service()
    _ingest(service, "REQ-2003")
    _issue(service, "CASE-2003")
    with pytest.raises(GradingError, match="no warehouse receipt of record"):
        service.record_grading(
            "CASE-2003", grader=GRADER, graded_condition="used", graded_on=GRADED_ON
        )


def test_grading_separation_of_duties_and_vocabulary():
    service = _service()
    _ingest(service, "REQ-2003")
    _parcel_in(service, "CASE-2003")
    with pytest.raises(GradingError, match="named grader"):
        service.record_grading(
            "CASE-2003", grader=" ", graded_condition="used", graded_on=GRADED_ON
        )
    with pytest.raises(GradingError, match="separation of duties"):
        service.record_grading(
            "CASE-2003", grader=RECEIVER, graded_condition="used", graded_on=GRADED_ON
        )
    with pytest.raises(GradingError, match="not in the grading vocabulary"):
        service.record_grading(
            "CASE-2003", grader=GRADER, graded_condition="shabby", graded_on=GRADED_ON
        )
    with pytest.raises(GradingError, match="not a recorded outcome"):
        service.record_grading(
            "CASE-2003", grader=GRADER, graded_condition="used", graded_on=GRADED_ON,
            functional_test_result="mostly",
        )


def test_grading_is_a_single_act_and_ineligible_cases_refuse():
    service = _service()
    _ingest(service, "REQ-2003", "REQ-2004")
    with pytest.raises(GradingError, match="not an eligible return"):
        service.record_grading(
            "CASE-2004", grader=GRADER, graded_condition="used", graded_on=GRADED_ON
        )
    _parcel_in(service, "CASE-2003")
    service.record_grading(
        "CASE-2003", grader=GRADER, graded_condition="used", graded_on=GRADED_ON
    )
    with pytest.raises(GradingError, match="already has a finalised grading report"):
        service.record_grading(
            "CASE-2003", grader="g.osei", graded_condition="like_new", graded_on=GRADED_ON
        )


# ---------------------------------------------------------------------------
# The cascade: graded worse, money not yet executed
# ---------------------------------------------------------------------------

def test_cascade_graded_worse_supersedes_and_reprices_exactly():
    service = _service()
    _ingest(service, "REQ-2003")
    old_computation = service.get_computation("CASE-2003")
    old_disposition = service.get_disposition("CASE-2003")
    assert old_computation.refund_total == Decimal("130.50")
    assert old_disposition.disposition == "RESTOCK" and old_disposition.basis == "declared"
    store = service._store
    old_computation_payload = store.get_computation("CASE-2003")
    old_disposition_payload = store.get_disposition("CASE-2003")
    _parcel_in(service, "CASE-2003")

    report = service.record_grading(
        "CASE-2003", grader=GRADER, graded_condition="used", graded_on=GRADED_ON,
        damage_notes="heel counter scuffed, laces worn",
    )

    # The report records the whole cascade by artefact id.
    assert report.declared_condition == "like_new" and report.graded_condition == "used"
    assert report.refund_delta == Decimal("-14.50")
    assert report.superseded_computation_id == old_computation.computation_id
    assert report.superseded_disposition_id == old_disposition.disposition_id
    assert report.adjustment_id is None  # nothing executed yet: no position

    # Step 2: the computation re-run over the graded condition.
    new_computation = service.get_computation("CASE-2003")
    assert new_computation.computation_id == report.new_computation_id
    assert new_computation.refund_total == Decimal("116.00")  # 145 − 20% fee
    assert new_computation.condition_basis == "graded"
    assert new_computation.graded_condition == "used"
    assert new_computation.supersedes == old_computation.computation_id
    fee = next(ln for ln in new_computation.lines if ln.line_id == "restocking_fee")
    assert fee.amount == Decimal("29.00")
    assert fee.inputs["condition_declared"] == "like_new"
    assert fee.inputs["graded_condition"] == "used"

    # Step 1: the disposition superseded on the graded basis.
    new_disposition = service.get_disposition("CASE-2003")
    assert new_disposition.disposition_id == report.new_disposition_id
    assert new_disposition.disposition == "OUTLET"  # footwear used, season band
    assert new_disposition.cell_id == "M-FTW-USED-SEASON"
    assert new_disposition.basis == "graded"
    assert new_disposition.graded_condition == "used"
    assert new_disposition.condition_declared == "like_new"
    assert new_disposition.supersedes == old_disposition.disposition_id

    # Chains read oldest-first; the case record follows the heads.
    assert [c.computation_id for c in service.list_computations_for_case("CASE-2003")] == [
        old_computation.computation_id, new_computation.computation_id,
    ]
    assert [d.disposition_id for d in service.list_dispositions_for_case("CASE-2003")] == [
        old_disposition.disposition_id, new_disposition.disposition_id,
    ]
    case = service.get_case("CASE-2003")
    assert case.computation.computation_id == new_computation.computation_id
    assert case.disposition.disposition_id == new_disposition.disposition_id

    # Step 4: the summary was re-issued against the new computation.
    assert "$116.00" in case.summary and "$29.00" in case.summary
    assert "$130.50" not in case.summary

    # The superseded artefacts are byte-identical, and both heads verify.
    chain_payloads = {
        p["computation_id"]: p for p in store.list_computations_for_case("CASE-2003")
    }
    assert chain_payloads[old_computation.computation_id] == old_computation_payload
    disp_payloads = {
        p["disposition_id"]: p for p in store.list_dispositions_for_case("CASE-2003")
    }
    assert disp_payloads[old_disposition.disposition_id] == old_disposition_payload
    assert service.verify_stored_computation("CASE-2003") == []
    assert service.verify_stored_disposition("CASE-2003") == []
    assert service.list_adjustments("CASE-2003") == []


def test_grading_matching_the_declaration_changes_nothing():
    service = _service()
    _ingest(service, "REQ-2001")
    _parcel_in(service, "CASE-2001")
    report = service.record_grading(
        "CASE-2001", grader=GRADER, graded_condition="new_with_tags", graded_on=GRADED_ON
    )
    assert report.new_computation_id is None and report.new_disposition_id is None
    assert report.refund_delta is None and report.adjustment_id is None
    assert len(service.list_computations_for_case("CASE-2001")) == 1
    assert len(service.list_dispositions_for_case("CASE-2001")) == 1


def test_grading_damaged_names_the_uncomputable_fee():
    service = _service()
    _ingest(service, "REQ-2003")
    _parcel_in(service, "CASE-2003")
    report = service.record_grading(
        "CASE-2003", grader=GRADER, graded_condition="damaged_by_customer",
        graded_on=GRADED_ON, damage_notes="sole split from upper",
    )
    new_computation = service.get_computation("CASE-2003")
    assert new_computation.is_final is False and new_computation.refund_total is None
    named = {c.missing_input for c in new_computation.not_computable}
    assert "restocking_fee_rate[condition=damaged_by_customer]" in named
    assert report.refund_delta is None  # no delta against an uncomputable total
    new_disposition = service.get_disposition("CASE-2003")
    assert new_disposition.status == "not_decidable"
    assert new_disposition.missing_dimension == "condition_declared"
    assert new_disposition.basis == "graded"
    with pytest.raises(PaymentsError, match="not final"):
        service.execute_refund("CASE-2003", idempotency_key="r7-damaged", as_of=GRADED_ON)


# ---------------------------------------------------------------------------
# Held electronics: the inspection unknowns are consumed by name
# ---------------------------------------------------------------------------

def test_held_case_does_not_finalise_without_its_inspection_facts():
    service = _service()
    _ingest(service, "REQ-2006")
    held = service.get_disposition("CASE-2006")
    assert held.disposition == "HOLD_FOR_INSPECTION"
    assert held.inspection_unknowns == ["functional_test_result", "data_wipe_status"]
    _parcel_in(service, "CASE-2006")
    with pytest.raises(GradingError, match="functional_test_result"):
        service.record_grading(
            "CASE-2006", grader=GRADER, graded_condition="used", graded_on=GRADED_ON
        )
    with pytest.raises(GradingError, match="data_wipe_status"):
        service.record_grading(
            "CASE-2006", grader=GRADER, graded_condition="used", graded_on=GRADED_ON,
            functional_test_result="pass",
        )
    assert service.list_grading_reports("CASE-2006") == []  # nothing finalised


def test_held_case_resolves_refurbish_on_pass_and_verified_wipe():
    service = _service()
    _ingest(service, "REQ-2006")
    held = service.get_disposition("CASE-2006")
    _parcel_in(service, "CASE-2006")
    report = service.record_grading(
        "CASE-2006", grader=GRADER, graded_condition="used", graded_on=GRADED_ON,
        functional_test_result="pass", data_wipe_status="verified",
    )
    successor = service.get_disposition("CASE-2006")
    assert successor.disposition_id == report.new_disposition_id
    assert successor.disposition == "REFURBISH" and successor.channel == "store"
    assert successor.cell_id == "GR-ELEC-PASS-WIPED"
    assert successor.basis == "graded" and successor.supersedes == held.disposition_id
    assert successor.inspection_unknowns == []
    # graded == declared: the money does not move.
    assert report.new_computation_id is None and report.refund_delta is None
    assert service.verify_stored_disposition("CASE-2006") == []


@pytest.mark.parametrize(
    "functional,wipe,cell",
    [
        ("fail", "verified", "GR-ELEC-FAIL"),
        ("fail", "not_verified", "GR-ELEC-FAIL"),
        ("pass", "not_verified", "GR-ELEC-PASS-UNWIPED"),
    ],
)
def test_held_case_recycles_when_not_resalable(functional, wipe, cell):
    service = _service()
    _ingest(service, "REQ-2006")
    _parcel_in(service, "CASE-2006")
    service.record_grading(
        "CASE-2006", grader=GRADER, graded_condition="used", graded_on=GRADED_ON,
        functional_test_result=functional, data_wipe_status=wipe,
    )
    successor = service.get_disposition("CASE-2006")
    assert successor.disposition == "RECYCLE" and successor.channel == "recycler"
    assert successor.cell_id == cell
    assert service.verify_stored_disposition("CASE-2006") == []


def test_held_case_graded_better_resolves_by_the_matrix_and_reprices():
    service = _service()
    _ingest(service, "REQ-2006")
    _parcel_in(service, "CASE-2006")
    # A held case's grading must record the inspection facts even when
    # the graded condition itself resolves through the matrix.
    with pytest.raises(GradingError, match="does not finalise"):
        service.record_grading(
            "CASE-2006", grader=GRADER, graded_condition="like_new", graded_on=GRADED_ON
        )
    report = service.record_grading(
        "CASE-2006", grader=GRADER, graded_condition="like_new", graded_on=GRADED_ON,
        functional_test_result="pass", data_wipe_status="verified",
    )
    successor = service.get_disposition("CASE-2006")
    assert successor.disposition == "REFURBISH" and successor.cell_id == "M-ELEC-LN-FRESH"
    assert report.refund_delta == Decimal("4.50")  # fee 20% → 10% on $45
    assert service.get_computation("CASE-2006").refund_total == Decimal("33.55")


# ---------------------------------------------------------------------------
# Executed, then graded: the adjustment positions
# ---------------------------------------------------------------------------

def _top_up_case(service) -> str:
    """CASE-2099: boots declared used, executed at $116.00."""
    service.ingest(
        ReturnRequest(
            request_id="REQ-2099", order_id="ORD-1003", sku="BOOT-TRL", qty=1,
            reason_code="wrong_size", condition_declared="used",
            requested_on=date(2026, 10, 26),
        )
    )
    service.record_approval(
        "CASE-2099", approver="d.okafor", role="returns_lead", as_of=DAY
    )
    receipt = service.execute_refund(
        "CASE-2099", idempotency_key="r7-topup-orig", as_of=DAY
    )
    assert receipt.amount == Decimal("116.00")
    return "CASE-2099"


def test_executed_then_graded_better_top_up_through_the_same_gates():
    double = LocalPaymentsDouble()
    service = _service(payments=double)
    _top_up_case(service)
    _parcel_in(service, "CASE-2099")
    report = service.record_grading(
        "CASE-2099", grader=GRADER, graded_condition="like_new", graded_on=GRADED_ON
    )
    assert report.refund_delta == Decimal("14.50")
    (adjustment,) = service.list_adjustments("CASE-2099")
    assert adjustment.kind == "top_up_due" and adjustment.amount == Decimal("14.50")
    assert adjustment.status == "open"
    assert adjustment.source_receipt_id is not None

    # The original approver approved $116.00, not $130.50: the gate is
    # re-checked against the NEW computation and refuses.
    with pytest.raises(ApprovalError, match="approval gate unmet for the top-up"):
        service.execute_top_up("CASE-2099", idempotency_key="tp-1", as_of=GRADED_ON)
    # And the full computation can never simply be executed again.
    service.record_approval(
        "CASE-2099", approver="m.osei", role="finance_manager", as_of=GRADED_ON
    )
    with pytest.raises(PaymentsError, match="already executed against a superseded"):
        service.execute_refund("CASE-2099", idempotency_key="r7-full", as_of=GRADED_ON)

    receipt = service.execute_top_up("CASE-2099", idempotency_key="tp-1", as_of=GRADED_ON)
    assert receipt.amount == Decimal("14.50")
    assert receipt.idempotency_key == "topup:tp-1"  # the top-up key domain
    assert double.movement_count == 2  # the original + exactly one top-up
    settled = service.get_adjustment(adjustment.adjustment_id)
    assert settled.status == "executed" and settled.top_up_receipt_id == receipt.receipt_id

    # Replay returns the original receipt; a fresh key is a duplicate.
    again = service.execute_top_up("CASE-2099", idempotency_key="tp-1", as_of=GRADED_ON)
    assert again.receipt_id == receipt.receipt_id and double.movement_count == 2
    with pytest.raises(DuplicateRefundError, match="already executed"):
        service.execute_top_up("CASE-2099", idempotency_key="tp-2", as_of=GRADED_ON)


def test_executed_then_graded_worse_records_a_recovery_position():
    double = LocalPaymentsDouble()
    service = _service(payments=double)
    _ingest(service, "REQ-2003")
    service.record_approval(
        "CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY
    )
    original = service.execute_refund("CASE-2003", idempotency_key="r7-rec", as_of=DAY)
    _parcel_in(service, "CASE-2003")
    report = service.record_grading(
        "CASE-2003", grader=GRADER, graded_condition="used", graded_on=GRADED_ON
    )
    (adjustment,) = service.list_adjustments("CASE-2003")
    assert adjustment.adjustment_id == report.adjustment_id
    assert adjustment.kind == "recovery_due" and adjustment.amount == Decimal("14.50")
    assert adjustment.status == "open"
    assert adjustment.source_receipt_id == original.receipt_id
    assert "no clawback execution" in adjustment.approval_note

    # No surface moves the recovery: the top-up path refuses (the
    # position is not a top-up) and full re-execution refuses.
    with pytest.raises(PaymentsError, match="no open top-up adjustment"):
        service.execute_top_up("CASE-2003", idempotency_key="tp-x", as_of=GRADED_ON)
    service.record_approval(
        "CASE-2003", approver="m.osei", role="finance_manager", as_of=GRADED_ON
    )
    with pytest.raises(PaymentsError, match="already executed against a superseded"):
        service.execute_refund("CASE-2003", idempotency_key="r7-again", as_of=GRADED_ON)
    # The original key still replays to the original receipt — the
    # money moved exactly once, however the case was later graded.
    replay = service.execute_refund("CASE-2003", idempotency_key="r7-rec", as_of=GRADED_ON)
    assert replay.receipt_id == original.receipt_id and double.movement_count == 1
    assert service.get_adjustment(adjustment.adjustment_id).status == "open"


def test_top_up_provider_failure_leaves_the_adjustment_open():
    double = LocalPaymentsDouble(fail_times={"topup:ft-1": 1})
    service = _service(payments=double)
    _top_up_case(service)
    _parcel_in(service, "CASE-2099")
    service.record_grading(
        "CASE-2099", grader=GRADER, graded_condition="like_new", graded_on=GRADED_ON
    )
    service.record_approval(
        "CASE-2099", approver="m.osei", role="finance_manager", as_of=GRADED_ON
    )
    with pytest.raises(PaymentsError, match="stays open"):
        service.execute_top_up("CASE-2099", idempotency_key="ft-1", as_of=GRADED_ON)
    (adjustment,) = service.list_adjustments("CASE-2099")
    assert adjustment.status == "open"
    assert service.get_receipt("CASE-2099").amount == Decimal("116.00")  # original only
    receipt = service.execute_top_up("CASE-2099", idempotency_key="ft-1", as_of=GRADED_ON)
    assert receipt.amount == Decimal("14.50") and double.movement_count == 2


# ---------------------------------------------------------------------------
# The fraud re-score trigger + the warehouse weigh-in
# ---------------------------------------------------------------------------

def test_wildly_worse_with_wear_evidence_rescores_on_the_warehouse_weight():
    service = _service()
    _ingest(service, "REQ-2001")
    before = service.get_fraud_score("CASE-2001")
    assert before.total_score == Decimal("0.00") and before.band == "CLEAR"
    _parcel_in(service, "CASE-2001")
    report = service.record_grading(
        "CASE-2001", grader=GRADER, graded_condition="used", graded_on=GRADED_ON,
        damage_notes="worn at a festival, tags in the pocket",
        received_weight_grams=100, wear_evidence=True,
    )
    assert report.fraud_rescore_triggered is True
    after = service.get_fraud_score("CASE-2001")
    assert report.new_fraud_score_id == after.score_id
    assert after.supersedes == before.score_id and after.score_id != before.score_id
    # 100 g against an 820 g jacket: the empty-box band, 30 points.
    weight = next(s for s in after.signals if s.signal_id == "weight_mismatch")
    assert weight.status == "computed" and weight.contribution == Decimal("30.00")
    assert weight.inputs["returned_weight_grams"] == "100"
    assert after.total_score == Decimal("30.00") and after.band == "MONITOR"
    assert service.get_case("CASE-2001").fraud_state == "monitor"
    assert service.verify_stored_fraud_score("CASE-2001") == []


def test_no_rescore_without_the_full_trigger():
    service = _service()
    _ingest(service, "REQ-2003")
    before = service.get_fraud_score("CASE-2003")
    _parcel_in(service, "CASE-2003")
    # like_new → used is one rank step (not "wildly worse"), even with
    # wear evidence recorded: the trigger does not fire.
    report = service.record_grading(
        "CASE-2003", grader=GRADER, graded_condition="used", graded_on=GRADED_ON,
        received_weight_grams=1450, wear_evidence=True,
    )
    assert report.fraud_rescore_triggered is False
    assert service.get_fraud_score("CASE-2003").score_id == before.score_id


# ---------------------------------------------------------------------------
# Timeline + persistence + surface parity
# ---------------------------------------------------------------------------

def test_timeline_shows_the_grading_and_names_an_ungraded_parcel():
    service = _service()
    _ingest(service, "REQ-2003")
    _parcel_in(service, "CASE-2003")
    view = service.get_timeline("CASE-2003", GRADED_ON)
    assert [g.kind for g in view.gaps] == ["warehouse_grading"]
    service.record_grading(
        "CASE-2003", grader=GRADER, graded_condition="used", graded_on=GRADED_ON
    )
    view = service.get_timeline("CASE-2003", GRADED_ON)
    stages = [e.stage for e in view.entries]
    assert stages.count("computation") == 2 and stages.count("disposition") == 2
    for expected in ("warehouse_receipt", "grading"):
        assert expected in stages
    assert view.gaps == []
    grading_entry = next(e for e in view.entries if e.stage == "grading")
    assert "declared like_new" in grading_entry.detail and "graded used" in grading_entry.detail


def test_sql_store_persists_the_whole_cascade(tmp_path):
    path = tmp_path / "r7.db"
    store = SqlStore(sqlite_path=path)
    applied = store.migrate()
    assert "0007" in applied
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    _ingest(service, "REQ-2003")
    _parcel_in(service, "CASE-2003")
    report = service.record_grading(
        "CASE-2003", grader=GRADER, graded_condition="used", graded_on=GRADED_ON
    )
    reopened = CaseService(SqlStore(sqlite_path=path), OfflineComposer())
    assert len(reopened.list_computations_for_case("CASE-2003")) == 2
    assert len(reopened.list_dispositions_for_case("CASE-2003")) == 2
    assert reopened.get_computation("CASE-2003").refund_total == Decimal("116.00")
    assert reopened.get_grading_report("CASE-2003").report_id == report.report_id
    assert reopened.get_warehouse_receipt("CASE-2003").received_by == RECEIVER
    assert reopened.verify_stored_computation("CASE-2003") == []
    assert reopened.verify_stored_disposition("CASE-2003") == []


def test_api_and_runtime_parity_for_grading():
    service = _service()
    _ingest(service, "REQ-2003")
    _issue(service, "CASE-2003")
    client = TestClient(create_app(service))
    resp = client.post(
        "/cases/CASE-2003/warehouse-receipts",
        json={"received_by": RECEIVER, "received_on": str(RECEIVED_ON)},
    )
    assert resp.status_code == 201
    resp = client.post(
        "/cases/CASE-2003/grading-reports",
        json={
            "grader": GRADER, "graded_condition": "used", "graded_on": str(GRADED_ON),
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["refund_delta"] == "-14.50" and body["graded_condition"] == "used"
    resp = client.post(
        "/cases/CASE-2003/grading-reports",
        json={"grader": "g.osei", "graded_condition": "used", "graded_on": str(GRADED_ON)},
    )
    assert resp.status_code == 409  # grading is a single act
    assert client.get("/cases/CASE-2003/grading-reports").json()[0]["report_id"] == body["report_id"]
    assert client.get("/cases/CASE-2003/adjustments").json() == []

    runtime_service = _service()
    _ingest(runtime_service, "REQ-2003")
    _issue(runtime_service, "CASE-2003")
    out = handle_payload(
        runtime_service,
        {
            "action": "record_warehouse_receipt", "case_id": "CASE-2003",
            "received_by": RECEIVER, "received_on": str(RECEIVED_ON),
        },
    )
    assert "warehouse_receipt" in out
    out = handle_payload(
        runtime_service,
        {
            "action": "record_grading", "case_id": "CASE-2003", "grader": GRADER,
            "graded_condition": "used", "graded_on": str(GRADED_ON),
        },
    )
    assert out["grading_report"]["report_id"] == body["report_id"]
    out = handle_payload(
        runtime_service, {"action": "list_adjustments", "case_id": "CASE-2003"}
    )
    assert out["adjustments"] == []
    out = handle_payload(
        runtime_service,
        {
            "action": "execute_top_up", "case_id": "CASE-2003",
            "idempotency_key": "tp-api", "as_of": str(GRADED_ON),
        },
    )
    assert "error" in out and "no open top-up adjustment" in out["error"]
