"""R6 gates: carrier/label tools behind the gateway + case timeline.

Graded: the gateway as the only tool path (definitions listed,
unknown tools refused and recorded, every call — success or
refusal — on the record with its inputs hash); label issuance
(idempotent per case, schedule read off the pack, cost reconciled
against the computation with mismatches surfaced on the artefact);
the gates (ineligible and fraud-blocked cases refused); void as a
named act after which a fresh label supersedes; the carrier event
script read as of a date; pickups for pack-flagged bulky categories
only; the summary gaining the label reference under the figure
verifier; and the derived timeline (artefacts in order, gaps
named). Parity across service, API and Runtime dispatch, and
persistence on the SQL store.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.api import create_app
from returns_resolution.carrier import DOUBLE_CARRIER, LocalCarrierDouble
from returns_resolution.gateway import ToolError
from returns_resolution.intake import FRAUD_FEED_REQUESTS, PLANTED_REQUESTS
from returns_resolution.modelport import OfflineComposer
from returns_resolution.models import ReturnRequest
from returns_resolution.runtime_app import handle_payload
from returns_resolution.service import (
    CarrierError,
    CaseService,
    FraudHoldError,
)
from returns_resolution.store import InMemoryStore, SqlStore
from returns_resolution.verifier import verify_summary

DAY = date(2026, 10, 27)


def _service(store=None, carrier=None) -> CaseService:
    service = CaseService(store or InMemoryStore(), OfflineComposer(), carrier_port=carrier)
    service.seed_orders()
    for request in PLANTED_REQUESTS:
        service.ingest(request)
    return service


def _issue(service, case_id, on=DAY):
    return service.invoke_tool(
        "create_return_label", {"case_id": case_id, "issued_on": on},
        case_id=case_id, called_on=on,
    )


def _tent_case(service) -> str:
    """An eligible gear case: the tent order returned inside its window."""
    case = service.ingest(
        ReturnRequest(
            request_id="REQ-2010", order_id="ORD-1002", sku="TENT-2P", qty=1,
            reason_code="changed_mind", condition_declared="used",
            requested_on=date(2026, 9, 15),
        )
    )
    return case.case_id


# --- the gateway -------------------------------------------------------------

def test_tool_definitions_are_listed_with_schemas():
    service = _service()
    definitions = {d.name: d for d in service.tool_definitions()}
    assert set(definitions) == {
        "create_return_label", "void_return_label", "get_carrier_events", "book_pickup",
        # R11: the gateway generalized — refund execution and the
        # grading callback are gateway tools now too.
        "execute_refund", "submit_grading_report",
    }
    for definition in definitions.values():
        assert definition.input_schema["type"] == "object"
        assert definition.input_schema["required"]
        assert definition.output_description


def test_unknown_tool_is_refused_and_recorded():
    service = _service()
    with pytest.raises(ToolError, match="unknown tool"):
        service.invoke_tool(
            "teleport_parcel", {"case_id": "CASE-2003"}, case_id="CASE-2003", called_on=DAY
        )
    calls = service.list_tool_calls("CASE-2003")
    assert len(calls) == 1
    assert calls[0].tool == "teleport_parcel"
    assert calls[0].status == "error"
    assert "unknown tool" in calls[0].error


def test_every_call_is_recorded_with_its_inputs_hash():
    service = _service()
    label = _issue(service, "CASE-2003")
    calls = service.list_tool_calls("CASE-2003")
    assert len(calls) == 1
    call = calls[0]
    assert call.status == "ok"
    assert call.result_ref == label.label_id
    assert label.via_call_id == call.call_id
    assert len(call.inputs_hash) == 64


# --- label issuance ------------------------------------------------------------

def test_label_artefact_carries_schedule_and_reconciliation():
    carrier = LocalCarrierDouble()
    service = _service(carrier=carrier)
    label = _issue(service, "CASE-2003")
    assert label.provider == DOUBLE_CARRIER
    assert label.service_level == "ground"  # footwear, under the freight threshold
    assert label.weight_grams == 1450
    assert label.cost == Decimal("6.95")
    assert label.status == "issued"
    assert label.policy_pack_version == "2026.10.1"
    assert label.supersedes is None
    # Footwear is a free-return category: deducted $0.00, the label
    # costs $6.95 — the variance is on the artefact, classified as
    # the pack's stated intent, not silently absorbed.
    rec = label.reconciliation
    assert rec.status == "retailer_absorbed"
    assert rec.deducted_amount == Decimal("0.00")
    assert rec.label_cost == Decimal("6.95")
    assert rec.variance == Decimal("6.95")


def test_label_cost_matched_when_deduction_equals_cost():
    service = _service()
    case = service.ingest(
        ReturnRequest(
            request_id="REQ-2011", order_id="ORD-1007", sku="STOVE-PKT", qty=1,
            reason_code="changed_mind", condition_declared="used",
            requested_on=date(2026, 10, 20),
        )
    )
    label = _issue(service, case.case_id)
    assert label.service_level == "ground"
    assert label.reconciliation.status == "matched"
    assert label.reconciliation.deducted_amount == Decimal("6.95")
    assert label.reconciliation.variance == Decimal("0.00")


def test_label_variance_is_surfaced_never_hidden():
    service = _service()
    label = _issue(service, "CASE-2006")  # electronics headlamp
    assert label.service_level == "ground_tracked"
    assert label.cost == Decimal("8.50")
    rec = label.reconciliation
    assert rec.status == "variance"
    assert rec.deducted_amount == Decimal("6.95")
    assert rec.variance == Decimal("1.55")
    assert "variance" in rec.detail


def test_heavy_parcel_moves_as_freight_at_the_pack_threshold():
    service = _service()
    case_id = _tent_case(service)
    label = _issue(service, case_id, on=date(2026, 9, 16))
    assert label.weight_grams == 2400
    assert label.service_level == "freight"
    assert label.cost == Decimal("24.00")
    assert label.reconciliation.status == "variance"
    assert label.reconciliation.variance == Decimal("17.05")


def test_label_issue_is_idempotent_per_case():
    carrier = LocalCarrierDouble()
    service = _service(carrier=carrier)
    first = _issue(service, "CASE-2003")
    second = _issue(service, "CASE-2003")
    assert second.label_id == first.label_id
    assert len(service.list_labels("CASE-2003")) == 1
    assert carrier.label_count == 1  # the port issued exactly one label
    assert len(service.list_tool_calls("CASE-2003")) == 2  # both calls recorded


def test_label_refused_for_ineligible_case():
    service = _service()
    with pytest.raises(CarrierError, match="not eligible"):
        _issue(service, "CASE-2004")  # one day past its window
    assert service.list_labels("CASE-2004") == []
    calls = service.list_tool_calls("CASE-2004")
    assert len(calls) == 1 and calls[0].status == "error"


def test_label_refused_for_fraud_blocked_case_until_cleared():
    service = CaseService(InMemoryStore(), OfflineComposer())
    service.seed_orders()
    service.seed_history()
    service.ingest(FRAUD_FEED_REQUESTS[0])  # CASE-3008, FRAUD_REVIEW at 95.00
    with pytest.raises(FraudHoldError):
        _issue(service, "CASE-3008")
    assert service.list_labels("CASE-3008") == []
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez",
        outcome="cleared_with_reason", reason_code="legitimate_pattern_explained",
    )
    label = _issue(service, "CASE-3008")
    assert label.status == "issued"


def test_label_refused_for_unknown_case():
    service = _service()
    with pytest.raises(LookupError):
        _issue(service, "CASE-9999")


# --- void + re-issue -------------------------------------------------------------

def test_void_is_a_named_act_and_fresh_label_supersedes():
    carrier = LocalCarrierDouble()
    service = _service(carrier=carrier)
    label = _issue(service, "CASE-2001")
    void = service.invoke_tool(
        "void_return_label",
        {
            "case_id": "CASE-2001", "label_id": label.label_id,
            "voided_by": "d.okafor", "reason": "wrong label printed", "voided_on": DAY,
        },
        case_id="CASE-2001", called_on=DAY,
    )
    assert void.label_id == label.label_id
    assert service.get_label(label.label_id).status == "voided"
    assert service.get_active_label("CASE-2001") is None
    fresh = _issue(service, "CASE-2001", on=DAY + timedelta(days=1))
    assert fresh.label_id != label.label_id
    assert fresh.carrier_ref != label.carrier_ref
    assert fresh.supersedes == label.label_id
    assert carrier.label_count == 2


def test_void_refusals_name_their_reason():
    service = _service()
    label = _issue(service, "CASE-2001")
    with pytest.raises(CarrierError, match="named actor"):
        service.invoke_tool(
            "void_return_label",
            {"case_id": "CASE-2001", "label_id": label.label_id,
             "voided_by": " ", "reason": "x", "voided_on": DAY},
            case_id="CASE-2001", called_on=DAY,
        )
    with pytest.raises(CarrierError, match="stated reason"):
        service.invoke_tool(
            "void_return_label",
            {"case_id": "CASE-2001", "label_id": label.label_id,
             "voided_by": "d.okafor", "reason": " ", "voided_on": DAY},
            case_id="CASE-2001", called_on=DAY,
        )
    service.invoke_tool(
        "void_return_label",
        {"case_id": "CASE-2001", "label_id": label.label_id,
         "voided_by": "d.okafor", "reason": "wrong label printed", "voided_on": DAY},
        case_id="CASE-2001", called_on=DAY,
    )
    with pytest.raises(CarrierError, match="already voided"):
        service.invoke_tool(
            "void_return_label",
            {"case_id": "CASE-2001", "label_id": label.label_id,
             "voided_by": "d.okafor", "reason": "again", "voided_on": DAY},
            case_id="CASE-2001", called_on=DAY,
        )
    with pytest.raises(LookupError):
        service.invoke_tool(
            "void_return_label",
            {"case_id": "CASE-2001", "label_id": "RL-nope",
             "voided_by": "d.okafor", "reason": "x", "voided_on": DAY},
            case_id="CASE-2001", called_on=DAY,
        )


# --- carrier events --------------------------------------------------------------

def test_carrier_events_follow_the_script_as_of_a_date():
    service = _service()
    label = _issue(service, "CASE-2003")
    at_issue = service.get_carrier_events(label.label_id, DAY)
    assert [e.kind for e in at_issue] == ["tendered"]
    next_day = service.get_carrier_events(label.label_id, DAY + timedelta(days=1))
    assert [e.kind for e in next_day] == ["tendered", "in_transit"]
    by_receipt = service.get_carrier_events(label.label_id, DAY + timedelta(days=3))
    assert [e.kind for e in by_receipt] == ["tendered", "in_transit", "received"]
    assert all(e.label_id == label.label_id for e in by_receipt)
    assert all(e.carrier_ref == label.carrier_ref for e in by_receipt)


def test_voided_label_is_never_reported_received():
    service = _service()
    label = _issue(service, "CASE-2003")
    service.invoke_tool(
        "void_return_label",
        {"case_id": "CASE-2003", "label_id": label.label_id,
         "voided_by": "d.okafor", "reason": "recalled", "voided_on": DAY},
        case_id="CASE-2003", called_on=DAY,
    )
    events = service.get_carrier_events(label.label_id, DAY + timedelta(days=5))
    assert "received" not in [e.kind for e in events]


def test_events_tool_matches_the_direct_read():
    service = _service()
    label = _issue(service, "CASE-2003")
    via_tool = service.invoke_tool(
        "get_carrier_events",
        {"case_id": "CASE-2003", "label_id": label.label_id, "as_of": DAY + timedelta(days=3)},
        case_id="CASE-2003", called_on=DAY + timedelta(days=3),
    )
    direct = service.get_carrier_events(label.label_id, DAY + timedelta(days=3))
    assert [e.model_dump() for e in via_tool] == [e.model_dump() for e in direct]


# --- pickups -----------------------------------------------------------------------

def test_pickup_books_for_bulky_category_against_active_label():
    service = _service()
    case_id = _tent_case(service)
    label = _issue(service, case_id, on=date(2026, 9, 16))
    booking = service.invoke_tool(
        "book_pickup", {"case_id": case_id, "scheduled_on": date(2026, 9, 17)},
        case_id=case_id, called_on=date(2026, 9, 17),
    )
    assert booking.label_id == label.label_id
    assert booking.provider == DOUBLE_CARRIER
    assert booking.window == "08:00-12:00"  # the pack's pickup window
    assert booking.status == "booked"
    assert booking.via_call_id is not None
    assert service.list_pickups(case_id)[0].pickup_id == booking.pickup_id


def test_pickup_refused_for_non_bulky_category_naming_the_rule():
    service = _service()
    _issue(service, "CASE-2003")  # footwear — has a label, still not bulky
    with pytest.raises(CarrierError, match="not a pack-flagged bulky category"):
        service.invoke_tool(
            "book_pickup", {"case_id": "CASE-2003", "scheduled_on": DAY},
            case_id="CASE-2003", called_on=DAY,
        )
    assert service.list_pickups("CASE-2003") == []


def test_pickup_refused_without_an_active_label():
    service = _service()
    case_id = _tent_case(service)
    with pytest.raises(CarrierError, match="no active label"):
        service.invoke_tool(
            "book_pickup", {"case_id": case_id, "scheduled_on": date(2026, 9, 17)},
            case_id=case_id, called_on=date(2026, 9, 17),
        )


# --- the summary gains the label -----------------------------------------------------

def test_summary_gains_label_reference_and_cost_under_the_verifier():
    service = _service()
    before = service.get_case("CASE-2006")
    assert "$8.50" not in before.summary
    label = _issue(service, "CASE-2006")
    assert label.carrier_ref not in before.summary
    case = service.get_case("CASE-2006")
    assert label.carrier_ref in case.summary
    assert "$8.50" in case.summary  # the label's own cost, as the artefact states it
    # Verifies only WITH the label in the universe: without it, the
    # $8.50 label cost is not a sayable figure.
    assert verify_summary(case.summary, case.decision, case.computation, label) == []
    assert verify_summary(case.summary, case.decision, case.computation) != []
    # A misstated label cost fails even with the label supplied.
    tampered = case.summary.replace("$8.50", "$9.99")
    assert verify_summary(tampered, case.decision, case.computation, label) != []


def test_label_on_a_not_final_computation_still_reconciles_its_shipping_line():
    service = _service()
    probe = service.ingest(
        ReturnRequest(
            request_id="REQ-2098", order_id="ORD-1007", sku="STOVE-PKT", qty=1,
            reason_code="changed_mind", condition_declared="waterlogged",
            requested_on=date(2026, 10, 25),
        )
    )
    assert probe.computation.is_final is False  # the fee names its missing rate
    label = _issue(service, probe.case_id)
    # The shipping line IS computable (gear, flat fee) — reconciliation
    # proceeds against it even while the fee line is not computable.
    assert label.reconciliation.status == "matched"
    assert label.reconciliation.deducted_amount == Decimal("6.95")


# --- the timeline -----------------------------------------------------------------------

def test_timeline_full_walk_is_ordered_and_gapless():
    service = _service()
    label = _issue(service, "CASE-2003")
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY)
    receipt = service.execute_refund("CASE-2003", idempotency_key="r6-walk", as_of=DAY)
    view = service.get_timeline("CASE-2003", DAY + timedelta(days=3))
    stages = [e.stage for e in view.entries]
    for expected in (
        "intake", "decision", "computation", "disposition", "fraud_score",
        "label", "carrier_event", "approval", "refund_execution", "receipt",
    ):
        assert expected in stages, f"missing stage {expected}"
    assert [e.at for e in view.entries] == sorted(e.at for e in view.entries)
    assert all(e.artefact_id for e in view.entries)
    receipt_entries = [e for e in view.entries if e.stage == "receipt"]
    assert receipt_entries[0].artefact_id == receipt.receipt_id
    label_entries = [e for e in view.entries if e.stage == "label"]
    assert label_entries[0].artefact_id == label.label_id
    assert view.gaps == []


def test_timeline_names_an_executed_refund_with_no_label():
    service = _service()
    service.execute_refund("CASE-2006", idempotency_key="r6-nolabel", as_of=DAY)
    view = service.get_timeline("CASE-2006", DAY)
    assert [g.kind for g in view.gaps] == ["return_label"]
    assert "executed" in view.gaps[0].detail


def test_timeline_names_a_ship_by_passed_with_no_label():
    service = _service()
    view = service.get_timeline("CASE-2001", date(2026, 12, 1))
    assert [g.kind for g in view.gaps] == ["return_label"]
    assert "ship-by" in view.gaps[0].detail


def test_timeline_names_a_voided_label_with_no_replacement():
    service = _service()
    label = _issue(service, "CASE-2003")
    service.invoke_tool(
        "void_return_label",
        {"case_id": "CASE-2003", "label_id": label.label_id,
         "voided_by": "d.okafor", "reason": "recalled", "voided_on": DAY},
        case_id="CASE-2003", called_on=DAY,
    )
    view = service.get_timeline("CASE-2003", DAY)
    assert [g.kind for g in view.gaps] == ["return_label"]
    assert "voided" in view.gaps[0].detail
    stages = [e.stage for e in view.entries]
    assert "label" in stages and "label_void" in stages


def test_timeline_names_a_pending_fraud_review():
    service = CaseService(InMemoryStore(), OfflineComposer())
    service.seed_orders()
    service.seed_history()
    service.ingest(FRAUD_FEED_REQUESTS[0])
    view = service.get_timeline("CASE-3008", DAY)
    assert [g.kind for g in view.gaps] == ["fraud_review_decision"]


def test_timeline_of_a_denied_case_is_short_and_gapless():
    service = _service()
    view = service.get_timeline("CASE-2004", DAY)
    assert view.gaps == []
    stages = [e.stage for e in view.entries]
    assert "label" not in stages and "receipt" not in stages
    assert "decision" in stages


# --- persistence + surface parity ---------------------------------------------------------

def test_sql_store_persists_labels_voids_and_tool_calls(tmp_path):
    store = SqlStore(sqlite_path=tmp_path / "r6.db")
    applied = store.migrate()
    assert applied == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010"]
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    service.ingest(PLANTED_REQUESTS[2])  # REQ-2003
    label = _issue(service, "CASE-2003")
    reopened = CaseService(SqlStore(sqlite_path=tmp_path / "r6.db"), OfflineComposer())
    stored = reopened.get_label(label.label_id)
    assert stored is not None and stored.carrier_ref == label.carrier_ref
    assert reopened.get_active_label("CASE-2003").label_id == label.label_id
    assert len(reopened.list_tool_calls("CASE-2003")) == 1


def test_api_parity_for_labels_events_pickups_and_timeline():
    service = _service()
    client = TestClient(create_app(service))
    tools = client.get("/tools")
    assert tools.status_code == 200
    assert {t["name"] for t in tools.json()} >= {"create_return_label", "book_pickup"}

    created = client.post("/cases/CASE-2003/labels", json={"issued_on": "2026-10-27"})
    assert created.status_code == 201
    label = created.json()
    assert label["reconciliation"]["status"] == "retailer_absorbed"
    active = client.get("/cases/CASE-2003/label")
    assert active.status_code == 200 and active.json()["label_id"] == label["label_id"]
    events = client.get(
        f"/cases/CASE-2003/labels/{label['label_id']}/events", params={"as_of": "2026-10-30"}
    )
    assert events.status_code == 200
    assert [e["kind"] for e in events.json()] == ["tendered", "in_transit", "received"]

    refused = client.post("/cases/CASE-2004/labels", json={"issued_on": "2026-10-27"})
    assert refused.status_code == 409

    voided = client.post(
        f"/cases/CASE-2003/labels/{label['label_id']}/void",
        json={"voided_by": "d.okafor", "reason": "recalled", "voided_on": "2026-10-27"},
    )
    assert voided.status_code == 201
    unnamed = client.post(
        f"/cases/CASE-2003/labels/{label['label_id']}/void",
        json={"voided_by": " ", "reason": "x", "voided_on": "2026-10-27"},
    )
    assert unnamed.status_code == 422

    view = client.get("/cases/CASE-2003/timeline", params={"as_of": "2026-10-28"})
    assert view.status_code == 200
    assert [g["kind"] for g in view.json()["gaps"]] == ["return_label"]
    calls = client.get("/cases/CASE-2003/tool-calls")
    assert calls.status_code == 200 and len(calls.json()) >= 3


def test_runtime_dispatch_parity_for_tools_and_timeline():
    service = _service()
    created = handle_payload(
        service, {"action": "create_return_label", "case_id": "CASE-2003", "issued_on": "2026-10-27"}
    )
    assert created["label"]["service_level"] == "ground"
    label_id = created["label"]["label_id"]
    events = handle_payload(
        service,
        {"action": "get_carrier_events", "case_id": "CASE-2003", "label_id": label_id, "as_of": "2026-10-30"},
    )
    assert [e["kind"] for e in events["events"]] == ["tendered", "in_transit", "received"]
    refused = handle_payload(
        service, {"action": "book_pickup", "case_id": "CASE-2003", "scheduled_on": "2026-10-28"}
    )
    assert "not a pack-flagged bulky category" in refused["error"]
    generic = handle_payload(
        service,
        {
            "action": "invoke_tool", "tool": "create_return_label", "case_id": "CASE-2001",
            "arguments": {"case_id": "CASE-2001", "issued_on": "2026-10-27"},
            "called_on": "2026-10-27",
        },
    )
    assert generic["result"]["case_id"] == "CASE-2001"
    view = handle_payload(service, {"action": "get_timeline", "case_id": "CASE-2003", "as_of": "2026-10-30"})
    assert view["timeline"]["case_id"] == "CASE-2003"
    defs = handle_payload(service, {"action": "get_tool_definitions"})
    assert {d["name"] for d in defs["tools"]} >= {"void_return_label", "get_carrier_events"}
