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

from returns_resolution.intake import FRAUD_FEED_REQUESTS, ReferenceFeed
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

    print("\n--- fraud scoring (pack 2026.10.1, history seed loaded) ---")
    service.seed_history()
    for request in FRAUD_FEED_REQUESTS:
        case = service.ingest(request)
        score = case.fraud_score
        parts = ", ".join(
            f"{s.signal_id}={s.contribution}" if s.status == "computed" else f"{s.signal_id}:{s.status}"
            for s in score.signals
        )
        print(f"  {case.case_id}  total={score.total_score}  band={score.band}  state={case.fraud_state}")
        print(f"      [{parts}]")
        if service.refund_path_blocked(case.case_id):
            print("      refund path: BLOCKED — only a named fraud-review decision can lift this")

    decision = service.record_fraud_review(
        "CASE-3008",
        reviewer="r.alvarez",
        outcome="cleared_with_reason",
        reason_code="legitimate_pattern_explained",
    )
    cleared = service.get_case("CASE-3008")
    print(
        f"  review {decision.review_id}: {decision.reviewer} recorded {decision.outcome} "
        f"({decision.reason_code}) on score {decision.score_id}"
    )
    print(
        f"  CASE-3008 state now {cleared.fraud_state}; the score artefact still reads "
        f"{cleared.fraud_score.band} at {cleared.fraud_score.total_score} — the act lifted the block, not the arithmetic"
    )

    print("\n--- approval gates + refund execution (payments: LOCAL DOUBLE — no real money) ---")
    from datetime import date as _date

    from returns_resolution.service import ApprovalError, DuplicateRefundError

    today = _date(2026, 10, 27)
    double = service.payments_port
    for case_id in ("CASE-2006", "CASE-2003", "CASE-2001"):
        status = service.approval_status(case_id)
        print(
            f"  {case_id}  refund ${status['refund_total']}  tier={status['tier']}  "
            f"approvals_required={status['approvals_required']}"
        )
    # Auto tier: CASE-2006 ($29.05 <= $50 cap) executes with no acts.
    receipt = service.execute_refund("CASE-2006", idempotency_key="demo-refund-2006", as_of=today)
    print(f"  CASE-2006 executed: receipt {receipt.receipt_id} via {receipt.provider} ref {receipt.provider_ref}")
    again = service.execute_refund("CASE-2006", idempotency_key="demo-refund-2006", as_of=today)
    print(
        f"  retry with the same key returned receipt {again.receipt_id} (identical: {again.receipt_id == receipt.receipt_id}); "
        f"money movements so far: {double.movement_count}"
    )
    # Single tier: CASE-2003 ($130.50) needs one named approver.
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=today)
    receipt = service.execute_refund("CASE-2003", idempotency_key="demo-refund-2003", as_of=today)
    print(f"  CASE-2003 approved by d.okafor (returns_lead) and executed: receipt {receipt.receipt_id}")
    # Dual tier: CASE-2001 ($189.00) needs a second, different approver.
    service.record_approval("CASE-2001", approver="d.okafor", role="returns_lead", as_of=today)
    try:
        service.execute_refund("CASE-2001", idempotency_key="demo-refund-2001", as_of=today)
    except ApprovalError as exc:
        print(f"  CASE-2001 with one approval: execution refused — {exc}")
    service.record_approval("CASE-2001", approver="m.osei", role="finance_manager", as_of=today)
    receipt = service.execute_refund("CASE-2001", idempotency_key="demo-refund-2001", as_of=today)
    print(f"  CASE-2001 second approval by m.osei (finance_manager); executed: receipt {receipt.receipt_id}")
    try:
        service.execute_refund("CASE-2001", idempotency_key="demo-refund-2001-RETRY", as_of=today)
    except DuplicateRefundError as exc:
        print(f"  CASE-2001 retried under a new key: refused — {exc}")
    print(f"  total money movements on the double: {double.movement_count} (one per executed case, retries included)")

    for case_id in ("CASE-2003", "CASE-2004"):
        case = cases[case_id]
        print(f"\n--- {case_id} summary (figure-verified) ---")
        print(case.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
