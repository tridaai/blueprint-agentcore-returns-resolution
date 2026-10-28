"""Production smoke (R10): the whole case lifecycle over HTTP.

Drives the planted CASE-2003 (boots, declared like-new) through the
full walk — ingest → decide → compute → disposition → fraud score →
approve → execute (with an idempotent replay) → label → carrier
receipt → warehouse grading → adjustment → message lifecycle →
memory parity — asserting every step over HTTP:

- the Runtime entrypoint runs as a real subprocess server (the same
  module the container runs) and performs the intake; its /ping,
  receipt read and from-memory score read are exercised over HTTP;
- the operations API (FastAPI) is driven through its ASGI transport;
- BOTH surfaces sit on ONE store: the Postgres profile when
  DATABASE_URL (or RETURNS_DATABASE_URL) is set — a private,
  run-scoped `prod_smoke_<token>` schema on that database —
  otherwise a throwaway SQLite file, and the profile used is
  printed in the ledger.

Every step prints a ledger line; any failed assertion is recorded,
the walk continues where it safely can, and the script exits
nonzero if any step failed.

Run: uv run python scripts/prod_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BASE = "http://127.0.0.1:8080"
CASE = "CASE-2003"  # boots: eligible, $130.50 single-approval refund
DAY = "2026-10-27"


def _money(value) -> Decimal:
    if value is None:
        return Decimal("NaN")  # never equals a real amount; == stays safe
    return Decimal(str(value))


class Ledger:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"[{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)
        return ok

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        return self.step(name, bool(condition), detail)


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=5) as resp:
        return json.loads(resp.read())


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _profile(tmp: Path) -> tuple[str, str]:
    """(profile label, store DSN/path description) + env for the runtime."""
    pg_url = os.environ.get("DATABASE_URL") or os.environ.get("RETURNS_DATABASE_URL")
    if pg_url:
        import uuid

        import psycopg

        # Run-scoped schema (R11 hermeticity): unique per smoke run,
        # so a persistent cluster never hands this run a previous
        # run's rows — and no schema is ever dropped to get there.
        schema = f"prod_smoke_{os.environ.get('RETURNS_RUN_ID') or uuid.uuid4().hex[:12]}"
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
        sep = "&" if "?" in pg_url else "?"
        dsn = f"{pg_url}{sep}options=-csearch_path%3D{schema}"
        return "postgres", dsn
    return "sqlite", str(tmp / "smoke.db")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="returns-smoke-"))
    profile, target = _profile(tmp)
    print(f"production smoke — store profile: {profile} ({target})")
    ledger = Ledger()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["RETURNS_EVIDENCE_DIR"] = str(tmp / "evidence")
    # The runtime's Config reads DATABASE_URL (the .env.example
    # contract): hand it the schema-scoped DSN so the subprocess and
    # this process's API service share ONE store. For the SQLite
    # profile the variable must be absent, not inherited.
    if profile == "postgres":
        env["DATABASE_URL"] = target
    else:
        env.pop("DATABASE_URL", None)
        env["RETURNS_SQLITE_PATH"] = target
    proc = subprocess.Popen(
        [sys.executable, "-m", "returns_resolution.runtime_app"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # -- the Runtime server itself --------------------------------
        healthy = False
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                ping = _get("/ping")
                if ping.get("status") in ("Healthy", "HealthyBusy"):
                    healthy = True
                    break
            except Exception:
                time.sleep(0.5)
        if not ledger.check("runtime /ping healthy", healthy):
            return _finish(ledger, proc)

        # The API surface over the SAME store the runtime seeded.
        from fastapi.testclient import TestClient

        from returns_resolution.api import create_app
        from returns_resolution.evidence import LocalEvidenceStore
        from returns_resolution.modelport import OfflineComposer
        from returns_resolution.service import CaseService
        from returns_resolution.store import SqlStore

        if profile == "postgres":
            store = SqlStore(database_url=target)
        else:
            store = SqlStore(sqlite_path=target)
        store.migrate()
        service = CaseService(
            store,
            OfflineComposer(),
            evidence_store=LocalEvidenceStore(root=tmp / "evidence"),
        )
        client = TestClient(create_app(service), raise_server_exceptions=False)

        resp = client.get("/health")
        ledger.check("api /health", resp.status_code == 200, f"status {resp.status_code}")

        # -- ingest (via the Runtime, over HTTP) → decide → compute ---
        feed = _post("/invocations", {"action": "intake_feed"})
        ledger.check(
            "runtime intake_feed",
            len(feed.get("ingested", [])) == 8,
            f"{len(feed.get('ingested', []))} requests ingested",
        )
        resp = client.get(f"/cases/{CASE}/decision")
        decision = resp.json()
        ledger.check(
            "decide: CASE-2003 eligible",
            resp.status_code == 200 and decision["eligible"] is True,
            f"pack {decision.get('policy_pack_version')}",
        )
        resp = client.get(f"/cases/{CASE}/computation")
        computation = resp.json()
        ledger.check(
            "compute: refund total $130.50, final",
            resp.status_code == 200
            and _money(computation.get("refund_total")) == Decimal("130.50")
            and computation.get("is_final") is True,
            f"computation {computation.get('computation_id')}",
        )
        resp = client.get(f"/cases/{CASE}/disposition")
        disposition = resp.json()
        ledger.check(
            "disposition: RESTOCK on declared basis",
            resp.status_code == 200 and disposition.get("disposition") == "RESTOCK",
            f"cell {disposition.get('cell_id')}",
        )
        resp = client.get(f"/cases/{CASE}/fraud-score")
        score = resp.json()
        ledger.check(
            "fraud score: CLEAR at 0.00",
            resp.status_code == 200
            and score.get("band") == "CLEAR"
            and _money(score.get("total_score")) == Decimal("0.00"),
            f"score {score.get('score_id')}",
        )

        # -- approve → execute → idempotent replay ---------------------
        resp = client.post(
            f"/cases/{CASE}/approvals",
            json={"approver": "d.okafor", "role": "returns_lead", "as_of": DAY},
        )
        ledger.check("approve: single named approver", resp.status_code == 201)
        resp = client.post(
            f"/cases/{CASE}/refund-executions",
            json={"idempotency_key": "smoke-2003", "as_of": DAY},
        )
        receipt = resp.json()
        ledger.check(
            "execute: receipt issued",
            resp.status_code == 201 and _money(receipt.get("amount")) == Decimal("130.50"),
            f"receipt {receipt.get('receipt_id')}",
        )
        resp = client.post(
            f"/cases/{CASE}/refund-executions",
            json={"idempotency_key": "smoke-2003", "as_of": DAY},
        )
        replay = resp.json()
        ledger.check(
            "execute replay: same key, same receipt, no second movement",
            resp.status_code == 201 and replay.get("receipt_id") == receipt.get("receipt_id"),
        )
        via_runtime = _post("/invocations", {"action": "get_receipt", "case_id": CASE})
        ledger.check(
            "runtime receipt read: same receipt id across surfaces",
            via_runtime.get("receipt", {}).get("receipt_id") == receipt.get("receipt_id"),
        )

        # -- label → carrier receipt ----------------------------------
        resp = client.post(f"/cases/{CASE}/labels", json={"issued_on": DAY})
        label = resp.json()
        ledger.check(
            "label: issued via the gateway tool port",
            resp.status_code == 201 and label.get("carrier_ref"),
            f"label {label.get('label_id')}",
        )
        resp = client.get(
            f"/cases/{CASE}/labels/{label['label_id']}/events",
            params={"as_of": "2026-10-30"},
        )
        kinds = [e["kind"] for e in resp.json()]
        ledger.check(
            "carrier events: parcel received by 2026-10-30",
            resp.status_code == 200 and kinds and kinds[-1] == "received",
            f"events {kinds}",
        )

        # -- warehouse receipt → grading → adjustment ------------------
        resp = client.post(
            f"/cases/{CASE}/warehouse-receipts",
            json={"received_by": "w.chen", "received_on": "2026-10-30"},
        )
        ledger.check("warehouse receipt recorded", resp.status_code == 201)
        resp = client.post(
            f"/cases/{CASE}/grading-reports",
            json={
                "grader": "p.novak",
                "graded_condition": "used",
                "graded_on": "2026-10-31",
                "damage_notes": "heel counter scuffed",
            },
        )
        report = resp.json()
        ledger.check(
            "grading: declared like-new graded used, delta -$14.50",
            resp.status_code == 201 and _money(report.get("refund_delta")) == Decimal("-14.50"),
            f"report {report.get('report_id')}",
        )
        resp = client.get(f"/cases/{CASE}/adjustments")
        adjustments = resp.json()
        recovery = [a for a in adjustments if a["kind"] == "recovery_due"]
        ledger.check(
            "adjustment: recovery position stands open (no clawback)",
            len(recovery) == 1
            and _money(recovery[0]["amount"]) == Decimal("14.50")
            and recovery[0]["status"] == "open",
        )

        # -- message lifecycle -----------------------------------------
        resp = client.post(
            f"/cases/{CASE}/messages",
            json={"message_type": "grading_outcome_notice", "as_of": "2026-10-31"},
        )
        message = resp.json()
        ledger.check("message: drafted", resp.status_code == 201, message.get("message_id", ""))
        mid = message["message_id"]
        states = []
        for act in ("verify", "queue", "send"):
            resp = client.post(
                f"/cases/{CASE}/messages/{mid}/{act}", json={"as_of": "2026-10-31"}
            )
            states.append((act, resp.status_code, resp.json().get("status")))
        ledger.check(
            "message lifecycle: verified → queued → sent",
            states
            == [("verify", 200, "verified"), ("queue", 200, "queued"), ("send", 200, "sent")],
            f"{states}",
        )

        # -- memory parity ----------------------------------------------
        resp = client.post("/memory/sync")
        ledger.check("memory sync", resp.status_code == 200, f"{resp.json()}")
        resp = client.get(f"/cases/{CASE}/fraud-score/from-memory")
        from_memory = resp.json()
        ledger.check(
            "memory parity (api): from-memory score id == stored score id",
            resp.status_code == 200 and from_memory.get("score_id") == score.get("score_id"),
        )
        via_runtime = _post(
            "/invocations", {"action": "score_case_from_memory", "case_id": CASE}
        )
        ledger.check(
            "memory parity (runtime): same score id over the Runtime surface",
            via_runtime.get("fraud_score", {}).get("score_id") == score.get("score_id"),
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return _finish(ledger, None)


def _finish(ledger: Ledger, proc) -> int:
    if proc is not None:
        proc.terminate()
    if ledger.failures:
        print(f"SMOKE: FAIL ({len(ledger.failures)} step(s): {', '.join(ledger.failures)})")
        return 1
    print("SMOKE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
