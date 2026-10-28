"""Postgres profile for the suite (R10; hermetic since R11).

With no environment set, the suite runs exactly as it always has:
InMemoryStore doubles plus SqlStore on per-test SQLite files.

When RETURNS_TEST_DATABASE_URL (or DATABASE_URL) points at a real
PostgreSQL, every SqlStore a test builds against a sqlite path is
redirected to a private schema on that database, named by a hash of
the path — so the whole suite runs against real Postgres while each
test keeps the isolation its tmp path gave it on SQLite, and a
"reopened" store on the same path lands on the same schema, exactly
like reopening the same file. Stores built with an explicit
database_url (the R1 dialect test) are left alone.

Hermeticity (R11): the schema name also carries a RUN TOKEN —
unique per suite run (RETURNS_RUN_ID if set, else generated here).
Pytest reuses tmp-path names across runs, so a path-only name made
a second run against a persistent cluster land on the first run's
already-populated schemas: migration-ledger collisions and memory
records leaking across runs. With the token, every run starts on
fresh schemas and two consecutive runs against one cluster are
independent. The token is constant within a run, so the "reopened
store" legs keep their same-schema semantics.
"""

from __future__ import annotations

import hashlib
import os
import uuid

_PG_URL = os.environ.get("RETURNS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
RUN_TOKEN = os.environ.get("RETURNS_RUN_ID") or uuid.uuid4().hex[:12]

if _PG_URL:
    import psycopg

    from returns_resolution.store import SqlStore

    _orig_init = SqlStore.__init__

    def _schema_for(sqlite_path: object) -> str:
        digest = hashlib.sha256(str(sqlite_path).encode()).hexdigest()[:20]
        return f"t_{digest}_{RUN_TOKEN}"

    def _ensure_schema(schema: str) -> None:
        with psycopg.connect(_PG_URL, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

    def _redirected_init(self, database_url=None, sqlite_path="./data/returns.db"):
        if database_url:
            return _orig_init(self, database_url=database_url, sqlite_path=sqlite_path)
        schema = _schema_for(sqlite_path)
        _ensure_schema(schema)
        sep = "&" if "?" in _PG_URL else "?"
        # search_path is the private schema ONLY: with `,public`
        # appended, an unmigrated private schema silently resolves
        # its tables against an already-migrated public schema and
        # migrate() reports nothing to do (found by the R10 PG run).
        url = f"{_PG_URL}{sep}options=-csearch_path%3D{schema}"
        return _orig_init(self, database_url=url, sqlite_path=sqlite_path)

    SqlStore.__init__ = _redirected_init  # type: ignore[method-assign]
