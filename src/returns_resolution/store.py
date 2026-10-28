"""Store port: SQLite local default, Postgres when DATABASE_URL is set.

One SQL store serves both dialects. Payloads are JSON text columns in
both dialects (never JSONB): the store contract is "the exact JSON the
domain layer wrote", and a text column keeps both dialects byte-equal.
Placeholders are written `?` and translated to `%s` for psycopg.

Migrations are numbered and recorded in a ledger table; the runner
applies each exactly once and re-running applies nothing.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol

# Migration 0001 — identical logical schema in both dialects; only the
# identity column type differs, so the two strings are kept side by side.
_MIGRATION_0001_SQLITE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    case_id TEXT PRIMARY KEY,
    policy_pack_version TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS summaries (
    case_id TEXT PRIMARY KEY,
    verified INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

_MIGRATION_0001_PG = _MIGRATION_0001_SQLITE.replace(
    "applied_at TEXT NOT NULL DEFAULT (datetime('now'))",
    "applied_at TEXT NOT NULL DEFAULT ''",
)

# Migration 0002 — refund computations (R2): one artefact per case,
# keyed by its content-addressed computation id.
_MIGRATION_0002_SQLITE = """
CREATE TABLE IF NOT EXISTS computations (
    computation_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE,
    policy_pack_version TEXT NOT NULL,
    is_final INTEGER NOT NULL,
    payload TEXT NOT NULL
);
"""

# Migration 0003 — disposition decisions (R3): one artefact per
# case, keyed by its content-addressed disposition id.
_MIGRATION_0003_SQLITE = """
CREATE TABLE IF NOT EXISTS dispositions (
    disposition_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE,
    matrix_version TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

# Migration 0004 — fraud scores + review decisions (R4). Scores are
# a chain per case (a re-score supersedes, never edits), so the case
# key is deliberately NOT unique; reviews are one per score artefact,
# enforced by the UNIQUE score key here and by the service above it.
_MIGRATION_0004_SQLITE = """
CREATE TABLE IF NOT EXISTS fraud_scores (
    score_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    pack_version TEXT NOT NULL,
    band TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fraud_reviews (
    review_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    score_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL
);
"""

# Migration 0005 — refund approvals, executions and receipts (R5).
# Approvals are acts (many per case, pinned to a computation), so no
# case uniqueness there; executions and receipts are unique per
# idempotency key AND receipts unique per computation — the two
# constraints that make a double refund structurally impossible.
_MIGRATION_0005_SQLITE = """
CREATE TABLE IF NOT EXISTS refund_approvals (
    approval_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    computation_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refund_executions (
    execution_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    computation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refund_receipts (
    receipt_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    computation_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL
);
"""

# Migration 0006 — carrier artefacts + tool-call records (R6).
# Labels are a chain per case (a fresh label supersedes a voided
# one), so the case key is NOT unique; the carrier reference is.
# Voids are one per label; pickups and call records are append-only
# acts.
_MIGRATION_0006_SQLITE = """
CREATE TABLE IF NOT EXISTS return_labels (
    label_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    carrier_ref TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS label_voids (
    void_id TEXT PRIMARY KEY,
    label_id TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pickups (
    pickup_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    label_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    call_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

# Migration 0007 — warehouse grading (R7). Computations and
# dispositions become CHAINS per case: grading supersedes the
# declared-basis computation and disposition with new artefacts, and
# the superseded ones must stay stored, byte-identical — so the
# case_id UNIQUE constraints from 0002/0003 are rebuilt away (the
# artefact ids stay primary keys). Plus the round's own tables:
# warehouse receipts (one per label), grading reports (one finalised
# report per case — structural, like the service's refusal), and
# refund adjustments. The rebuild SQL is dialect-neutral, as with
# 0002-0006.
_MIGRATION_0007_SQLITE = """
CREATE TABLE IF NOT EXISTS computations_chain (
    computation_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    policy_pack_version TEXT NOT NULL,
    is_final INTEGER NOT NULL,
    payload TEXT NOT NULL
);
INSERT INTO computations_chain (computation_id, case_id, policy_pack_version, is_final, payload)
    SELECT computation_id, case_id, policy_pack_version, is_final, payload FROM computations;
DROP TABLE computations;
ALTER TABLE computations_chain RENAME TO computations;
CREATE TABLE IF NOT EXISTS dispositions_chain (
    disposition_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    matrix_version TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
INSERT INTO dispositions_chain (disposition_id, case_id, matrix_version, status, payload)
    SELECT disposition_id, case_id, matrix_version, status, payload FROM dispositions;
DROP TABLE dispositions;
ALTER TABLE dispositions_chain RENAME TO dispositions;
CREATE TABLE IF NOT EXISTS warehouse_receipts (
    warehouse_receipt_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    label_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grading_reports (
    report_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refund_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

# Migration 0008 — customer messages (R8). One row per message
# artefact: the lifecycle mutates a message's status in place (its
# transitions accumulate on the payload), so the case key is not
# unique — a case holds its whole message history, refused and
# withdrawn messages included.
_MIGRATION_0008_SQLITE = """
CREATE TABLE IF NOT EXISTS customer_messages (
    message_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

# Migration 0009 — customer memory (R9): the memory port's records
# and its access log. Records are append-only (the insert is
# conflict-ignore: a record id already standing is never
# overwritten); the access log is append-only too. There is no
# delete path for either table anywhere in the store — retention
# expiry is a read-side window over `occurred_on`, computed by the
# port, not an erasure.
_MIGRATION_0009_SQLITE = """
CREATE TABLE IF NOT EXISTS memory_records (
    record_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    occurred_on TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_access_log (
    entry_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

MIGRATIONS: list[tuple[str, str, str]] = [
    ("0001", _MIGRATION_0001_SQLITE, _MIGRATION_0001_PG),
    ("0002", _MIGRATION_0002_SQLITE, _MIGRATION_0002_SQLITE),
    ("0003", _MIGRATION_0003_SQLITE, _MIGRATION_0003_SQLITE),
    ("0004", _MIGRATION_0004_SQLITE, _MIGRATION_0004_SQLITE),
    ("0005", _MIGRATION_0005_SQLITE, _MIGRATION_0005_SQLITE),
    ("0006", _MIGRATION_0006_SQLITE, _MIGRATION_0006_SQLITE),
    ("0007", _MIGRATION_0007_SQLITE, _MIGRATION_0007_SQLITE),
    ("0008", _MIGRATION_0008_SQLITE, _MIGRATION_0008_SQLITE),
    ("0009", _MIGRATION_0009_SQLITE, _MIGRATION_0009_SQLITE),
]


def _chain_head(payloads: list[dict[str, Any]], id_key: str) -> dict[str, Any] | None:
    """The current artefact of a supersession chain: the one no
    other artefact in the chain names as its `supersedes`. A broken
    chain (every artefact superseded) falls back to the last by id —
    stated here rather than hidden, and unreachable through the
    service, which only ever appends heads."""
    if not payloads:
        return None
    superseded = {p.get("supersedes") for p in payloads} - {None}
    heads = [p for p in payloads if p[id_key] not in superseded]
    return heads[-1] if heads else payloads[-1]


class Store(Protocol):
    def migrate(self) -> list[str]: ...
    def put_order(self, payload: dict[str, Any]) -> None: ...
    def get_order(self, order_id: str) -> dict[str, Any] | None: ...
    def list_orders(self) -> list[dict[str, Any]]: ...
    def put_case(self, payload: dict[str, Any]) -> None: ...
    def get_case(self, case_id: str) -> dict[str, Any] | None: ...
    def get_case_by_request(self, request_id: str) -> dict[str, Any] | None: ...
    def list_cases(self) -> list[dict[str, Any]]: ...
    def put_decision(self, payload: dict[str, Any]) -> None: ...
    def get_decision(self, case_id: str) -> dict[str, Any] | None: ...
    def put_summary(self, case_id: str, verified: bool, payload: dict[str, Any]) -> None: ...
    def get_summary(self, case_id: str) -> dict[str, Any] | None: ...
    def put_evidence(self, payload: dict[str, Any]) -> None: ...
    def list_evidence(self, case_id: str) -> list[dict[str, Any]]: ...
    def put_computation(self, payload: dict[str, Any]) -> None: ...
    def get_computation(self, case_id: str) -> dict[str, Any] | None: ...
    def list_computations(self) -> list[dict[str, Any]]: ...
    def list_computations_for_case(self, case_id: str) -> list[dict[str, Any]]: ...
    def put_disposition(self, payload: dict[str, Any]) -> None: ...
    def get_disposition(self, case_id: str) -> dict[str, Any] | None: ...
    def list_dispositions(self) -> list[dict[str, Any]]: ...
    def list_dispositions_for_case(self, case_id: str) -> list[dict[str, Any]]: ...
    def put_fraud_score(self, payload: dict[str, Any]) -> None: ...
    def get_fraud_score(self, score_id: str) -> dict[str, Any] | None: ...
    def list_fraud_scores(self, case_id: str) -> list[dict[str, Any]]: ...
    def list_all_fraud_scores(self) -> list[dict[str, Any]]: ...
    def put_fraud_review(self, payload: dict[str, Any]) -> None: ...
    def get_fraud_review(self, score_id: str) -> dict[str, Any] | None: ...
    def list_fraud_reviews(self, case_id: str) -> list[dict[str, Any]]: ...
    def put_approval(self, payload: dict[str, Any]) -> None: ...
    def list_approvals(self, case_id: str) -> list[dict[str, Any]]: ...
    def put_execution(self, payload: dict[str, Any]) -> None: ...
    def get_execution(self, execution_id: str) -> dict[str, Any] | None: ...
    def get_execution_by_key(self, idempotency_key: str) -> dict[str, Any] | None: ...
    def list_executions(self, case_id: str) -> list[dict[str, Any]]: ...
    def put_receipt(self, payload: dict[str, Any]) -> None: ...
    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None: ...
    def get_receipt_by_key(self, idempotency_key: str) -> dict[str, Any] | None: ...
    def get_receipt_by_computation(self, computation_id: str) -> dict[str, Any] | None: ...
    def get_receipt_for_case(self, case_id: str) -> dict[str, Any] | None: ...
    def list_receipts(self) -> list[dict[str, Any]]: ...
    def put_label(self, payload: dict[str, Any]) -> None: ...
    def get_label(self, label_id: str) -> dict[str, Any] | None: ...
    def list_labels(self, case_id: str) -> list[dict[str, Any]]: ...
    def list_all_labels(self) -> list[dict[str, Any]]: ...
    def put_void(self, payload: dict[str, Any]) -> None: ...
    def get_void_by_label(self, label_id: str) -> dict[str, Any] | None: ...
    def list_voids(self, case_id: str) -> list[dict[str, Any]]: ...
    def put_pickup(self, payload: dict[str, Any]) -> None: ...
    def list_pickups(self, case_id: str) -> list[dict[str, Any]]: ...
    def put_tool_call(self, payload: dict[str, Any]) -> None: ...
    def list_tool_calls(self, case_id: str) -> list[dict[str, Any]]: ...
    def count_tool_calls(self, case_id: str) -> int: ...
    def put_warehouse_receipt(self, payload: dict[str, Any]) -> None: ...
    def get_warehouse_receipt_for_label(self, label_id: str) -> dict[str, Any] | None: ...
    def list_warehouse_receipts(self, case_id: str) -> list[dict[str, Any]]: ...
    def put_grading_report(self, payload: dict[str, Any]) -> None: ...
    def get_grading_report(self, report_id: str) -> dict[str, Any] | None: ...
    def list_grading_reports(self, case_id: str) -> list[dict[str, Any]]: ...
    def put_adjustment(self, payload: dict[str, Any]) -> None: ...
    def get_adjustment(self, adjustment_id: str) -> dict[str, Any] | None: ...
    def list_adjustments(self, case_id: str) -> list[dict[str, Any]]: ...
    def put_message(self, payload: dict[str, Any]) -> None: ...
    def get_message(self, message_id: str) -> dict[str, Any] | None: ...
    def list_messages(self, case_id: str) -> list[dict[str, Any]]: ...
    def list_all_messages(self) -> list[dict[str, Any]]: ...
    def count_messages(self, case_id: str) -> int: ...
    def put_memory_record(self, payload: dict[str, Any]) -> None: ...
    def get_memory_record(self, record_id: str) -> dict[str, Any] | None: ...
    def list_memory_records(self, customer_id: str) -> list[dict[str, Any]]: ...
    def list_memory_record_index(self) -> list[dict[str, Any]]: ...
    def put_memory_access(self, payload: dict[str, Any]) -> None: ...
    def list_memory_access(self, customer_id: str | None = None) -> list[dict[str, Any]]: ...
    def count_memory_access(self) -> int: ...


class SqlStore:
    def __init__(self, database_url: str | None = None, sqlite_path: str | Path = "./data/returns.db"):
        if database_url:
            import psycopg
            from psycopg.rows import dict_row

            self._dialect = "postgres"
            self._conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=True)
        else:
            self._dialect = "sqlite"
            path = Path(sqlite_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: the HTTP surfaces serve from a
            # threadpool, and a connection created at startup must be
            # usable from a request thread — with the default the API
            # 500s on every store read under threaded serving (found
            # by the R10 production smoke; psycopg has no such
            # restriction, which is why Postgres never showed it).
            # The store's operations are single short statements with
            # an immediate commit, so cross-thread use stays safe for
            # this single-process local profile.
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

    # -- dialect helpers ------------------------------------------------
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._dialect == "postgres" else sql

    def _execute(self, sql: str, params: tuple = ()) -> None:
        cur = self._conn.cursor()
        cur.execute(self._sql(sql), params)
        if self._dialect == "sqlite":
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(self._sql(sql), params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def _query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _exec_script(self, script: str) -> None:
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self._execute(statement)

    # -- migrations -------------------------------------------------------
    def migrate(self) -> list[str]:
        # The ledger must exist before it can be read; 0001 creates it,
        # so first run applies against a guarded probe.
        try:
            applied = {r["version"] for r in self._query("SELECT version FROM schema_migrations")}
        except Exception:
            if self._dialect == "sqlite":
                self._conn.rollback()
            applied = set()
        done: list[str] = []
        for version, sqlite_sql, pg_sql in MIGRATIONS:
            if version in applied:
                continue
            self._exec_script(pg_sql if self._dialect == "postgres" else sqlite_sql)
            # The ledger write is conflict-tolerant (R11): if the
            # version already stands recorded — another store on
            # this schema applied it first — recording it again is
            # a no-op, never a unique violation. Calling migrate()
            # on an already-migrated schema applies nothing and
            # raises nothing, in both dialects.
            self._execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?) "
                "ON CONFLICT (version) DO NOTHING",
                (version, "applied"),
            )
            done.append(version)
        return done

    # -- orders -----------------------------------------------------------
    def put_order(self, payload: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO orders (order_id, payload) VALUES (?, ?) "
            "ON CONFLICT (order_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO orders (order_id, payload) VALUES (%s, %s) "
            "ON CONFLICT (order_id) DO UPDATE SET payload = EXCLUDED.payload",
            (payload["order_id"], json.dumps(payload)),
        )

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT payload FROM orders WHERE order_id = ?", (order_id,))
        return json.loads(row["payload"]) if row else None

    def list_orders(self) -> list[dict[str, Any]]:
        return [json.loads(r["payload"]) for r in self._query("SELECT payload FROM orders ORDER BY order_id")]

    # -- cases --------------------------------------------------------------
    def put_case(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO cases (case_id, request_id, order_id, customer_id, status, payload) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (case_id) DO UPDATE SET "
            "status = excluded.status, payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO cases (case_id, request_id, order_id, customer_id, status, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (case_id) DO UPDATE SET "
            "status = EXCLUDED.status, payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["case_id"],
                payload["request"]["request_id"],
                payload["order_id"],
                payload["customer_id"],
                payload["status"],
                json.dumps(payload),
            ),
        )

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT payload FROM cases WHERE case_id = ?", (case_id,))
        return json.loads(row["payload"]) if row else None

    def get_case_by_request(self, request_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT payload FROM cases WHERE request_id = ?", (request_id,))
        return json.loads(row["payload"]) if row else None

    def list_cases(self) -> list[dict[str, Any]]:
        return [json.loads(r["payload"]) for r in self._query("SELECT payload FROM cases ORDER BY case_id")]

    # -- decisions -----------------------------------------------------------
    def put_decision(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO decisions (case_id, policy_pack_version, eligible, payload) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (case_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO decisions (case_id, policy_pack_version, eligible, payload) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (case_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["case_id"],
                payload["policy_pack_version"],
                1 if payload["eligible"] else 0,
                json.dumps(payload),
            ),
        )

    def get_decision(self, case_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT payload FROM decisions WHERE case_id = ?", (case_id,))
        return json.loads(row["payload"]) if row else None

    # -- summaries -------------------------------------------------------------
    def put_summary(self, case_id: str, verified: bool, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO summaries (case_id, verified, payload) VALUES (?, ?, ?) "
            "ON CONFLICT (case_id) DO UPDATE SET verified = excluded.verified, payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO summaries (case_id, verified, payload) VALUES (%s, %s, %s) "
            "ON CONFLICT (case_id) DO UPDATE SET verified = EXCLUDED.verified, payload = EXCLUDED.payload"
        )
        self._execute(sql, (case_id, 1 if verified else 0, json.dumps(payload)))

    def get_summary(self, case_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT payload FROM summaries WHERE case_id = ?", (case_id,))
        return json.loads(row["payload"]) if row else None

    # -- evidence ----------------------------------------------------------------
    def put_evidence(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO evidence (evidence_id, case_id, kind, uri, sha256, payload) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (evidence_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO evidence (evidence_id, case_id, kind, uri, sha256, payload) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (evidence_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["evidence_id"],
                payload["case_id"],
                payload["kind"],
                payload["uri"],
                payload["sha256"],
                json.dumps(payload),
            ),
        )

    def list_evidence(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query("SELECT payload FROM evidence WHERE case_id = ? ORDER BY evidence_id", (case_id,))
        return [json.loads(r["payload"]) for r in rows]

    # -- computations (R2) -----------------------------------------------------
    def put_computation(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO computations (computation_id, case_id, policy_pack_version, is_final, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (computation_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO computations (computation_id, case_id, policy_pack_version, is_final, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (computation_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["computation_id"],
                payload["case_id"],
                payload["policy_pack_version"],
                1 if payload["is_final"] else 0,
                json.dumps(payload),
            ),
        )

    def get_computation(self, case_id: str) -> dict[str, Any] | None:
        # The CURRENT computation: the head of the case's supersession
        # chain (R7). Before R7 every chain has exactly one member.
        return _chain_head(self.list_computations_for_case(case_id), "computation_id")

    def list_computations(self) -> list[dict[str, Any]]:
        return [json.loads(r["payload"]) for r in self._query("SELECT payload FROM computations ORDER BY case_id")]

    def list_computations_for_case(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM computations WHERE case_id = ? ORDER BY computation_id",
            (case_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- dispositions (R3) ---------------------------------------------------
    def put_disposition(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO dispositions (disposition_id, case_id, matrix_version, status, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (disposition_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO dispositions (disposition_id, case_id, matrix_version, status, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (disposition_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["disposition_id"],
                payload["case_id"],
                payload["matrix_version"],
                payload["status"],
                json.dumps(payload),
            ),
        )

    def get_disposition(self, case_id: str) -> dict[str, Any] | None:
        # The CURRENT disposition: the head of the case's supersession
        # chain (R7). Before R7 every chain has exactly one member.
        return _chain_head(self.list_dispositions_for_case(case_id), "disposition_id")

    def list_dispositions(self) -> list[dict[str, Any]]:
        return [json.loads(r["payload"]) for r in self._query("SELECT payload FROM dispositions ORDER BY case_id")]

    def list_dispositions_for_case(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM dispositions WHERE case_id = ? ORDER BY disposition_id",
            (case_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- fraud scores + reviews (R4) --------------------------------------
    def put_fraud_score(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO fraud_scores (score_id, case_id, pack_version, band, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (score_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO fraud_scores (score_id, case_id, pack_version, band, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (score_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["score_id"],
                payload["case_id"],
                payload["pack_version"],
                payload["band"],
                json.dumps(payload),
            ),
        )

    def get_fraud_score(self, score_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT payload FROM fraud_scores WHERE score_id = ?", (score_id,))
        return json.loads(row["payload"]) if row else None

    def list_fraud_scores(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM fraud_scores WHERE case_id = ? ORDER BY score_id", (case_id,)
        )
        return [json.loads(r["payload"]) for r in rows]

    def list_all_fraud_scores(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT payload FROM fraud_scores ORDER BY case_id, score_id")
        return [json.loads(r["payload"]) for r in rows]

    def put_fraud_review(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO fraud_reviews (review_id, case_id, score_id, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (review_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO fraud_reviews (review_id, case_id, score_id, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (review_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["review_id"],
                payload["case_id"],
                payload["score_id"],
                json.dumps(payload),
            ),
        )

    def get_fraud_review(self, score_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT payload FROM fraud_reviews WHERE score_id = ?", (score_id,))
        return json.loads(row["payload"]) if row else None

    def list_fraud_reviews(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM fraud_reviews WHERE case_id = ? ORDER BY review_id", (case_id,)
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- approvals, executions, receipts (R5) ------------------------------
    def put_approval(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO refund_approvals (approval_id, case_id, computation_id, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (approval_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO refund_approvals (approval_id, case_id, computation_id, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (approval_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["approval_id"],
                payload["case_id"],
                payload["computation_id"],
                json.dumps(payload),
            ),
        )

    def list_approvals(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM refund_approvals WHERE case_id = ? ORDER BY approval_id",
            (case_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    def put_execution(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO refund_executions (execution_id, case_id, computation_id, idempotency_key, status, payload) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (execution_id) DO UPDATE SET "
            "status = excluded.status, payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO refund_executions (execution_id, case_id, computation_id, idempotency_key, status, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (execution_id) DO UPDATE SET "
            "status = EXCLUDED.status, payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["execution_id"],
                payload["case_id"],
                payload["computation_id"],
                payload["idempotency_key"],
                payload["status"],
                json.dumps(payload),
            ),
        )

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT payload FROM refund_executions WHERE execution_id = ?", (execution_id,)
        )
        return json.loads(row["payload"]) if row else None

    def get_execution_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT payload FROM refund_executions WHERE idempotency_key = ?", (idempotency_key,)
        )
        return json.loads(row["payload"]) if row else None

    def list_executions(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM refund_executions WHERE case_id = ? ORDER BY execution_id",
            (case_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    def put_receipt(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO refund_receipts (receipt_id, case_id, computation_id, idempotency_key, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (receipt_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO refund_receipts (receipt_id, case_id, computation_id, idempotency_key, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (receipt_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["receipt_id"],
                payload["case_id"],
                payload["computation_id"],
                payload["idempotency_key"],
                json.dumps(payload),
            ),
        )

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT payload FROM refund_receipts WHERE receipt_id = ?", (receipt_id,)
        )
        return json.loads(row["payload"]) if row else None

    def get_receipt_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT payload FROM refund_receipts WHERE idempotency_key = ?", (idempotency_key,)
        )
        return json.loads(row["payload"]) if row else None

    def get_receipt_by_computation(self, computation_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT payload FROM refund_receipts WHERE computation_id = ?", (computation_id,)
        )
        return json.loads(row["payload"]) if row else None

    def get_receipt_for_case(self, case_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT payload FROM refund_receipts WHERE case_id = ?", (case_id,)
        )
        return json.loads(row["payload"]) if row else None

    def list_receipts(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT payload FROM refund_receipts ORDER BY case_id, receipt_id")
        return [json.loads(r["payload"]) for r in rows]

    # -- labels, voids, pickups, tool calls (R6) -------------------------
    def put_label(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO return_labels (label_id, case_id, carrier_ref, status, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (label_id) DO UPDATE SET "
            "status = excluded.status, payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO return_labels (label_id, case_id, carrier_ref, status, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (label_id) DO UPDATE SET "
            "status = EXCLUDED.status, payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["label_id"],
                payload["case_id"],
                payload["carrier_ref"],
                payload["status"],
                json.dumps(payload),
            ),
        )

    def get_label(self, label_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT payload FROM return_labels WHERE label_id = ?", (label_id,))
        return json.loads(row["payload"]) if row else None

    def list_labels(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM return_labels WHERE case_id = ? ORDER BY label_id", (case_id,)
        )
        return [json.loads(r["payload"]) for r in rows]

    def list_all_labels(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT payload FROM return_labels ORDER BY case_id, label_id")
        return [json.loads(r["payload"]) for r in rows]

    def put_void(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO label_voids (void_id, label_id, case_id, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (void_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO label_voids (void_id, label_id, case_id, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (void_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["void_id"],
                payload["label_id"],
                payload["case_id"],
                json.dumps(payload),
            ),
        )

    def get_void_by_label(self, label_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT payload FROM label_voids WHERE label_id = ?", (label_id,))
        return json.loads(row["payload"]) if row else None

    def list_voids(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM label_voids WHERE case_id = ? ORDER BY void_id", (case_id,)
        )
        return [json.loads(r["payload"]) for r in rows]

    def put_pickup(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO pickups (pickup_id, case_id, label_id, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (pickup_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO pickups (pickup_id, case_id, label_id, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (pickup_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["pickup_id"],
                payload["case_id"],
                payload["label_id"],
                json.dumps(payload),
            ),
        )

    def list_pickups(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM pickups WHERE case_id = ? ORDER BY pickup_id", (case_id,)
        )
        return [json.loads(r["payload"]) for r in rows]

    def put_tool_call(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO tool_calls (call_id, case_id, tool, status, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (call_id) DO UPDATE SET "
            "status = excluded.status, payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO tool_calls (call_id, case_id, tool, status, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (call_id) DO UPDATE SET "
            "status = EXCLUDED.status, payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["call_id"],
                payload["case_id"],
                payload["tool"],
                payload["status"],
                json.dumps(payload),
            ),
        )

    def list_tool_calls(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM tool_calls WHERE case_id = ? ORDER BY call_id", (case_id,)
        )
        return [json.loads(r["payload"]) for r in rows]

    def count_tool_calls(self, case_id: str) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM tool_calls WHERE case_id = ?", (case_id,))
        return int(rows[0]["n"]) if rows else 0

    # -- warehouse receipts, grading reports, adjustments (R7) -----------
    def put_warehouse_receipt(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO warehouse_receipts (warehouse_receipt_id, case_id, label_id, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (warehouse_receipt_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO warehouse_receipts (warehouse_receipt_id, case_id, label_id, payload) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (warehouse_receipt_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["warehouse_receipt_id"],
                payload["case_id"],
                payload["label_id"],
                json.dumps(payload),
            ),
        )

    def get_warehouse_receipt_for_label(self, label_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT payload FROM warehouse_receipts WHERE label_id = ?", (label_id,)
        )
        return json.loads(row["payload"]) if row else None

    def list_warehouse_receipts(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM warehouse_receipts WHERE case_id = ? ORDER BY warehouse_receipt_id",
            (case_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    def put_grading_report(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO grading_reports (report_id, case_id, payload) "
            "VALUES (?, ?, ?) ON CONFLICT (report_id) DO UPDATE SET payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO grading_reports (report_id, case_id, payload) "
            "VALUES (%s, %s, %s) ON CONFLICT (report_id) DO UPDATE SET payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (payload["report_id"], payload["case_id"], json.dumps(payload)),
        )

    def get_grading_report(self, report_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT payload FROM grading_reports WHERE report_id = ?", (report_id,)
        )
        return json.loads(row["payload"]) if row else None

    def list_grading_reports(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM grading_reports WHERE case_id = ? ORDER BY report_id",
            (case_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    def put_adjustment(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO refund_adjustments (adjustment_id, case_id, kind, status, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (adjustment_id) DO UPDATE SET "
            "status = excluded.status, payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO refund_adjustments (adjustment_id, case_id, kind, status, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (adjustment_id) DO UPDATE SET "
            "status = EXCLUDED.status, payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["adjustment_id"],
                payload["case_id"],
                payload["kind"],
                payload["status"],
                json.dumps(payload),
            ),
        )

    def get_adjustment(self, adjustment_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT payload FROM refund_adjustments WHERE adjustment_id = ?", (adjustment_id,)
        )
        return json.loads(row["payload"]) if row else None

    def list_adjustments(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM refund_adjustments WHERE case_id = ? ORDER BY adjustment_id",
            (case_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- customer messages (R8) -------------------------------------------
    def put_message(self, payload: dict[str, Any]) -> None:
        sql = (
            "INSERT INTO customer_messages (message_id, case_id, message_type, status, payload) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (message_id) DO UPDATE SET "
            "status = excluded.status, payload = excluded.payload"
            if self._dialect == "sqlite"
            else "INSERT INTO customer_messages (message_id, case_id, message_type, status, payload) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (message_id) DO UPDATE SET "
            "status = EXCLUDED.status, payload = EXCLUDED.payload"
        )
        self._execute(
            sql,
            (
                payload["message_id"],
                payload["case_id"],
                payload["message_type"],
                payload["status"],
                json.dumps(payload),
            ),
        )

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT payload FROM customer_messages WHERE message_id = ?", (message_id,)
        )
        return json.loads(row["payload"]) if row else None

    def list_messages(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM customer_messages WHERE case_id = ? ORDER BY message_id",
            (case_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    def list_all_messages(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT payload FROM customer_messages ORDER BY case_id, message_id")
        return [json.loads(r["payload"]) for r in rows]

    def count_messages(self, case_id: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM customer_messages WHERE case_id = ?", (case_id,)
        )
        return int(rows[0]["n"]) if rows else 0

    # -- customer memory (R9) ----------------------------------------------
    def put_memory_record(self, payload: dict[str, Any]) -> None:
        # Append-only: a record id already standing is never
        # overwritten — memory records are immutable once written.
        self._execute(
            "INSERT INTO memory_records "
            "(record_id, customer_id, case_id, event_type, source_ref, occurred_on, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (record_id) DO NOTHING",
            (
                payload["record_id"],
                payload["customer_id"],
                payload["case_id"],
                payload["event_type"],
                payload["source_ref"],
                payload["occurred_on"],
                json.dumps(payload),
            ),
        )

    def get_memory_record(self, record_id: str) -> dict[str, Any] | None:
        row = self._query_one(
            "SELECT payload FROM memory_records WHERE record_id = ?", (record_id,)
        )
        return json.loads(row["payload"]) if row else None

    def list_memory_records(self, customer_id: str) -> list[dict[str, Any]]:
        # The customer predicate is IN THE QUERY: this is the only
        # per-customer record read, and it cannot return another
        # customer's rows.
        rows = self._query(
            "SELECT payload FROM memory_records WHERE customer_id = ? "
            "ORDER BY occurred_on, record_id",
            (customer_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    def list_memory_record_index(self) -> list[dict[str, Any]]:
        # Write-side index only: ids and keys, no facts — sync
        # reconciles against this, it is not a retrieval surface.
        rows = self._query(
            "SELECT record_id, customer_id, case_id, event_type, source_ref "
            "FROM memory_records ORDER BY record_id"
        )
        return [dict(r) for r in rows]

    def put_memory_access(self, payload: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO memory_access_log (entry_id, customer_id, purpose, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (entry_id) DO NOTHING",
            (
                payload["entry_id"],
                payload["customer_id"],
                payload["purpose"],
                json.dumps(payload),
            ),
        )

    def list_memory_access(self, customer_id: str | None = None) -> list[dict[str, Any]]:
        if customer_id is None:
            rows = self._query("SELECT payload FROM memory_access_log")
        else:
            rows = self._query(
                "SELECT payload FROM memory_access_log WHERE customer_id = ?",
                (customer_id,),
            )
        entries = [json.loads(r["payload"]) for r in rows]
        return sorted(entries, key=lambda e: (e["seq"], e["entry_id"]))

    def count_memory_access(self) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM memory_access_log")
        return int(rows[0]["n"]) if rows else 0


class InMemoryStore:
    """Test double implementing the same port with dicts."""

    def __init__(self) -> None:
        self._orders: dict[str, dict] = {}
        self._cases: dict[str, dict] = {}
        self._decisions: dict[str, dict] = {}
        self._summaries: dict[str, dict] = {}
        self._evidence: dict[str, dict] = {}
        self._computations: dict[str, dict] = {}
        self._dispositions: dict[str, dict] = {}
        self._fraud_scores: dict[str, dict] = {}
        self._fraud_reviews: dict[str, dict] = {}
        self._approvals: dict[str, dict] = {}
        self._executions: dict[str, dict] = {}
        self._receipts: dict[str, dict] = {}
        self._labels: dict[str, dict] = {}
        self._voids: dict[str, dict] = {}
        self._pickups: dict[str, dict] = {}
        self._tool_calls: dict[str, dict] = {}
        self._warehouse_receipts: dict[str, dict] = {}
        self._grading_reports: dict[str, dict] = {}
        self._adjustments: dict[str, dict] = {}
        self._messages: dict[str, dict] = {}
        self._memory_records: dict[str, dict] = {}
        self._memory_access: dict[str, dict] = {}

    def migrate(self) -> list[str]:
        return ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009"]

    def put_order(self, payload: dict[str, Any]) -> None:
        self._orders[payload["order_id"]] = payload

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        return self._orders.get(order_id)

    def list_orders(self) -> list[dict[str, Any]]:
        return [self._orders[k] for k in sorted(self._orders)]

    def put_case(self, payload: dict[str, Any]) -> None:
        self._cases[payload["case_id"]] = payload

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        return self._cases.get(case_id)

    def get_case_by_request(self, request_id: str) -> dict[str, Any] | None:
        for case in self._cases.values():
            if case["request"]["request_id"] == request_id:
                return case
        return None

    def list_cases(self) -> list[dict[str, Any]]:
        return [self._cases[k] for k in sorted(self._cases)]

    def put_decision(self, payload: dict[str, Any]) -> None:
        self._decisions[payload["case_id"]] = payload

    def get_decision(self, case_id: str) -> dict[str, Any] | None:
        return self._decisions.get(case_id)

    def put_summary(self, case_id: str, verified: bool, payload: dict[str, Any]) -> None:
        self._summaries[case_id] = payload

    def get_summary(self, case_id: str) -> dict[str, Any] | None:
        return self._summaries.get(case_id)

    def put_evidence(self, payload: dict[str, Any]) -> None:
        self._evidence[payload["evidence_id"]] = payload

    def list_evidence(self, case_id: str) -> list[dict[str, Any]]:
        return [e for e in self._evidence.values() if e["case_id"] == case_id]

    def put_computation(self, payload: dict[str, Any]) -> None:
        self._computations[payload["computation_id"]] = payload

    def get_computation(self, case_id: str) -> dict[str, Any] | None:
        return _chain_head(self.list_computations_for_case(case_id), "computation_id")

    def list_computations(self) -> list[dict[str, Any]]:
        return sorted(
            self._computations.values(),
            key=lambda p: (p["case_id"], p["computation_id"]),
        )

    def list_computations_for_case(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._computations[k]
            for k in sorted(self._computations)
            if self._computations[k]["case_id"] == case_id
        ]

    def put_disposition(self, payload: dict[str, Any]) -> None:
        self._dispositions[payload["disposition_id"]] = payload

    def get_disposition(self, case_id: str) -> dict[str, Any] | None:
        return _chain_head(self.list_dispositions_for_case(case_id), "disposition_id")

    def list_dispositions(self) -> list[dict[str, Any]]:
        return sorted(
            self._dispositions.values(),
            key=lambda p: (p["case_id"], p["disposition_id"]),
        )

    def list_dispositions_for_case(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._dispositions[k]
            for k in sorted(self._dispositions)
            if self._dispositions[k]["case_id"] == case_id
        ]

    def put_fraud_score(self, payload: dict[str, Any]) -> None:
        self._fraud_scores[payload["score_id"]] = payload

    def get_fraud_score(self, score_id: str) -> dict[str, Any] | None:
        return self._fraud_scores.get(score_id)

    def list_fraud_scores(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._fraud_scores[k]
            for k in sorted(self._fraud_scores)
            if self._fraud_scores[k]["case_id"] == case_id
        ]

    def list_all_fraud_scores(self) -> list[dict[str, Any]]:
        return [self._fraud_scores[k] for k in sorted(self._fraud_scores)]

    def put_fraud_review(self, payload: dict[str, Any]) -> None:
        self._fraud_reviews[payload["review_id"]] = payload

    def get_fraud_review(self, score_id: str) -> dict[str, Any] | None:
        for review in self._fraud_reviews.values():
            if review["score_id"] == score_id:
                return review
        return None

    def list_fraud_reviews(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._fraud_reviews[k]
            for k in sorted(self._fraud_reviews)
            if self._fraud_reviews[k]["case_id"] == case_id
        ]

    def put_approval(self, payload: dict[str, Any]) -> None:
        self._approvals[payload["approval_id"]] = payload

    def list_approvals(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._approvals[k]
            for k in sorted(self._approvals)
            if self._approvals[k]["case_id"] == case_id
        ]

    def put_execution(self, payload: dict[str, Any]) -> None:
        self._executions[payload["execution_id"]] = payload

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        return self._executions.get(execution_id)

    def get_execution_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        for execution in self._executions.values():
            if execution["idempotency_key"] == idempotency_key:
                return execution
        return None

    def list_executions(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._executions[k]
            for k in sorted(self._executions)
            if self._executions[k]["case_id"] == case_id
        ]

    def put_receipt(self, payload: dict[str, Any]) -> None:
        self._receipts[payload["receipt_id"]] = payload

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        return self._receipts.get(receipt_id)

    def get_receipt_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        for receipt in self._receipts.values():
            if receipt["idempotency_key"] == idempotency_key:
                return receipt
        return None

    def get_receipt_by_computation(self, computation_id: str) -> dict[str, Any] | None:
        for receipt in self._receipts.values():
            if receipt["computation_id"] == computation_id:
                return receipt
        return None

    def get_receipt_for_case(self, case_id: str) -> dict[str, Any] | None:
        for receipt in self._receipts.values():
            if receipt["case_id"] == case_id:
                return receipt
        return None

    def list_receipts(self) -> list[dict[str, Any]]:
        return [self._receipts[k] for k in sorted(self._receipts)]

    def put_label(self, payload: dict[str, Any]) -> None:
        self._labels[payload["label_id"]] = payload

    def get_label(self, label_id: str) -> dict[str, Any] | None:
        return self._labels.get(label_id)

    def list_labels(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._labels[k]
            for k in sorted(self._labels)
            if self._labels[k]["case_id"] == case_id
        ]

    def list_all_labels(self) -> list[dict[str, Any]]:
        return [self._labels[k] for k in sorted(self._labels)]

    def put_void(self, payload: dict[str, Any]) -> None:
        self._voids[payload["void_id"]] = payload

    def get_void_by_label(self, label_id: str) -> dict[str, Any] | None:
        for void in self._voids.values():
            if void["label_id"] == label_id:
                return void
        return None

    def list_voids(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._voids[k]
            for k in sorted(self._voids)
            if self._voids[k]["case_id"] == case_id
        ]

    def put_pickup(self, payload: dict[str, Any]) -> None:
        self._pickups[payload["pickup_id"]] = payload

    def list_pickups(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._pickups[k]
            for k in sorted(self._pickups)
            if self._pickups[k]["case_id"] == case_id
        ]

    def put_tool_call(self, payload: dict[str, Any]) -> None:
        self._tool_calls[payload["call_id"]] = payload

    def list_tool_calls(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._tool_calls[k]
            for k in sorted(self._tool_calls)
            if self._tool_calls[k]["case_id"] == case_id
        ]

    def count_tool_calls(self, case_id: str) -> int:
        return sum(1 for c in self._tool_calls.values() if c["case_id"] == case_id)

    def put_warehouse_receipt(self, payload: dict[str, Any]) -> None:
        self._warehouse_receipts[payload["warehouse_receipt_id"]] = payload

    def get_warehouse_receipt_for_label(self, label_id: str) -> dict[str, Any] | None:
        for receipt in self._warehouse_receipts.values():
            if receipt["label_id"] == label_id:
                return receipt
        return None

    def list_warehouse_receipts(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._warehouse_receipts[k]
            for k in sorted(self._warehouse_receipts)
            if self._warehouse_receipts[k]["case_id"] == case_id
        ]

    def put_grading_report(self, payload: dict[str, Any]) -> None:
        self._grading_reports[payload["report_id"]] = payload

    def get_grading_report(self, report_id: str) -> dict[str, Any] | None:
        return self._grading_reports.get(report_id)

    def list_grading_reports(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._grading_reports[k]
            for k in sorted(self._grading_reports)
            if self._grading_reports[k]["case_id"] == case_id
        ]

    def put_adjustment(self, payload: dict[str, Any]) -> None:
        self._adjustments[payload["adjustment_id"]] = payload

    def get_adjustment(self, adjustment_id: str) -> dict[str, Any] | None:
        return self._adjustments.get(adjustment_id)

    def list_adjustments(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._adjustments[k]
            for k in sorted(self._adjustments)
            if self._adjustments[k]["case_id"] == case_id
        ]

    def put_message(self, payload: dict[str, Any]) -> None:
        self._messages[payload["message_id"]] = payload

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        return self._messages.get(message_id)

    def list_messages(self, case_id: str) -> list[dict[str, Any]]:
        return [
            self._messages[k]
            for k in sorted(self._messages)
            if self._messages[k]["case_id"] == case_id
        ]

    def list_all_messages(self) -> list[dict[str, Any]]:
        return [self._messages[k] for k in sorted(self._messages)]

    def count_messages(self, case_id: str) -> int:
        return sum(1 for m in self._messages.values() if m["case_id"] == case_id)

    def put_memory_record(self, payload: dict[str, Any]) -> None:
        # Append-only, like the SQL store's conflict-ignore insert:
        # a record id already standing is never overwritten.
        self._memory_records.setdefault(payload["record_id"], payload)

    def get_memory_record(self, record_id: str) -> dict[str, Any] | None:
        return self._memory_records.get(record_id)

    def list_memory_records(self, customer_id: str) -> list[dict[str, Any]]:
        rows = [
            r for r in self._memory_records.values() if r["customer_id"] == customer_id
        ]
        return sorted(rows, key=lambda r: (r["occurred_on"], r["record_id"]))

    def list_memory_record_index(self) -> list[dict[str, Any]]:
        return [
            {
                "record_id": r["record_id"],
                "customer_id": r["customer_id"],
                "case_id": r["case_id"],
                "event_type": r["event_type"],
                "source_ref": r["source_ref"],
            }
            for r in (self._memory_records[k] for k in sorted(self._memory_records))
        ]

    def put_memory_access(self, payload: dict[str, Any]) -> None:
        self._memory_access.setdefault(payload["entry_id"], payload)

    def list_memory_access(self, customer_id: str | None = None) -> list[dict[str, Any]]:
        entries = [
            e
            for e in self._memory_access.values()
            if customer_id is None or e["customer_id"] == customer_id
        ]
        return sorted(entries, key=lambda e: (e["seq"], e["entry_id"]))

    def count_memory_access(self) -> int:
        return len(self._memory_access)
