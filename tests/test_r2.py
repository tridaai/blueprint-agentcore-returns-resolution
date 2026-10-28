"""R2 gates: the refund computation engine — hand-computed fixtures to
the cent, tamper detection, not-computable honesty (never a silent
zero), tender allocation, and the summary/verifier consuming the
computation as the money of record."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.eligibility import evaluate
from returns_resolution.intake import PLANTED_REQUESTS
from returns_resolution.modelport import OfflineComposer
from returns_resolution.models import Order, ReturnRequest, TenderPortion
from returns_resolution.policy import load_policy_pack
from returns_resolution.refunds import compute_refund, verify_computation
from returns_resolution.seed import CATALOGUE, ORDERS
from returns_resolution.service import CaseService
from returns_resolution.store import InMemoryStore, SqlStore
from returns_resolution.verifier import verify_summary

# Hand-computed from the seed data + pack v1 refund schedule:
# rates new_with_tags 0% / like_new 10% / used 20%; free return
# shipping for apparel+footwear; other categories deduct $6.95.
FIXTURES = {
    # jacket 189.00, new_with_tags -> fee 0.00; apparel -> shipping 0.00
    "CASE-2001": {"item_amount": "189.00", "restocking_fee": "0.00", "return_shipping": "0.00", "refund_total": "189.00"},
    # boots 145.00, like_new -> fee 14.50; footwear -> shipping 0.00
    "CASE-2003": {"item_amount": "145.00", "restocking_fee": "14.50", "return_shipping": "0.00", "refund_total": "130.50"},
    # headlamp 45.00 x1, used -> fee 9.00; electronics -> shipping 6.95
    "CASE-2006": {"item_amount": "45.00", "restocking_fee": "9.00", "return_shipping": "6.95", "refund_total": "29.05"},
}


def make_service(store=None) -> CaseService:
    service = CaseService(store or InMemoryStore(), OfflineComposer())
    service.seed_orders()
    return service


def _lines(computation) -> dict[str, Decimal | None]:
    return {line.line_id: line.amount for line in computation.lines}


# -- fixtures exact to the cent -------------------------------------------


@pytest.mark.parametrize("case_id,expected", FIXTURES.items())
def test_refund_fixtures_are_exact_to_the_cent(case_id, expected):
    service = make_service()
    request = next(r for r in PLANTED_REQUESTS if f"CASE-{r.request_id[4:]}" == case_id)
    service.ingest(request)
    computation = service.get_computation(case_id)
    amounts = _lines(computation)
    for line_id, expected_amount in expected.items():
        if line_id == "refund_total":
            assert computation.refund_total == Decimal(expected_amount)
        else:
            assert amounts[line_id] == Decimal(expected_amount), (case_id, line_id)
    assert computation.is_final is True
    assert computation.policy_pack_version == "2026.10.1"
    assert computation.computation_id.startswith("RC-")


def test_multi_line_order_multi_qty_refund():
    # Both headlamps from the two-line... single-line, qty-2 order
    # ORD-1006: item 90.00, used fee 18.00, shipping 6.95 -> 65.05.
    service = make_service()
    request = ReturnRequest(
        request_id="REQ-2099", order_id="ORD-1006", sku="LAMP-LMN", qty=2,
        reason_code="defective", condition_declared="used",
        requested_on=date(2026, 10, 25),
    )
    service.ingest(request)
    computation = service.get_computation("CASE-2099")
    assert _lines(computation) == {
        "item_amount": Decimal("90.00"),
        "restocking_fee": Decimal("18.00"),
        "return_shipping": Decimal("6.95"),
    }
    assert computation.refund_total == Decimal("65.05")


def test_ineligible_case_computation_is_a_final_zero():
    service = make_service()
    service.ingest(PLANTED_REQUESTS[3])  # REQ-2004, window expired
    computation = service.get_computation("CASE-2004")
    assert computation.eligible is False
    assert computation.lines == []
    assert computation.refund_total == Decimal("0.00")
    assert computation.is_final is True


def test_every_line_carries_its_derivation():
    service = make_service()
    service.ingest(PLANTED_REQUESTS[5])  # REQ-2006
    computation = service.get_computation("CASE-2006")
    for line in computation.lines:
        assert line.rule and line.policy_ref and line.inputs, line.line_id
    fee = next(line for line in computation.lines if line.line_id == "restocking_fee")
    assert fee.inputs["rate"] == "0.20"
    assert "0.20" in fee.policy_ref


def test_computation_is_deterministic_and_content_addressed():
    pack = load_policy_pack()
    request = PLANTED_REQUESTS[0]
    order = ORDERS[request.order_id]
    decision = evaluate(pack, order, CATALOGUE[request.sku], request)
    first = compute_refund(pack, order, CATALOGUE[request.sku], request, decision)
    second = compute_refund(pack, order, CATALOGUE[request.sku], request, decision)
    assert first.computation_id == second.computation_id
    assert first.model_dump() == second.model_dump()


# -- tamper detection --------------------------------------------------------


def test_tampering_with_any_figure_is_detected():
    service = make_service()
    service.ingest(PLANTED_REQUESTS[2])  # REQ-2003
    assert service.verify_stored_computation("CASE-2003") == []

    stored = service.get_computation("CASE-2003")
    tampered_line = stored.model_copy(deep=True)
    tampered_line.lines[1].amount = Decimal("1.00")
    assert verify_computation(
        tampered_line, load_policy_pack(), ORDERS["ORD-1003"], CATALOGUE["BOOT-TRL"],
        PLANTED_REQUESTS[2], service.get_case("CASE-2003").decision,
    ), "a changed line amount must be detected"

    tampered_total = stored.model_copy(deep=True)
    tampered_total.refund_total = Decimal("145.00")
    assert verify_computation(
        tampered_total, load_policy_pack(), ORDERS["ORD-1003"], CATALOGUE["BOOT-TRL"],
        PLANTED_REQUESTS[2], service.get_case("CASE-2003").decision,
    ), "a changed refund total must be detected"


# -- not computable, never zero-filled --------------------------------------


def test_unknown_condition_makes_the_fee_not_computable_not_zero():
    service = make_service()
    request = ReturnRequest(
        request_id="REQ-2098", order_id="ORD-1007", sku="STOVE-PKT", qty=1,
        reason_code="changed_mind", condition_declared="waterlogged",
        requested_on=date(2026, 10, 25),
    )
    case = service.ingest(request)
    computation = service.get_computation("CASE-2098")
    fee = next(line for line in computation.lines if line.line_id == "restocking_fee")
    assert fee.status == "not_computable"
    assert fee.amount is None, "a missing rate must never surface as a zero fee"
    assert "waterlogged" in fee.missing_input
    assert computation.refund_total is None
    assert computation.is_final is False
    # The summary states the missing input and quotes no refund total.
    assert "restocking_fee_rate" in case.summary
    assert "$59.95" in case.summary  # item amount is computed and sayable
    assert service.verify_stored_computation("CASE-2098") == []


def test_no_line_is_ever_a_silent_zero_fill():
    service = make_service()
    for request in PLANTED_REQUESTS:
        service.ingest(request)
    for computation in service.list_computations():
        for line in computation.lines:
            if line.status == "not_computable":
                assert line.amount is None and line.missing_input
            else:
                assert line.amount is not None


def test_tender_allocation_names_the_missing_breakdown():
    service = make_service()
    service.ingest(PLANTED_REQUESTS[0])
    computation = service.get_computation("CASE-2001")
    assert computation.tender_allocations == []
    named = [c for c in computation.not_computable if c.component == "tender_allocation"]
    assert named and named[0].missing_input == "payment_tender_breakdown"
    # The total itself is final; only its allocation awaits the input.
    assert computation.is_final is True and computation.refund_total == Decimal("189.00")


def test_tender_allocation_follows_recorded_order_and_caps():
    pack = load_policy_pack()
    order = ORDERS["ORD-1001"].model_copy(
        update={
            "tenders": [
                TenderPortion(method="gift_card", amount=Decimal("50.00")),
                TenderPortion(method="card", amount=Decimal("175.00")),
            ]
        }
    )
    request = PLANTED_REQUESTS[0]
    decision = evaluate(pack, order, CATALOGUE[request.sku], request)
    computation = compute_refund(pack, order, CATALOGUE[request.sku], request, decision)
    assert [(a.method, a.amount) for a in computation.tender_allocations] == [
        ("gift_card", Decimal("50.00")),
        ("card", Decimal("139.00")),  # 189.00 total - 50.00 gift card
    ]
    assert not [c for c in computation.not_computable if c.component == "tender_allocation"]


def test_tender_amounts_that_do_not_sum_to_the_order_are_not_allocated():
    pack = load_policy_pack()
    order = ORDERS["ORD-1001"].model_copy(
        update={"tenders": [TenderPortion(method="card", amount=Decimal("10.00"))]}
    )
    request = PLANTED_REQUESTS[0]
    decision = evaluate(pack, order, CATALOGUE[request.sku], request)
    computation = compute_refund(pack, order, CATALOGUE[request.sku], request, decision)
    assert computation.tender_allocations == []
    assert any(
        c.component == "tender_allocation" and "do not sum" in c.missing_input
        for c in computation.not_computable
    )


# -- summary + verifier consume the computation -------------------------------


def test_summary_quotes_the_computed_refund_not_the_prefee_figure():
    service = make_service()
    case = service.ingest(PLANTED_REQUESTS[2])  # boots: decision says 145, refund is 130.50
    assert case.decision.refund_due == Decimal("145.00")
    assert "$130.50" in case.summary and "$14.50" in case.summary
    assert verify_summary(case.summary, case.decision, case.computation) == []
    # The computed fee is not sayable from the decision alone:
    assert verify_summary(case.summary, case.decision), "fee figure needs the computation universe"


def test_a_refund_figure_the_computation_did_not_produce_is_rejected():
    service = make_service()
    case = service.ingest(PLANTED_REQUESTS[2])
    wrong = case.summary.replace("$130.50", "$135.50")
    assert verify_summary(wrong, case.decision, case.computation)


# -- store / API / runtime parity ----------------------------------------------


def test_computation_persists_through_the_sql_store(tmp_path):
    path = tmp_path / "returns.db"
    store = SqlStore(sqlite_path=path)
    assert store.migrate() == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"]
    assert store.migrate() == []
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    service.ingest(PLANTED_REQUESTS[2])

    reopened = SqlStore(sqlite_path=path)
    assert reopened.migrate() == []
    stored = reopened.get_computation("CASE-2003")
    assert stored["refund_total"] == "130.50"
    assert stored["is_final"] is True


def test_api_and_runtime_computation_reads():
    from returns_resolution.api import create_app
    from returns_resolution.runtime_app import handle_payload

    service = make_service()
    client = TestClient(create_app(service))
    client.post("/intake/feed")
    view = client.get("/cases/CASE-2006/computation").json()
    assert view["refund_total"] == "29.05"
    assert view["lines"][1]["policy_ref"].startswith("policy.restocking_fee_rates")
    verify = client.get("/cases/CASE-2006/computation/verify").json()
    assert verify == {"case_id": "CASE-2006", "ok": True, "problems": []}
    assert client.get("/cases/CASE-9999/computation").status_code == 404

    out = handle_payload(service, {"action": "get_computation", "case_id": "CASE-2006"})
    assert out["computation"]["refund_total"] == "29.05"
    out = handle_payload(service, {"action": "verify_computation", "case_id": "CASE-2006"})
    assert out["ok"] is True


def test_ingest_stays_idempotent_with_computations():
    service = make_service()
    service.ingest(PLANTED_REQUESTS[0])
    service.ingest(PLANTED_REQUESTS[0])
    assert len(service.list_computations()) == 1
