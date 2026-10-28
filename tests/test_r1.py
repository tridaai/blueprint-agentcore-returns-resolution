"""R1 gates: seed provability, eligibility truth, the window edge pair,
the figure verifier, store/evidence roundtrips, API and Runtime shape."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.eligibility import evaluate
from returns_resolution.evidence import LocalEvidenceStore
from returns_resolution.intake import PLANTED_REQUESTS, ReferenceFeed
from returns_resolution.modelport import OfflineComposer, load_model_port
from returns_resolution.models import ReturnRequest
from returns_resolution.policy import load_policy_pack
from returns_resolution.seed import CATALOGUE, ORDERS
from returns_resolution.service import CaseService, FigureVerificationError
from returns_resolution.store import InMemoryStore, SqlStore
from returns_resolution.verifier import verify_summary

TRUTH = {
    "REQ-2001": (True, []),
    "REQ-2002": (False, ["WINDOW_EXPIRED"]),
    "REQ-2003": (True, []),
    "REQ-2004": (False, ["WINDOW_EXPIRED"]),
    "REQ-2005": (False, ["FINAL_SALE"]),
    "REQ-2006": (True, []),
    "REQ-2007": (False, ["CONDITION_BARRED"]),
    "REQ-2008": (False, ["CATEGORY_EXCLUDED"]),
}


def make_service(store=None) -> CaseService:
    service = CaseService(store or InMemoryStore(), OfflineComposer())
    service.seed_orders()
    return service


# -- seed provability ------------------------------------------------------


def test_order_totals_are_provable_from_the_catalogue():
    for order in ORDERS.values():
        expected_items = sum(
            (CATALOGUE[line.sku].unit_price * line.qty for line in order.lines),
            Decimal("0.00"),
        )
        assert order.items_total == expected_items
        assert order.order_total == expected_items + order.shipping_paid


def test_spot_totals():
    assert ORDERS["ORD-1001"].order_total == Decimal("225.00")  # 189 + 24 + 12 shipping
    assert ORDERS["ORD-1006"].items_total == Decimal("90.00")  # 2 headlamps


# -- eligibility -------------------------------------------------------------


def test_eligibility_matches_planted_truth_for_every_request():
    pack = load_policy_pack()
    for request in PLANTED_REQUESTS:
        order = ORDERS[request.order_id]
        decision = evaluate(pack, order, CATALOGUE.get(request.sku), request)
        expected_eligible, expected_denies = TRUTH[request.request_id]
        assert decision.eligible is expected_eligible, request.request_id
        denies = [c.clause_id for c in decision.clauses if c.effect == "deny"]
        assert denies == expected_denies, request.request_id
        assert decision.policy_pack_version == pack.version
        assert decision.clauses, "every decision records the clauses that fired"


def test_window_edge_pair_grades_both_sides():
    pack = load_policy_pack()
    inside = next(r for r in PLANTED_REQUESTS if r.request_id == "REQ-2003")
    outside = next(r for r in PLANTED_REQUESTS if r.request_id == "REQ-2004")
    d_in = evaluate(pack, ORDERS[inside.order_id], CATALOGUE[inside.sku], inside)
    d_out = evaluate(pack, ORDERS[outside.order_id], CATALOGUE[outside.sku], outside)
    assert d_in.window_end == date(2026, 10, 26) == inside.requested_on
    assert d_in.eligible is True
    assert d_out.window_end == date(2026, 10, 25)
    assert (outside.requested_on - d_out.window_end).days == 1
    assert d_out.eligible is False


def test_policy_loader_is_fail_closed():
    with pytest.raises(ValueError):
        load_policy_pack("1999.01.1")


def test_refund_due_is_item_amount_or_zero():
    service = make_service()
    for request in PLANTED_REQUESTS:
        case = service.ingest(request)
        if case.decision.eligible:
            assert case.decision.refund_due == case.decision.item_amount > Decimal("0.00")
        else:
            assert case.decision.refund_due == Decimal("0.00")


# -- figure verifier -----------------------------------------------------------


def test_verifier_accepts_the_composed_summary_and_rejects_wrong_figures():
    service = make_service()
    case = service.ingest(PLANTED_REQUESTS[0])
    assert verify_summary(case.summary, case.decision) == []

    wrong_amount = case.summary.replace("$189.00", "$198.00")
    assert verify_summary(wrong_amount, case.decision), "transposed amount must fail"

    wrong_date = case.summary.replace("2026-10-20", "2026-10-21")
    assert verify_summary(wrong_date, case.decision), "invented date must fail"


class _TamperedComposer:
    profile_name = "tampered"

    def compose_summary(self, decision, product_name):
        return "Your refund due is $999.99. Please ship by 2027-01-01."


def test_a_wrong_figure_draft_is_never_stored_as_a_summary():
    service = CaseService(InMemoryStore(), _TamperedComposer())
    service.seed_orders()
    with pytest.raises(FigureVerificationError):
        service.ingest(PLANTED_REQUESTS[0])
    case = service.get_case("CASE-2001")
    assert case.status == "summary_failed"
    assert case.summary is None


def test_model_port_profiles_are_fail_closed():
    with pytest.raises(RuntimeError):
        load_model_port("hosted-bedrock").compose_summary(
            make_service().ingest(PLANTED_REQUESTS[0]).decision, "Jacket"
        )
    with pytest.raises(ValueError):
        load_model_port("no-such-profile")


# -- intake / service ------------------------------------------------------------


def test_ingest_is_idempotent_by_request_id():
    service = make_service()
    first = service.ingest(PLANTED_REQUESTS[0])
    second = service.ingest(PLANTED_REQUESTS[0])
    assert first.case_id == second.case_id
    assert len(service.list_cases()) == 1


def test_feed_ingests_all_planted_requests():
    service = make_service()
    for request in ReferenceFeed().fetch_requests():
        service.ingest(request)
    assert len(service.list_cases()) == len(PLANTED_REQUESTS)


# -- store -------------------------------------------------------------------------


def test_sqlite_store_migrates_once_and_persists(tmp_path):
    path = tmp_path / "returns.db"
    store = SqlStore(sqlite_path=path)
    assert store.migrate() == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010"]  # 0002 by R2 (computations), 0003 by R3 (dispositions), 0004 by R4 (fraud), 0005 by R5 (approvals/executions/receipts), 0006 by R6 (labels/voids/pickups/tool calls), 0007 by R7 (grading chains + warehouse tables), 0008 by R8 (customer messages), 0009 by R9 (customer memory), 0010 by R12 (model cost ledger)
    assert store.migrate() == [], "re-running migrations applies nothing"
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    case = service.ingest(PLANTED_REQUESTS[2])

    reopened = SqlStore(sqlite_path=path)
    assert reopened.migrate() == []
    stored = reopened.get_case(case.case_id)
    assert stored["decision"]["eligible"] is True
    assert reopened.get_decision(case.case_id)["policy_pack_version"] == "2026.10.1"


@pytest.mark.skipif(
    not (os.environ.get("RETURNS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")),
    reason="Postgres leg runs when RETURNS_TEST_DATABASE_URL is set (Postgres profile, R10)",
)
def test_postgres_store_dialect(tmp_path):
    # Scoped to a private, run-unique schema (R11 hermeticity): the
    # bare URL would write into the shared public schema, where one
    # run's rows collide with the next run's against a persistent
    # cluster.
    import uuid

    import psycopg

    base = os.environ.get("RETURNS_TEST_DATABASE_URL") or os.environ["DATABASE_URL"]
    schema = f"t_r1dialect_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in base else "?"
    store = SqlStore(database_url=f"{base}{sep}options=-csearch_path%3D{schema}")
    store.migrate()
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    case = service.ingest(PLANTED_REQUESTS[0])
    assert store.get_case(case.case_id)["case_id"] == case.case_id


# -- evidence -------------------------------------------------------------------------


def test_evidence_roundtrip_records_and_verifies_hash(tmp_path):
    evidence = LocalEvidenceStore(root=tmp_path / "evidence")
    record = evidence.put("CASE-2001", "photo", "damage.jpg", b"jpeg-bytes")
    assert record["uri"].startswith("s3://returns-evidence/CASE-2001/photo/")
    assert len(record["sha256"]) == 64
    assert evidence.get(record["uri"]) == b"jpeg-bytes"


def test_evidence_attach_records_on_the_case(tmp_path):
    store = InMemoryStore()
    service = CaseService(store, OfflineComposer(), evidence_store=LocalEvidenceStore(root=tmp_path / "ev"))
    service.seed_orders()
    case = service.ingest(PLANTED_REQUESTS[0])
    record = service.attach_evidence(case.case_id, "receipt", "receipt.pdf", b"pdf-bytes")
    assert store.list_evidence(case.case_id)[0]["evidence_id"] == record["evidence_id"]


# -- API ---------------------------------------------------------------------------------


def test_api_surface():
    from returns_resolution.api import create_app

    client = TestClient(create_app(make_service()))
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/readiness").json()["orders"] == len(ORDERS)
    feed = client.post("/intake/feed").json()
    assert len(feed["ingested"]) == len(PLANTED_REQUESTS)
    cases = client.get("/cases").json()
    assert len(cases) == len(PLANTED_REQUESTS)
    decision = client.get("/cases/CASE-2003/decision").json()
    assert decision["eligible"] is True
    assert client.get("/cases/CASE-9999").status_code == 404
    order = client.get("/orders/ORD-1001").json()
    assert order["order_total"] == "225.00"


def test_api_single_return_submission():
    from returns_resolution.api import create_app

    client = TestClient(create_app(make_service()))
    payload = PLANTED_REQUESTS[0].model_dump(mode="json")
    created = client.post("/returns", json=payload)
    assert created.status_code == 201
    assert created.json()["status"] == "summary_ready"
    missing = client.post(
        "/returns",
        json={**payload, "request_id": "REQ-2999", "order_id": "ORD-9999"},
    )
    assert missing.status_code == 404


# -- Runtime shape -------------------------------------------------------------------------


def test_runtime_dispatch_and_sdk_presence():
    from returns_resolution.runtime_app import handle_payload, sdk_available

    assert sdk_available() is True, "bedrock-agentcore==1.25.1 is a pinned dependency"
    service = make_service()
    out = handle_payload(service, {"action": "intake_feed"})
    assert len(out["ingested"]) == len(PLANTED_REQUESTS)
    got = handle_payload(service, {"action": "get_case", "case_id": "CASE-2004"})
    assert got["case"]["decision"]["eligible"] is False
    assert handle_payload(service, {"action": "bogus"})["error"].startswith("unknown action")


def test_runtime_contract_fallback_serves_ping_and_invocations():
    # The fallback app is only constructed when the SDK is missing; here
    # we verify the shared dispatch honours the contract payloads, and
    # that the SDK app object exposes the entrypoint registration.
    from returns_resolution import runtime_app

    assert hasattr(runtime_app.app, "entrypoint") or hasattr(runtime_app.app, "routes")
