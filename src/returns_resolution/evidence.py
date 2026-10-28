"""Evidence store port — S3-shaped, local double in R1.

Photos and receipts are evidence: they are stored under content-hash
names, the SHA-256 is recorded with the case at put time, and reads
re-verify the hash. The port's URI shape is the production shape
(`s3://<bucket>/<case>/<kind>/<sha>-<filename>`); the local double
writes under RETURNS_EVIDENCE_DIR and, in deployment, an S3 adapter
slots behind the same port — the same pattern the portfolio uses for
SeaweedFS-backed evidence in docker-compose.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol


class EvidenceStore(Protocol):
    def put(self, case_id: str, kind: str, filename: str, content: bytes) -> dict: ...
    def get(self, uri: str) -> bytes: ...


class LocalEvidenceStore:
    def __init__(self, root: str | Path = "./data/evidence", bucket: str = "returns-evidence"):
        self._root = Path(root)
        self._bucket = bucket

    def _path_for(self, case_id: str, kind: str, sha: str, filename: str) -> Path:
        return self._root / case_id / kind / f"{sha[:16]}-{filename}"

    def put(self, case_id: str, kind: str, filename: str, content: bytes) -> dict:
        sha = hashlib.sha256(content).hexdigest()
        path = self._path_for(case_id, kind, sha, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        uri = f"s3://{self._bucket}/{case_id}/{kind}/{sha[:16]}-{filename}"
        return {
            "evidence_id": f"EV-{sha[:12]}",
            "case_id": case_id,
            "kind": kind,
            "filename": filename,
            "uri": uri,
            "sha256": sha,
            "size_bytes": len(content),
        }

    def get(self, uri: str) -> bytes:
        prefix = f"s3://{self._bucket}/"
        if not uri.startswith(prefix):
            raise ValueError(f"uri is not in bucket {self._bucket}: {uri}")
        rel = uri[len(prefix):]
        parts = rel.split("/")
        case_id, kind, stored = parts[0], parts[1], parts[2]
        sha_prefix, filename = stored.split("-", 1)
        path = self._path_for(case_id, kind, sha_prefix, filename)
        content = path.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if not actual.startswith(sha_prefix):
            raise ValueError(f"evidence hash mismatch for {uri}")
        return content
