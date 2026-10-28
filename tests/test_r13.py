"""R13 gates: the operations console.

Graded: the console is one static file served by the API; its
board, queues, money and gateway views are assembled only from
artefacts of record; the browser receives an assessment's total
and band, never its detailed payload; the review queue is exactly
the open review holds; money totals in the console's own
JavaScript equal sums over the underlying receipts and
adjustments; the static file carries none of the banned
assessment-internal vocabulary; and its only mutation calls are
the named approval and review acts the API already gates.
"""

from __future__ import annotations

import importlib.util
import os
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from returns_resolution.api import create_app
from returns_resolution.intake import PLANTED_REQUESTS
from returns_resolution.modelport import OfflineComposer
from returns_resolution.service import CaseService
from returns_resolution.store import InMemoryStore, SqlStore

ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("RETURNS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pg_only = pytest.mark.skipif(
    not PG_URL, reason="Postgres profile runs when RETURNS_TEST_DATABASE_URL is set (R13)"
)


def _check_module():
    path = ROOT / "scripts" / "check_console.py"
    spec = importlib.util.spec_from_file_location("check_console", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def console_service():
    return _check_module().build_service()


@pytest.fixture(scope="module")
def console_client(console_service):
    return TestClient(create_app(console_service))


def test_console_is_one_static_file_with_the_required_footer(console_client):
    response = console_client.get("/console")
    assert response.status_code == 200
    html = response.text
    assert html.count("<script") == 1 and "<script src" not in html
    assert "<link" not in html and "stylesheet" not in html
    assert "https://trida.ai/logos-rounded/trida-rounded.png" in html
    assert "Visit Trida AI →" in html
    assert "View all blueprints →" in html
    assert (
        "Money figures on this page are sums of artefact figures returned by the API"
        in html
    )


def test_console_data_queues_and_stages_match_the_record(console_service, console_client):
    data = console_client.get("/console/data").json()
    cases = {c["case_id"]: c for c in data["cases"]}

    assert cases["CASE-3001"]["stage"] == "closed"  # stored closed history
    assert cases["CASE-2003"]["stage"] == "graded"
    assert cases["CASE-2006"]["stage"] == "graded"
    assert cases["CASE-2004"]["stage"] == "closed"  # eligibility denied

    expected_review = sorted(
        c.case_id for c in console_service.list_cases() if c.fraud_state == "fraud_review"
    )
    assert [c["case_id"] for c in data["queues"]["review"]] == expected_review
    assert expected_review == ["CASE-3008"]

    approvals = data["queues"]["approvals"]
    assert [row["case_id"] for row in approvals] == ["CASE-2001"]
    assert approvals[0]["approvers"] == ["d.okafor"]
    assert approvals[0]["amount"] == "189.00"

    recovery = data["queues"]["recovery"]
    assert [row["case_id"] for row in recovery] == ["CASE-2003"]
    assert recovery[0]["amount"] == "14.50"
    assert recovery[0]["status"] == "open"


def test_console_money_rows_are_the_artefacts_of_record(console_service, console_client):
    data = console_client.get("/console/data").json()
    receipts = console_service.list_receipts()
    adjustments = console_service.list_all_adjustments()
    assert {r["receipt_id"] for r in data["money"]["receipts"]} == {
        r.receipt_id for r in receipts
    }
    assert {a["adjustment_id"] for a in data["money"]["adjustments"]} == {
        a.adjustment_id for a in adjustments
    }
    top_up_ids = {a.top_up_receipt_id for a in adjustments if a.top_up_receipt_id}
    for row in data["money"]["receipts"]:
        assert row["kind"] == ("top_up" if row["receipt_id"] in top_up_ids else "refund")
    assert sum(Decimal(r["amount"]) for r in data["money"]["receipts"]) == sum(
        r.amount for r in receipts
    )


def test_case_detail_is_a_complete_chain_without_assessment_payload(
    console_client,
):
    detail = console_client.get("/console/cases/CASE-2003").json()
    assert len(detail["computations"]) == 2
    assert len(detail["dispositions"]) == 2
    assert len(detail["approvals"]) == 1
    assert len(detail["executions"]) == 1
    assert len(detail["receipts"]) == 1
    assert len(detail["labels"]) == 1
    assert len(detail["warehouse_receipts"]) == 1
    assert len(detail["grading_reports"]) == 1
    assert [a["kind"] for a in detail["adjustments"]] == ["recovery_due"]
    assert detail["messages"], "the summary message stands on the chain"
    assert detail["cost_entries"], "the case cost ledger stands on the chain"
    assert detail["memory_records"], "memory milestones stand on the chain"
    for assessment in detail["assessments"]:
        assert set(assessment) == {
            "id", "total", "band", "pack_version", "as_of", "supersedes", "history_source"
        }

    def walk(value):
        if isinstance(value, dict):
            assert "signals" not in value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(detail)
    assert console_client.get("/console/cases/CASE-9999").status_code == 404


def test_console_javascript_totals_and_static_vocabulary():
    assert _check_module().main() == 0


def test_console_actions_use_the_existing_named_act_endpoints(tmp_path):
    store = SqlStore(sqlite_path=tmp_path / "r13-actions.db")
    store.migrate()
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    service.ingest(PLANTED_REQUESTS[0])  # CASE-2001, dual tier
    client = TestClient(create_app(service))

    first = client.post(
        "/cases/CASE-2001/approvals",
        json={"approver": "d.okafor", "role": "returns_lead", "as_of": "2026-10-27"},
    )
    assert first.status_code == 201
    queue = client.get("/console/data").json()["queues"]["approvals"]
    assert [row["case_id"] for row in queue] == ["CASE-2001"]

    second = client.post(
        "/cases/CASE-2001/approvals",
        json={"approver": "m.osei", "role": "finance_manager", "as_of": "2026-10-27"},
    )
    assert second.status_code == 201
    assert client.get("/console/data").json()["queues"]["approvals"] == []


@pg_only
def test_console_reads_on_the_postgres_profile(tmp_path):
    store = SqlStore(sqlite_path=tmp_path / "r13-pg.db")
    store.migrate()
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    service.ingest(PLANTED_REQUESTS[2])
    client = TestClient(create_app(service))
    assert client.get("/console").status_code == 200
    data = client.get("/console/data").json()
    assert [c["case_id"] for c in data["cases"]] == ["CASE-2003"]
    detail = client.get("/console/cases/CASE-2003").json()
    assert detail["case"]["case_id"] == "CASE-2003"
    assert detail["computations"][0]["refund_total"] == "130.50"
