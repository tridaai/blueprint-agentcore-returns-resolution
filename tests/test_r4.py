"""R4 gates: the fraud scoring pack — signals graded signal by
signal against hand-derived arithmetic, planted truth end to end
(the serial returner in FRAUD_REVIEW at exactly 95.00, the
wardrobing pattern in MONITOR at exactly 36.00, clean customers at
0.00 CLEAR), insufficient/not-computable honesty named never
zero-assumed, the block as case state, the review act as the only
way through it, supersession by link, and tamper detection — on the
service, the SQL store, the API and the Runtime dispatch.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.api import create_app
from returns_resolution.fraud import (
    CaseFact,
    load_scoring_pack,
    score_case,
)
from returns_resolution.intake import FRAUD_FEED_REQUESTS, PLANTED_REQUESTS
from returns_resolution.modelport import OfflineComposer
from returns_resolution.models import Order, OrderLine, TenderPortion
from returns_resolution.runtime_app import handle_payload
from returns_resolution.seed import PRODUCT_WEIGHTS_GRAMS
from returns_resolution.service import CaseService, FraudHoldError, FraudReviewError
from returns_resolution.store import InMemoryStore, SqlStore

PACK = load_scoring_pack()


def _service(store=None) -> CaseService:
    service = CaseService(store or InMemoryStore(), OfflineComposer())
    service.seed_orders()
    service.seed_history()
    return service


def _fact(
    case_id: str,
    *,
    customer: str = "CUST-T",
    order_id: str = "ORD-T1",
    sku: str = "JKT-ALPINE",
    category: str | None = "apparel",
    condition: str = "used",
    requested: date = date(2026, 10, 24),
    delivered: date = date(2026, 10, 1),
    qty: int = 1,
    weight: int | None = None,
) -> CaseFact:
    return CaseFact(
        case_id=case_id, order_id=order_id, customer_id=customer, sku=sku, qty=qty,
        category=category, condition_declared=condition, requested_on=requested,
        delivered_on=delivered, returned_weight_grams=weight,
    )


def _order(
    order_id: str,
    *,
    customer: str = "CUST-T",
    delivered: date = date(2026, 10, 1),
    tenders: list[str] | None = None,
) -> Order:
    return Order(
        order_id=order_id, customer_id=customer,
        placed_on=delivered - timedelta(days=3), delivered_on=delivered,
        lines=[OrderLine(sku="JKT-ALPINE", qty=1, unit_price=Decimal("100.00"))],
        shipping_paid=Decimal("0.00"),
        tenders=[TenderPortion(method=m, amount=Decimal("100.00")) for m in tenders]
        if tenders is not None else None,
    )


def _signal(score, signal_id: str):
    return next(s for s in score.signals if s.signal_id == signal_id)


# ---------------------------------------------------------------------------
# The pack itself
# ---------------------------------------------------------------------------

def test_pack_loader_is_fail_closed():
    with pytest.raises(ValueError):
        load_scoring_pack("1999.01.1")
    assert load_scoring_pack().version == "2026.10.1"
    assert load_scoring_pack("2026.10.1").fingerprint() == PACK.fingerprint()


def test_band_thresholds():
    assert PACK.band_for(Decimal("60")) == "FRAUD_REVIEW"
    assert PACK.band_for(Decimal("59.99")) == "MONITOR"
    assert PACK.band_for(Decimal("25")) == "MONITOR"
    assert PACK.band_for(Decimal("24.99")) == "CLEAR"


# ---------------------------------------------------------------------------
# Signal arithmetic, graded by hand
# ---------------------------------------------------------------------------

def test_velocity_allowance_and_cap():
    scored = _fact("CASE-900")
    priors = [_fact(f"CASE-9{i:02d}", order_id=f"ORD-9{i:02d}", requested=date(2026, 10, 1)) for i in range(10, 19)]
    score = score_case(PACK, scored=scored, facts=[scored, *priors], orders=[], weights_grams={})
    velocity = _signal(score, "return_velocity")
    assert velocity.raw_value == "9"
    # (9 - 2 allowance) x 8 = 56 -> capped at 40.
    assert velocity.contribution == Decimal("40.00")

    two = score_case(PACK, scored=scored, facts=[scored, *priors[:2]], orders=[], weights_grams={})
    assert _signal(two, "return_velocity").contribution == Decimal("0.00")
    three = score_case(PACK, scored=scored, facts=[scored, *priors[:3]], orders=[], weights_grams={})
    assert _signal(three, "return_velocity").contribution == Decimal("8.00")


def test_velocity_window_edge():
    scored = _fact("CASE-900", requested=date(2026, 10, 24))
    inside = _fact("CASE-901", order_id="ORD-901", requested=date(2026, 7, 26))  # exactly 90 days
    outside = _fact("CASE-902", order_id="ORD-902", requested=date(2026, 7, 25))  # 91 days
    score = score_case(PACK, scored=scored, facts=[scored, inside, outside], orders=[], weights_grams={})
    assert _signal(score, "return_velocity").raw_value == "1"


def test_share_tiers_and_minimum_history():
    scored = _fact("CASE-900", order_id="ORD-T5")
    orders = [_order(f"ORD-T{i}") for i in range(1, 6)]
    facts = [scored] + [
        _fact(f"CASE-90{i}", order_id=f"ORD-T{i}", requested=date(2026, 10, 2)) for i in range(1, 4)
    ]
    score = score_case(PACK, scored=scored, facts=facts, orders=orders, weights_grams={})
    share = _signal(score, "serial_returner_share")
    assert share.status == "computed" and share.raw_value == "0.8000"
    assert share.contribution == Decimal("35.00")

    thin = score_case(PACK, scored=scored, facts=facts, orders=orders[:3], weights_grams={})
    share = _signal(thin, "serial_returner_share")
    assert share.status == "insufficient_data"
    assert share.contribution == Decimal("0")
    assert share.missing_input is not None and "4" in share.missing_input


def test_wardrobing_age_boundary_and_cap():
    scored = _fact("CASE-900", condition="new_with_tags", requested=date(2026, 10, 22), delivered=date(2026, 10, 1))
    # scored: age 21 -> counts. prior at age 20 -> does not. gear does not.
    young = _fact("CASE-901", order_id="ORD-901", condition="new_with_tags",
                  requested=date(2026, 10, 21), delivered=date(2026, 10, 1))
    gear = _fact("CASE-902", order_id="ORD-902", category="gear", condition="new_with_tags",
                 requested=date(2026, 10, 22), delivered=date(2026, 9, 1))
    score = score_case(PACK, scored=scored, facts=[scored, young, gear], orders=[], weights_grams={})
    w = _signal(score, "wardrobing_indicator")
    assert w.raw_value == "1" and w.contribution == Decimal("12.00")

    many = [scored] + [
        _fact(f"CASE-9{i:02d}", order_id=f"ORD-9{i:02d}", condition="new_with_tags",
              requested=date(2026, 10, 21), delivered=date(2026, 9, 19))
        for i in range(10, 14)
    ]
    capped = score_case(PACK, scored=scored, facts=many, orders=[], weights_grams={})
    assert _signal(capped, "wardrobing_indicator").contribution == Decimal("36.00")  # 5 x 12 -> cap


def test_weight_bands_and_missing_inputs():
    base = dict(sku="BOOT-TRL", category="footwear")
    empty = score_case(PACK, scored=_fact("CASE-900", weight=500, **base),
                       facts=[_fact("CASE-900", weight=500, **base)], orders=[], weights_grams=PRODUCT_WEIGHTS_GRAMS)
    w = _signal(empty, "weight_mismatch")
    assert w.raw_value == "0.3448" and w.contribution == Decimal("30.00")

    light = score_case(PACK, scored=_fact("CASE-900", weight=1000, **base),
                       facts=[_fact("CASE-900", weight=1000, **base)], orders=[], weights_grams=PRODUCT_WEIGHTS_GRAMS)
    assert _signal(light, "weight_mismatch").contribution == Decimal("10.00")

    full = score_case(PACK, scored=_fact("CASE-900", weight=1400, **base),
                      facts=[_fact("CASE-900", weight=1400, **base)], orders=[], weights_grams=PRODUCT_WEIGHTS_GRAMS)
    assert _signal(full, "weight_mismatch").contribution == Decimal("0.00")

    unscanned = score_case(PACK, scored=_fact("CASE-900", **base),
                           facts=[_fact("CASE-900", **base)], orders=[], weights_grams=PRODUCT_WEIGHTS_GRAMS)
    w = _signal(unscanned, "weight_mismatch")
    assert w.status == "not_computable" and w.missing_input == "returned_weight_grams"

    no_master = score_case(PACK, scored=_fact("CASE-900", sku="MYSTERY", category="footwear", weight=100),
                           facts=[_fact("CASE-900", sku="MYSTERY", category="footwear", weight=100)],
                           orders=[], weights_grams=PRODUCT_WEIGHTS_GRAMS)
    w = _signal(no_master, "weight_mismatch")
    assert w.status == "not_computable" and w.missing_input == "product_ship_weight_grams"


def test_card_churn_tiers_and_missing_tenders():
    scored = _fact("CASE-900", order_id="ORD-T1")
    orders = [
        _order("ORD-T1", tenders=["card_a"]),
        _order("ORD-T2", tenders=["card_b"]),
        _order("ORD-T3", tenders=["card_c"]),
        _order("ORD-T4", tenders=["card_d"]),
    ]
    score = score_case(PACK, scored=scored, facts=[scored], orders=orders, weights_grams={})
    churn = _signal(score, "card_churn")
    assert churn.raw_value == "4" and churn.contribution == Decimal("20.00")

    three = score_case(PACK, scored=scored, facts=[scored], orders=orders[:3], weights_grams={})
    assert _signal(three, "card_churn").contribution == Decimal("10.00")

    none = score_case(PACK, scored=scored, facts=[scored], orders=[_order("ORD-T1")], weights_grams={})
    churn = _signal(none, "card_churn")
    assert churn.status == "insufficient_data" and churn.missing_input == "payment_tender_breakdown"


def test_scoring_is_reproducible_byte_for_byte():
    scored = _fact("CASE-900", order_id="ORD-T1")
    orders = [_order("ORD-T1", tenders=["card_a"])]
    a = score_case(PACK, scored=scored, facts=[scored], orders=orders, weights_grams={})
    b = score_case(PACK, scored=scored, facts=[scored], orders=orders, weights_grams={})
    assert a.model_dump(mode="json") == b.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Planted truth, end to end
# ---------------------------------------------------------------------------

def test_serial_returner_lands_in_fraud_review_at_95():
    service = _service()
    case = service.ingest(FRAUD_FEED_REQUESTS[0])
    score = case.fraud_score
    assert score.total_score == Decimal("95.00")
    assert score.band == "FRAUD_REVIEW"
    assert case.fraud_state == "fraud_review"
    assert _signal(score, "return_velocity").contribution == Decimal("40.00")
    assert _signal(score, "return_velocity").raw_value == "7"
    assert _signal(score, "serial_returner_share").contribution == Decimal("35.00")
    assert _signal(score, "serial_returner_share").raw_value == "1.0000"
    assert _signal(score, "card_churn").contribution == Decimal("20.00")
    assert _signal(score, "wardrobing_indicator").contribution == Decimal("0.00")
    weight = _signal(score, "weight_mismatch")
    assert weight.status == "not_computable" and weight.missing_input == "returned_weight_grams"
    assert set(score.input_case_ids) == {f"CASE-300{i}" for i in range(1, 9)}
    assert set(score.input_order_ids) == {f"ORD-300{i}" for i in range(1, 9)}
    assert service.verify_stored_fraud_score("CASE-3008") == []
    # The disposition stands — the block is on the refund path, not
    # on the item's recorded fate.
    assert service.get_disposition("CASE-3008") is not None


def test_wardrobing_pattern_lands_in_monitor_at_36():
    service = _service()
    case = service.ingest(FRAUD_FEED_REQUESTS[1])
    score = case.fraud_score
    assert score.total_score == Decimal("36.00")
    assert score.band == "MONITOR"
    assert case.fraud_state == "monitor"
    assert _signal(score, "wardrobing_indicator").contribution == Decimal("36.00")
    assert _signal(score, "return_velocity").contribution == Decimal("0.00")
    share = _signal(score, "serial_returner_share")
    assert share.status == "insufficient_data" and share.contribution == Decimal("0")
    churn = _signal(score, "card_churn")
    assert churn.status == "insufficient_data" and churn.missing_input == "payment_tender_breakdown"
    assert service.refund_path_blocked("CASE-3103") is False
    assert service.verify_stored_fraud_score("CASE-3103") == []


def test_clean_feed_customers_score_clear_zero():
    service = _service()
    for request in PLANTED_REQUESTS:
        case = service.ingest(request)
        assert case.fraud_score is not None
        assert case.fraud_score.total_score == Decimal("0.00")
        assert case.fraud_score.band == "CLEAR"
        assert case.fraud_state == "clear"
        assert service.verify_stored_fraud_score(case.case_id) == []


# ---------------------------------------------------------------------------
# The block and the review act
# ---------------------------------------------------------------------------

def test_block_and_the_only_way_through_it():
    service = _service()
    service.ingest(FRAUD_FEED_REQUESTS[0])
    assert service.refund_path_blocked("CASE-3008") is True
    with pytest.raises(FraudHoldError):
        service.assert_refund_path_open("CASE-3008")

    decision = service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez",
        outcome="cleared_with_reason", reason_code="legitimate_pattern_explained",
    )
    case = service.get_case("CASE-3008")
    assert case.fraud_state == "fraud_cleared"
    assert service.refund_path_blocked("CASE-3008") is False
    service.assert_refund_path_open("CASE-3008")
    # The act lifted the block; the score artefact is untouched.
    assert case.fraud_score.band == "FRAUD_REVIEW"
    assert case.fraud_score.total_score == Decimal("95.00")
    assert case.fraud_review.review_id == decision.review_id
    assert service.verify_stored_fraud_score("CASE-3008") == []


def test_confirmed_fraud_is_sticky_across_rescoring():
    service = _service()
    service.ingest(FRAUD_FEED_REQUESTS[0])
    service.record_fraud_review(
        "CASE-3008", reviewer="m.osei", outcome="confirmed_fraud", reason_code="pattern_verified",
    )
    assert service.get_case("CASE-3008").fraud_state == "fraud_confirmed"
    service.rescore_case("CASE-3008")
    assert service.get_case("CASE-3008").fraud_state == "fraud_confirmed"
    assert service.refund_path_blocked("CASE-3008") is True


def test_review_act_refusals():
    service = _service()
    service.ingest(FRAUD_FEED_REQUESTS[0])
    service.ingest(FRAUD_FEED_REQUESTS[1])
    with pytest.raises(FraudReviewError):  # unnamed reviewer
        service.record_fraud_review("CASE-3008", reviewer="  ", outcome="cleared_with_reason",
                                    reason_code="legitimate_pattern_explained")
    with pytest.raises(FraudReviewError):  # outcome outside the vocabulary
        service.record_fraud_review("CASE-3008", reviewer="r.alvarez", outcome="edit_score",
                                    reason_code="legitimate_pattern_explained")
    with pytest.raises(FraudReviewError):  # reason not coded for the outcome
        service.record_fraud_review("CASE-3008", reviewer="r.alvarez", outcome="cleared_with_reason",
                                    reason_code="pattern_verified")
    with pytest.raises(FraudReviewError):  # a MONITOR case is not in fraud review
        service.record_fraud_review("CASE-3103", reviewer="r.alvarez", outcome="confirmed_fraud",
                                    reason_code="pattern_verified")
    service.record_fraud_review("CASE-3008", reviewer="r.alvarez", outcome="cleared_with_reason",
                                reason_code="data_error_corrected")
    with pytest.raises(FraudReviewError):  # one decision per score
        service.record_fraud_review("CASE-3008", reviewer="m.osei", outcome="confirmed_fraud",
                                    reason_code="pattern_verified")
    with pytest.raises(LookupError):
        service.record_fraud_review("CASE-9999", reviewer="r.alvarez", outcome="confirmed_fraud",
                                    reason_code="pattern_verified")


# ---------------------------------------------------------------------------
# Supersession + tamper
# ---------------------------------------------------------------------------

def test_rescore_supersedes_by_link_never_edits():
    service = _service()
    service.ingest(FRAUD_FEED_REQUESTS[0])
    first = service.get_fraud_score("CASE-3008")
    # No new evidence: the same artefact comes back, nothing written.
    again = service.rescore_case("CASE-3008")
    assert again.score_id == first.score_id
    assert len([s for s in service.list_fraud_scores() if s.case_id == "CASE-3008"]) == 1

    # New evidence: another SER order lands in the store (no return).
    from returns_resolution.models import Order as OrderModel
    from returns_resolution.seed import CATALOGUE

    extra = OrderModel(
        order_id="ORD-3009", customer_id="CUST-SER",
        placed_on=date(2026, 10, 13), delivered_on=date(2026, 10, 16),
        lines=[{"sku": "STOVE-PKT", "qty": 1, "unit_price": str(CATALOGUE["STOVE-PKT"].unit_price)}],
        shipping_paid="0.00",
        tenders=[{"method": "card_mc_5522", "amount": "59.95"}],
    )
    service._store.put_order(extra.model_dump(mode="json"))
    fresh = service.rescore_case("CASE-3008")
    assert fresh.score_id != first.score_id
    assert fresh.supersedes == first.score_id
    # Share is now 8 of 9 = 0.8889, still the top tier: same total.
    assert fresh.total_score == Decimal("95.00")
    assert _signal(fresh, "serial_returner_share").raw_value == "0.8889"
    chain = [s for s in service.list_fraud_scores() if s.case_id == "CASE-3008"]
    assert len(chain) == 2
    assert service.get_fraud_score("CASE-3008").score_id == fresh.score_id
    assert service.verify_stored_fraud_score("CASE-3008") == []
    # The superseded artefact still stands, unedited.
    old = service._store.get_fraud_score(first.score_id)
    assert old["band"] == "FRAUD_REVIEW" and old["supersedes"] is None


def test_tampered_score_is_detected():
    service = _service()
    service.ingest(FRAUD_FEED_REQUESTS[0])
    stored = service._store.get_fraud_score(service.get_fraud_score("CASE-3008").score_id)
    for mutate in (
        lambda p: p.__setitem__("band", "CLEAR"),
        lambda p: p.__setitem__("total_score", "0.00"),
    ):
        tampered = {**stored}
        mutate(tampered)
        service._store.put_fraud_score(tampered)
        assert service.verify_stored_fraud_score("CASE-3008") != []
        service._store.put_fraud_score(stored)
    assert service.verify_stored_fraud_score("CASE-3008") == []


# ---------------------------------------------------------------------------
# SQL store + API + Runtime parity
# ---------------------------------------------------------------------------

def test_fraud_artefacts_persist_through_the_sql_store(tmp_path):
    path = tmp_path / "returns.db"
    store = SqlStore(sqlite_path=path)
    applied = store.migrate()
    assert "0004" in applied
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    service.seed_history()
    service.ingest(FRAUD_FEED_REQUESTS[0])
    service.record_fraud_review("CASE-3008", reviewer="r.alvarez",
                                outcome="confirmed_fraud", reason_code="evidence_conclusive")
    reopened_store = SqlStore(sqlite_path=path)
    assert reopened_store.migrate() == []
    reopened = CaseService(reopened_store, OfflineComposer())
    score = reopened.get_fraud_score("CASE-3008")
    assert score.total_score == Decimal("95.00") and score.band == "FRAUD_REVIEW"
    assert reopened.get_case("CASE-3008").fraud_state == "fraud_confirmed"
    assert reopened.verify_stored_fraud_score("CASE-3008") == []
    review = reopened.get_fraud_review("CASE-3008")
    assert review.reviewer == "r.alvarez" and review.outcome == "confirmed_fraud"


def test_api_fraud_surfaces():
    service = _service()
    client = TestClient(create_app(service))
    client.post("/intake/feed")
    assert client.post("/returns", json=FRAUD_FEED_REQUESTS[0].model_dump(mode="json")).status_code == 201

    score = client.get("/cases/CASE-3008/fraud-score")
    assert score.status_code == 200
    assert score.json()["band"] == "FRAUD_REVIEW" and score.json()["total_score"] == "95.00"

    signals = client.get("/cases/CASE-3008/fraud-score/signals").json()
    assert {s["signal_id"] for s in signals["signals"]} == {
        "return_velocity", "serial_returner_share", "wardrobing_indicator",
        "weight_mismatch", "card_churn",
    }
    assert client.get("/cases/CASE-3008/fraud-score/verify").json()["ok"] is True
    assert client.get("/fraud/scoring-pack").json()["version"] == "2026.10.1"

    refused = client.post("/cases/CASE-3008/fraud-review", json={
        "reviewer": "r.alvarez", "outcome": "edit_score", "reason_code": "x"})
    assert refused.status_code == 409
    unnamed = client.post("/cases/CASE-3008/fraud-review", json={
        "reviewer": " ", "outcome": "cleared_with_reason", "reason_code": "data_error_corrected"})
    assert unnamed.status_code == 422

    made = client.post("/cases/CASE-3008/fraud-review", json={
        "reviewer": "r.alvarez", "outcome": "cleared_with_reason",
        "reason_code": "legitimate_pattern_explained"})
    assert made.status_code == 201
    assert client.get("/cases/CASE-3008").json()["fraud_state"] == "fraud_cleared"
    assert client.get("/cases/CASE-3008/fraud-review").json()["reviewer"] == "r.alvarez"


def test_runtime_dispatch_parity():
    service = _service()
    out = handle_payload(service, {"action": "intake", "request": FRAUD_FEED_REQUESTS[0].model_dump(mode="json")})
    assert out["case"]["fraud_score"]["band"] == "FRAUD_REVIEW"
    got = handle_payload(service, {"action": "get_fraud_score", "case_id": "CASE-3008"})
    assert got["fraud_score"]["total_score"] == "95.00"
    signals = handle_payload(service, {"action": "get_fraud_signals", "case_id": "CASE-3008"})
    assert len(signals["signals"]) == 5
    assert handle_payload(service, {"action": "verify_fraud_score", "case_id": "CASE-3008"})["ok"] is True
    refused = handle_payload(service, {
        "action": "record_fraud_review", "case_id": "CASE-3008",
        "reviewer": "r.alvarez", "outcome": "edit_score", "reason_code": "x"})
    assert "error" in refused
    made = handle_payload(service, {
        "action": "record_fraud_review", "case_id": "CASE-3008",
        "reviewer": "r.alvarez", "outcome": "confirmed_fraud", "reason_code": "pattern_verified"})
    assert made["fraud_review"]["outcome"] == "confirmed_fraud"
    pack = handle_payload(service, {"action": "get_scoring_pack"})
    assert pack["scoring_pack"]["version"] == "2026.10.1"
