"""R10 gates: the Postgres production profile + production smoke
contracts.

- The threaded-serving regression: the API serves from a threadpool,
  and a SQLite connection created at startup must answer from a
  request thread (the defect the production smoke found; Postgres
  never had it).
- Cross-profile artefact identity: the same planted work on
  InMemoryStore, SqlStore/SQLite and (when a test URL is set)
  SqlStore/Postgres mints byte-identical content-addressed ids.
- Postgres-only legs: the migration ledger on a fresh schema, the
  R7 chain-head resolution after grading, and memory access-log
  ordering — all against real PostgreSQL.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from returns_resolution.api import create_app
from returns_resolution.intake import FRAUD_FEED_REQUESTS, PLANTED_REQUESTS
from returns_resolution.modelport import OfflineComposer
from returns_resolution.service import CaseService
from returns_resolution.store import InMemoryStore, SqlStore

PG_URL = os.environ.get("RETURNS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pg_only = pytest.mark.skipif(
    not PG_URL, reason="Postgres profile runs when RETURNS_TEST_DATABASE_URL is set (R10)"
)

DAY = date(2026, 10, 27)


# Run-scoped (R11 hermeticity): pytest reuses tmp-path names
# across runs, so the schema suffix carries a per-run token — a
# second run against a persistent cluster never lands on the
# first run's schemas.
_RUN_TOKEN = os.environ.get("RETURNS_RUN_ID") or uuid.uuid4().hex[:12]


def _pg_store(tmp_path) -> SqlStore:
    import psycopg

    schema = "t_" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:20] + "_" + _RUN_TOKEN
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    sep = "&" if "?" in PG_URL else "?"
    store = SqlStore(database_url=f"{PG_URL}{sep}options=-csearch_path%3D{schema}")
    store.migrate()
    return store


# -- threaded serving (the smoke's defect) ---------------------------------


def test_api_on_sqlite_store_answers_from_a_request_thread(tmp_path):
    store = SqlStore(sqlite_path=tmp_path / "threaded.db")
    store.migrate()
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    service.ingest(PLANTED_REQUESTS[2])
    client = TestClient(create_app(service), raise_server_exceptions=False)
    resp = client.get("/cases/CASE-2003/decision")
    assert resp.status_code == 200, resp.text
    assert resp.json()["eligible"] is True


# -- cross-profile artefact identity -----------------------------------------


def _canonical_fingerprint(store) -> dict:
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    service.seed_history()
    for request in PLANTED_REQUESTS:
        service.ingest(request)
    for request in FRAUD_FEED_REQUESTS:
        service.ingest(request)
    # Money moves on the auto-tier case; a label is issued on the
    # boots case; a message walks its lifecycle on the executed case.
    receipt = service.execute_refund("CASE-2006", idempotency_key="r10-fp", as_of=DAY)
    service.invoke_tool(
        "create_return_label",
        {"case_id": "CASE-2003", "issued_on": DAY},
        case_id="CASE-2003",
        called_on=DAY,
    )
    message = service.draft_message("CASE-2006", "refund_executed_notice", as_of=DAY)
    service.verify_message(message.message_id, as_of=DAY)
    service.sync_memory()
    return {
        "receipt_id": receipt.receipt_id,
        "computations": sorted(
            c["computation_id"] for c in store.list_computations()
        ),
        "dispositions": sorted(
            d["disposition_id"] for d in store.list_dispositions()
        ),
        "fraud_scores": sorted(
            s["score_id"] for s in store.list_all_fraud_scores()
        ),
        "labels": sorted(lbl["label_id"] for lbl in store.list_all_labels()),
        "messages": sorted(m["message_id"] for m in store.list_all_messages()),
        "memory_records": sorted(
            row["record_id"] for row in store.list_memory_record_index()
        ),
    }


def test_artefact_ids_are_identical_across_profiles(tmp_path):
    memory_fp = _canonical_fingerprint(InMemoryStore())
    sqlite_store = SqlStore(sqlite_path=tmp_path / "fp.db")
    sqlite_store.migrate()
    sqlite_fp = _canonical_fingerprint(sqlite_store)
    assert sqlite_fp == memory_fp
    if PG_URL:
        pg_fp = _canonical_fingerprint(_pg_store(tmp_path / "pg"))
        assert pg_fp == memory_fp


# -- Postgres-only legs --------------------------------------------------------


@pg_only
def test_postgres_migration_ledger_applies_once(tmp_path):
    store = _pg_store(tmp_path)
    assert store.migrate() == [], "the factory migrated; a re-run applies nothing"
    versions = [
        r["version"]
        for r in store._query("SELECT version FROM schema_migrations ORDER BY version")
    ]
    assert versions == [f"000{i}" for i in range(1, 10)] + ["0010"]


@pg_only
def test_postgres_chain_head_after_grading(tmp_path):
    store = _pg_store(tmp_path)
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    case = service.ingest(PLANTED_REQUESTS[2])  # CASE-2003 boots
    before = service.get_computation(case.case_id)
    service.invoke_tool(
        "create_return_label",
        {"case_id": case.case_id, "issued_on": DAY},
        case_id=case.case_id,
        called_on=DAY,
    )
    service.record_warehouse_receipt(
        case.case_id, received_by="w.chen", received_on=date(2026, 10, 30)
    )
    service.record_grading(
        case.case_id,
        grader="p.novak",
        graded_condition="used",
        graded_on=date(2026, 10, 31),
        damage_notes="heel counter scuffed",
    )
    chain = store.list_computations_for_case(case.case_id)
    assert len(chain) == 2, "declared + graded computations both stand"
    head = service.get_computation(case.case_id)
    assert head.computation_id != before.computation_id
    assert head.computation_id == store.get_computation(case.case_id)["computation_id"]
    disp_chain = store.list_dispositions_for_case(case.case_id)
    assert len(disp_chain) == 2
    assert service.get_disposition(case.case_id).basis == "graded"


@pg_only
def test_postgres_memory_access_log_keeps_sequence_order(tmp_path):
    store = _pg_store(tmp_path)
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    service.seed_history()
    for request in PLANTED_REQUESTS[:3]:
        service.ingest(request)
    service.sync_memory()
    customer = service.get_case("CASE-2001").customer_id
    service.retrieve_memory(customer, purpose="first", actor="test")
    service.retrieve_memory(customer, purpose="second", actor="test")
    entries = service.list_memory_access_log(customer)
    assert entries, "retrievals are logged"
    seqs = [e.seq for e in entries]
    assert seqs == sorted(seqs), "the access log reads back in sequence order"
    purposes = [e.purpose for e in entries if e.purpose in ("first", "second")]
    assert purposes == ["first", "second"]
