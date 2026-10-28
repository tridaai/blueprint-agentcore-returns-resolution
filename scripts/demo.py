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
    # The demo is a walkthrough, not a store of record: start from a
    # fresh demo database so the walkthrough always shows the current
    # round's behaviour end to end.
    demo_db = Path("./data/demo-returns.db")
    if demo_db.exists():
        demo_db.unlink()
    store = SqlStore(sqlite_path=demo_db)
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

    print("\n--- dispositions (matrix 2026.10.1, basis: declared) ---")
    for case_id in sorted(cases):
        disp = service.get_disposition(case_id)
        if disp is None:
            print(f"  {case_id}  — (ineligible: no disposition)")
        elif disp.status == "not_decidable":
            print(f"  {case_id}  NOT DECIDABLE — missing dimension: {disp.missing_dimension}")
        else:
            route = disp.cell_id
            if disp.reroutes:
                hops = " -> ".join(
                    f"{s.from_cell_id} [{s.closed_channel} closed]" for s in disp.reroutes
                )
                route = f"{hops} -> {disp.cell_id}"
            print(f"  {case_id}  {disp.disposition:<18} via {disp.channel:<10} cell {route}")
            for unknown in disp.inspection_unknowns:
                print(f"      inspection must establish: {unknown}")

    for case_id in ("CASE-2003", "CASE-2004"):
        case = cases[case_id]
        print(f"\n--- {case_id} summary (figure-verified) ---")
        print(case.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
