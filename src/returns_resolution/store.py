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

MIGRATIONS: list[tuple[str, str, str]] = [
    ("0001", _MIGRATION_0001_SQLITE, _MIGRATION_0001_PG),
    ("0002", _MIGRATION_0002_SQLITE, _MIGRATION_0002_SQLITE),
    ("0003", _MIGRATION_0003_SQLITE, _MIGRATION_0003_SQLITE),
    ("0004", _MIGRATION_0004_SQLITE, _MIGRATION_0004_SQLITE),
]


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
    def put_disposition(self, payload: dict[str, Any]) -> None: ...
    def get_disposition(self, case_id: str) -> dict[str, Any] | None: ...
    def list_dispositions(self) -> list[dict[str, Any]]: ...
    def put_fraud_score(self, payload: dict[str, Any]) -> None: ...
    def get_fraud_score(self, score_id: str) -> dict[str, Any] | None: ...
    def list_fraud_scores(self, case_id: str) -> list[dict[str, Any]]: ...
    def list_all_fraud_scores(self) -> list[dict[str, Any]]: ...
    def put_fraud_review(self, payload: dict[str, Any]) -> None: ...
    def get_fraud_review(self, score_id: str) -> dict[str, Any] | None: ...
    def list_fraud_reviews(self, case_id: str) -> list[dict[str, Any]]: ...


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
            self._conn = sqlite3.connect(str(path))
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
            self._execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
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
        row = self._query_one("SELECT payload FROM computations WHERE case_id = ?", (case_id,))
        return json.loads(row["payload"]) if row else None

    def list_computations(self) -> list[dict[str, Any]]:
        return [json.loads(r["payload"]) for r in self._query("SELECT payload FROM computations ORDER BY case_id")]

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
        row = self._query_one("SELECT payload FROM dispositions WHERE case_id = ?", (case_id,))
        return json.loads(row["payload"]) if row else None

    def list_dispositions(self) -> list[dict[str, Any]]:
        return [json.loads(r["payload"]) for r in self._query("SELECT payload FROM dispositions ORDER BY case_id")]

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

    def migrate(self) -> list[str]:
        return ["0001", "0002", "0003", "0004"]

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
        self._computations[payload["case_id"]] = payload

    def get_computation(self, case_id: str) -> dict[str, Any] | None:
        return self._computations.get(case_id)

    def list_computations(self) -> list[dict[str, Any]]:
        return [self._computations[k] for k in sorted(self._computations)]

    def put_disposition(self, payload: dict[str, Any]) -> None:
        self._dispositions[payload["case_id"]] = payload

    def get_disposition(self, case_id: str) -> dict[str, Any] | None:
        return self._dispositions.get(case_id)

    def list_dispositions(self) -> list[dict[str, Any]]:
        return [self._dispositions[k] for k in sorted(self._dispositions)]

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
