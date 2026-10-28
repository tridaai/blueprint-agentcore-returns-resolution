"""Demo: walk an eligible and an ineligible return end to end.

Runs in-process against a demo SQLite file: seeds the Cascade
Outfitters orders, ingests the planted feed, and prints the two cases
that frame the blueprint — REQ-2003, approved on the last day of its
window, and REQ-2004, refused one day past its own.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from returns_resolution.intake import ReferenceFeed
from returns_resolution.modelport import OfflineComposer
from returns_resolution.service import CaseService
from returns_resolution.store import SqlStore


def main() -> int:
    store = SqlStore(sqlite_path="./data/demo-returns.db")
    store.migrate()
    service = CaseService(store, OfflineComposer())
    seeded = service.seed_orders()
    print(f"seeded {seeded} orders")

    cases = {}
    for request in ReferenceFeed().fetch_requests():
        case = service.ingest(request)
        cases[case.case_id] = case
        d = case.decision
        clauses = ", ".join(f"{c.clause_id}({c.effect})" for c in d.clauses)
        print(
            f"{case.case_id}  eligible={d.eligible}  refund_due=${d.refund_due}  "
            f"pack={d.policy_pack_version}  clauses=[{clauses}]"
        )

    for case_id in ("CASE-2003", "CASE-2006"):
        computation = service.get_computation(case_id)
        print(f"\n--- {case_id} refund computation ({computation.computation_id}, final={computation.is_final}) ---")
        for line in computation.lines:
            print(f"  {line.line_id:<16} {line.kind:<9} ${line.amount}  [{line.policy_ref}]")
        print(f"  refund_total     ${computation.refund_total}")
        for component in computation.not_computable:
            print(f"  not computable: {component.component} — missing {component.missing_input}")

    for case_id in ("CASE-2003", "CASE-2004"):
        case = cases[case_id]
        print(f"\n--- {case_id} summary (figure-verified) ---")
        print(case.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
