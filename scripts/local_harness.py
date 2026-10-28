"""Local harness: exercise the AgentCore Runtime HTTP contract.

Starts the runtime entrypoint as a subprocess (the same module the
container runs), waits for GET /ping to report Healthy, then drives
POST /invocations: seed, ingest the planted feed, and read back the
edge case. This is the closest local analogue of invoking the agent
on AgentCore Runtime — same contract, same payloads, no AWS account.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8080"


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


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="returns-harness-")
    env = dict(os.environ)
    env["RETURNS_SQLITE_PATH"] = str(Path(tmp) / "returns.db")
    env["RETURNS_EVIDENCE_DIR"] = str(Path(tmp) / "evidence")
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.Popen(
        [sys.executable, "-m", "returns_resolution.runtime_app"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 60
        while True:
            try:
                ping = _get("/ping")
                if ping.get("status") in ("Healthy", "HealthyBusy"):
                    print(f"ping: {ping}")
                    break
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError("runtime server did not become healthy")
                time.sleep(0.5)

        feed = _post("/invocations", {"action": "intake_feed"})
        print(f"ingested {len(feed['ingested'])} requests")
        case = _post("/invocations", {"action": "get_case", "case_id": "CASE-2003"})
        decision = case["case"]["decision"]
        assert decision["eligible"] is True, decision
        print(f"CASE-2003 eligible={decision['eligible']} refund_due={decision['refund_due']}")
        case4 = _post("/invocations", {"action": "get_case", "case_id": "CASE-2004"})
        assert case4["case"]["decision"]["eligible"] is False
        print(f"CASE-2004 eligible=False (window edge, as planted)")
        print("harness: OK")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
