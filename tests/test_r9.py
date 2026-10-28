"""R9 — the AgentCore Memory port: event-shaped records derived
at the case milestones, customer-scoped logged retrieval,
retention with tombstones, store/memory fraud-score parity, and
the messaging universe extended to the case's own customer's
memory — and only that customer's."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.api import create_app
from returns_resolution.intake import FRAUD_FEED_REQUESTS, PLANTED_REQUESTS
from returns_resolution.memory import (
    DOUBLE_MEMORY,
    EVENT_CASE_DECIDED,
    EVENT_CASE_GRADED,
    EVENT_FRAUD_REVIEW_CLOSED,
    EVENT_REFUND_EXECUTED,
    MemoryPack,
    load_memory_pack,
)
from returns_resolution.modelport import OfflineComposer
from returns_resolution.models import Order, OrderLine, ReturnRequest
from returns_resolution.runtime_app import handle_payload
from returns_resolution.seed import CATALOGUE
from returns_resolution.service import CaseService
from returns_resolution.store import InMemoryStore, SqlStore

DAY = date(2026, 10, 27)
RECEIVED = date(2026, 10, 30)  # the carrier's scripted received day
GRADED = date(2026, 10, 31)
LATER = date(2026, 11, 2)


def _service(memory_pack=None):
    service = CaseService(
        InMemoryStore(), OfflineComposer(), memory_pack=memory_pack
    )
    service.seed_orders()
    service.seed_history()
    return service


def _label(service, case_id, on=DAY):
    return service.invoke_tool(
        "create_return_label", {"case_id": case_id, "issued_on": on},
        case_id=case_id, called_on=on,
    )


def _execute_2003(service):
    _label(service, "CASE-2003")
    service.record_approval(
        "CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY
    )
    return service.execute_refund("CASE-2003", idempotency_key="r9-2003", as_of=DAY)


def _grade_2003(service):
    service.record_warehouse_receipt("CASE-2003", received_by="w.chen", received_on=RECEIVED)
    return service.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="used",
        graded_on=GRADED, damage_notes="heel counter scuffed",
    )


def _second_cid_case(service, requested_on=LATER):
    """A second live case for CUST-CID (the boots customer), planted
    through the store like any order of record."""
    service._store.put_order(
        Order(
            order_id="ORD-1009", customer_id="CUST-CID",
            placed_on=date(2026, 10, 26), delivered_on=date(2026, 10, 28),
            lines=[OrderLine(sku="BOOT-TRL", qty=1, unit_price=CATALOGUE["BOOT-TRL"].unit_price)],
            shipping_paid=Decimal("0.00"),
        ).model_dump(mode="json")
    )
    return service.ingest(
        ReturnRequest(
            request_id="REQ-2009", order_id="ORD-1009", sku="BOOT-TRL", qty=1,
            reason_code="changed_mind", condition_declared="new_with_tags",
            requested_on=requested_on,
        )
    )


# -- the pack + the records ---------------------------------------------------


def test_memory_pack_loader_is_fail_closed():
    pack = load_memory_pack()
    assert pack.version == "2026.10.1"
    assert pack.retention_days == 730
    with pytest.raises(ValueError):
        load_memory_pack("1999.01.1")


def test_decision_milestone_writes_an_event_record():
    service = _service()
    case = service.ingest(PLANTED_REQUESTS[0])  # CASE-2001, CUST-ANA
    records = service.retrieve_memory("CUST-ANA", as_of=DAY)
    assert len(records) == 1
    record = records[0]
    assert record.event_type == EVENT_CASE_DECIDED
    assert record.case_id == "CASE-2001"
    assert record.eligible is True
    assert record.refund_total == Decimal("189.00")
    assert record.disposition == "RESTOCK"
    assert record.fraud_band == "CLEAR"
    assert record.fact is not None and record.fact.case_id == "CASE-2001"
    assert record.provider == DOUBLE_MEMORY  # the double labels its writes
    assert record.memory_pack_version == "2026.10.1"
    assert (record.expires_on - record.occurred_on).days == 730
    assert record.source_ref == case.computation.computation_id


def test_execution_grading_and_review_milestones():
    service = _service()
    for request in PLANTED_REQUESTS:
        service.ingest(request)
    for request in FRAUD_FEED_REQUESTS:
        service.ingest(request)
    _execute_2003(service)
    _grade_2003(service)
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez",
        outcome="cleared_with_reason", reason_code="legitimate_pattern_explained",
    )

    cid = service.retrieve_memory("CUST-CID", as_of=LATER)
    by_type = {r.event_type: r for r in cid}
    assert by_type[EVENT_REFUND_EXECUTED].refund_amount == Decimal("130.50")
    graded = by_type[EVENT_CASE_GRADED]
    assert graded.graded_condition == "used"
    assert graded.refund_delta == Decimal("-14.50")
    assert graded.disposition == "OUTLET"

    ser = service.retrieve_memory("CUST-SER", as_of=LATER)
    closed = [r for r in ser if r.event_type == EVENT_FRAUD_REVIEW_CLOSED]
    assert len(closed) == 1
    assert closed[0].review_outcome == "cleared_with_reason"
    assert closed[0].fraud_band == "FRAUD_REVIEW"  # the band, never the score internals
    decided = [r for r in ser if r.event_type == EVENT_CASE_DECIDED]
    assert decided[0].fraud_band == "FRAUD_REVIEW"


def test_milestone_writes_are_idempotent():
    service = _service()
    service.sync_memory()  # backfill first, so the sync below must add none
    for request in PLANTED_REQUESTS:
        service.ingest(request)
    _execute_2003(service)
    # An idempotent replay of the execution writes no second record.
    service.execute_refund("CASE-2003", idempotency_key="r9-2003", as_of=DAY)
    executed = [
        r for r in service.retrieve_memory("CUST-CID", as_of=LATER)
        if r.event_type == EVENT_REFUND_EXECUTED
    ]
    assert len(executed) == 1
    # And a sync over milestones the hooks already wrote adds none.
    assert service.sync_memory() == 0


def test_sync_backfills_the_planted_history():
    service = CaseService(InMemoryStore(), OfflineComposer())
    service.seed_orders()
    service.seed_history()
    written = service.sync_memory()
    assert written == 9  # the seven SER + two WARD closed history cases
    assert service.sync_memory() == 0  # idempotent
    ser = service.retrieve_memory("CUST-SER", as_of=date(2026, 10, 24))
    assert len(ser) == 7
    assert all(r.event_type == EVENT_CASE_DECIDED for r in ser)
    assert all(r.fact is not None for r in ser)
    assert {r.fact.case_id for r in ser} == {
        "CASE-3001", "CASE-3002", "CASE-3003", "CASE-3004",
        "CASE-3005", "CASE-3006", "CASE-3007",
    }


# -- the limits: scoping, logging, retention -----------------------------------


def test_retrieval_is_scoped_to_one_customer():
    service = _service()
    service.sync_memory()
    for request in FRAUD_FEED_REQUESTS:
        service.ingest(request)
    ser = service.retrieve_memory("CUST-SER", as_of=LATER)
    ward = service.retrieve_memory("CUST-WARD", as_of=LATER)
    assert ser and all(r.customer_id == "CUST-SER" for r in ser)
    assert ward and all(r.customer_id == "CUST-WARD" for r in ward)
    assert not ({r.record_id for r in ser} & {r.record_id for r in ward})
    # A probe for a stranger returns nothing — and is itself logged.
    assert service.retrieve_memory("CUST-NOPE", as_of=LATER) == []
    probe = [e for e in service.list_memory_access_log("CUST-NOPE")]
    assert len(probe) == 1 and probe[0].returned_record_ids == []


def test_every_retrieval_is_logged():
    service = _service()
    service.sync_memory()
    service.ingest(FRAUD_FEED_REQUESTS[0])
    before = len(service.list_memory_access_log())
    service.retrieve_memory("CUST-SER", as_of=LATER)
    service.retrieve_memory("CUST-SER", as_of=LATER, purpose="audit")
    service.memory_retention_status("CUST-SER", as_of=LATER)
    service.score_case_from_memory("CASE-3008")
    entries = service.list_memory_access_log()[before:]
    assert [e.purpose for e in entries] == [
        "staff_read", "audit", "retention_status", "fraud_scoring",
    ]
    index = {
        row["record_id"]: row["customer_id"]
        for row in service._store.list_memory_record_index()
    }
    for entry in entries:
        assert entry.customer_id == "CUST-SER"
        assert all(index[rid] == "CUST-SER" for rid in entry.returned_record_ids)


def test_retention_expiry_and_tombstone():
    pack = MemoryPack(version="2026.10.1", retention_days=20)
    service = _service(memory_pack=pack)
    service.ingest(PLANTED_REQUESTS[2])  # CASE-2003, decided 2026-10-26
    record = service.retrieve_memory("CUST-CID", as_of=date(2026, 11, 15))
    assert len(record) == 1  # live through its expiry day
    assert service.retrieve_memory("CUST-CID", as_of=date(2026, 11, 16)) == []
    status = service.memory_retention_status("CUST-CID", as_of=date(2026, 11, 16))
    assert status["active_count"] == 0
    assert status["expired_count"] == 1
    assert status["expired_record_ids"] == [record[0].record_id]
    # Expired is not deleted: the record still stands in the store.
    index = service._store.list_memory_record_index()
    assert [row["record_id"] for row in index] == [record[0].record_id]


# -- the consumers: fraud parity + verified messaging ---------------------------


def test_score_parity_store_vs_memory():
    service = _service()
    service.sync_memory()
    for request in PLANTED_REQUESTS:
        service.ingest(request)
    for request in FRAUD_FEED_REQUESTS:
        service.ingest(request)
    for case_id, total, band in (
        ("CASE-3008", "95.00", "FRAUD_REVIEW"),
        ("CASE-3103", "36.00", "MONITOR"),
        ("CASE-2001", "0.00", "CLEAR"),
        ("CASE-2003", "0.00", "CLEAR"),
    ):
        stored = service.get_fraud_score(case_id)
        from_memory = service.score_case_from_memory(case_id)
        assert from_memory is not None
        assert stored.history_source == "store"
        assert from_memory.history_source == "memory"
        assert from_memory.score_id == stored.score_id
        assert from_memory.total_score == Decimal(total) == stored.total_score
        assert from_memory.band == band == stored.band
        assert [s.model_dump(mode="json") for s in from_memory.signals] == [
            s.model_dump(mode="json") for s in stored.signals
        ]


def test_score_parity_survives_grading_weight():
    service = _service()
    service.ingest(PLANTED_REQUESTS[0])  # CASE-2001, the jacket
    _label(service, "CASE-2001")
    service.record_warehouse_receipt("CASE-2001", received_by="w.chen", received_on=RECEIVED)
    service.record_grading(
        "CASE-2001", grader="p.novak", graded_condition="like_new",
        graded_on=GRADED, received_weight_grams=800,
    )
    rescored = service.rescore_case("CASE-2001")
    from_memory = service.score_case_from_memory("CASE-2001")
    # The graded weigh-in (800g against the 820g master) is the
    # weight of record on BOTH paths: same signals, same artefact.
    assert from_memory.score_id == rescored.score_id
    weight = next(s for s in from_memory.signals if s.signal_id == "weight_mismatch")
    assert weight.status == "computed"
    assert weight.inputs["returned_weight_grams"] == "800"


def test_score_from_memory_without_records_returns_none():
    service = _service()
    # CASE-3001 stands in the store as planted history, but no sync
    # has run: memory holds no decision record for it.
    assert service.score_case_from_memory("CASE-3001") is None
    assert service.score_case_from_memory("CASE-9999") is None


def _executed_ser_refund(service):
    service.ingest(FRAUD_FEED_REQUESTS[0])  # CASE-3008
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez",
        outcome="cleared_with_reason", reason_code="legitimate_pattern_explained",
    )
    service.record_approval(
        "CASE-3008", approver="d.okafor", role="returns_lead", as_of=DAY
    )
    return service.execute_refund("CASE-3008", idempotency_key="r9-3008", as_of=DAY)


def test_message_may_quote_own_customers_prior_refund():
    service = _service()
    service.ingest(PLANTED_REQUESTS[2])  # CASE-2003
    _execute_2003(service)  # $130.50 moved, on memory's record
    _second_cid_case(service)
    draft = service.draft_message(
        "CASE-2009", "return_summary", as_of=LATER,
        model_note="As with your earlier return, the refund of $130.50 was "
                   "sent to your original payment method.",
    )
    verified = service.verify_message(draft.message_id, as_of=LATER)
    assert verified.status == "verified", verified.violations


def test_message_quoting_another_customers_amount_is_refused():
    service = _service()
    receipt = _executed_ser_refund(service)
    assert receipt.amount == Decimal("116.00")
    ser_amounts = {
        r.refund_amount
        for r in service.retrieve_memory("CUST-SER", as_of=LATER)
        if r.refund_amount is not None
    }
    assert Decimal("116.00") in ser_amounts  # the figure exists in memory
    service.ingest(PLANTED_REQUESTS[2])
    _second_cid_case(service)
    draft = service.draft_message(
        "CASE-2009", "return_summary", as_of=LATER,
        model_note="Your earlier refund of $116.00 has been processed.",
    )
    refused = service.verify_message(draft.message_id, as_of=LATER)
    assert refused.status == "refused"
    assert any("116.00" in v for v in refused.violations)


def test_expired_memory_figure_is_refused_in_messages():
    pack = MemoryPack(version="2026.10.1", retention_days=20)
    service = _service(memory_pack=pack)
    service.ingest(PLANTED_REQUESTS[2])
    _execute_2003(service)
    _second_cid_case(service)
    note = "As with your earlier return, the refund of $130.50 was sent on."
    live = service.draft_message("CASE-2009", "return_summary", as_of=LATER, model_note=note)
    assert service.verify_message(live.message_id, as_of=LATER).status == "verified"
    stale = service.draft_message(
        "CASE-2009", "return_summary", as_of=date(2026, 12, 5), model_note=note
    )
    refused = service.verify_message(stale.message_id, as_of=date(2026, 12, 5))
    assert refused.status == "refused"
    assert any("130.50" in v for v in refused.violations)


# -- persistence + surfaces -----------------------------------------------------


def test_memory_persists_through_the_sql_store(tmp_path):
    store = SqlStore(sqlite_path=tmp_path / "r9.db")
    assert "0009" in store.migrate()
    first = CaseService(store, OfflineComposer())
    first.seed_orders()
    first.ingest(PLANTED_REQUESTS[0])
    second = CaseService(store, OfflineComposer())
    records = second.retrieve_memory("CUST-ANA", as_of=DAY)
    assert len(records) == 1 and records[0].event_type == EVENT_CASE_DECIDED
    from_memory = second.score_case_from_memory("CASE-2001")
    assert from_memory is not None and from_memory.score_id == second.get_fraud_score("CASE-2001").score_id


def test_api_and_runtime_parity():
    service = _service()
    service.ingest(PLANTED_REQUESTS[0])
    client = TestClient(create_app(service))
    config = client.get("/memory/config")
    assert config.status_code == 200
    assert config.json()["retention_days"] == 730
    assert client.post("/memory/sync").json() == {"records_written": 9}
    records = client.get("/customers/CUST-ANA/memory").json()
    assert len(records) == 1 and records[0]["event_type"] == EVENT_CASE_DECIDED
    retention = client.get("/customers/CUST-ANA/memory/retention").json()
    assert retention["active_count"] == 1 and retention["expired_count"] == 0
    log = client.get("/customers/CUST-ANA/memory/access-log").json()
    assert log and all(e["customer_id"] == "CUST-ANA" for e in log)
    assert client.get("/customers/CUST-NOPE/memory").status_code == 404
    scored = client.get("/cases/CASE-2001/fraud-score/from-memory")
    assert scored.status_code == 200
    assert scored.json()["history_source"] == "memory"
    assert scored.json()["score_id"] == service.get_fraud_score("CASE-2001").score_id

    out = handle_payload(service, {"action": "get_customer_memory", "customer_id": "CUST-ANA"})
    assert len(out["records"]) == 1
    out = handle_payload(service, {"action": "score_case_from_memory", "case_id": "CASE-2001"})
    assert out["fraud_score"]["history_source"] == "memory"
    out = handle_payload(service, {"action": "list_memory_access_log", "customer_id": "CUST-ANA"})
    assert out["access_log"]
    out = handle_payload(service, {"action": "get_memory_retention", "customer_id": "CUST-ANA"})
    assert out["retention"]["active_count"] == 1
