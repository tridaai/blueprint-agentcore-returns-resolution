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

    print("\n--- carrier tools via the gateway (carrier: LOCAL DOUBLE — nothing really ships) ---")
    from returns_resolution.models import ReturnRequest as _ReturnRequest
    from returns_resolution.service import CarrierError as _CarrierError
    from returns_resolution.service import GradingError as _GradingError

    carrier = service.carrier_port

    def _issue(case_id, on):
        return service.invoke_tool(
            "create_return_label", {"case_id": case_id, "issued_on": on},
            case_id=case_id, called_on=on,
        )

    label2003 = _issue("CASE-2003", today)
    rec = label2003.reconciliation
    print(
        f"  CASE-2003 label {label2003.carrier_ref}: {label2003.service_level}, {label2003.weight_grams} g, "
        f"cost ${label2003.cost} — reconciliation {rec.status} (deducted ${rec.deducted_amount}, variance ${rec.variance})"
    )
    again = _issue("CASE-2003", today)
    print(
        f"  re-issue returned the same label ({again.label_id == label2003.label_id}); "
        f"labels issued at the carrier port: {carrier.label_count}"
    )
    events = service.invoke_tool(
        "get_carrier_events",
        {"case_id": "CASE-2003", "label_id": label2003.label_id, "as_of": _date(2026, 10, 30)},
        case_id="CASE-2003", called_on=_date(2026, 10, 30),
    )
    print(f"  CASE-2003 tracking by 2026-10-30: {', '.join(e.kind for e in events)}")

    label2006 = _issue("CASE-2006", today)
    rec6 = label2006.reconciliation
    print(
        f"  CASE-2006 label {label2006.carrier_ref}: {label2006.service_level}, cost ${label2006.cost} — "
        f"reconciliation {rec6.status} (deducted ${rec6.deducted_amount}, variance ${rec6.variance} on the record)"
    )

    label2001 = _issue("CASE-2001", today)
    void = service.invoke_tool(
        "void_return_label",
        {
            "case_id": "CASE-2001", "label_id": label2001.label_id,
            "voided_by": "d.okafor", "reason": "customer printed the wrong size label",
            "voided_on": today,
        },
        case_id="CASE-2001", called_on=today,
    )
    fresh = _issue("CASE-2001", _date(2026, 10, 28))
    print(
        f"  CASE-2001 label voided by {void.voided_by} ({void.reason}); fresh label {fresh.carrier_ref} "
        f"supersedes {fresh.supersedes == label2001.label_id}"
    )

    # A bulky return: the tent order, requested inside its window.
    tent = service.ingest(
        _ReturnRequest(
            request_id="REQ-2010", order_id="ORD-1002", sku="TENT-2P", qty=1,
            reason_code="changed_mind", condition_declared="used",
            requested_on=_date(2026, 9, 15),
        )
    )
    tent_label = _issue(tent.case_id, _date(2026, 9, 16))
    pickup = service.invoke_tool(
        "book_pickup", {"case_id": tent.case_id, "scheduled_on": _date(2026, 9, 17)},
        case_id=tent.case_id, called_on=_date(2026, 9, 17),
    )
    print(
        f"  {tent.case_id} (tent, {tent_label.weight_grams} g) label {tent_label.carrier_ref}: "
        f"{tent_label.service_level} at ${tent_label.cost}; pickup {pickup.carrier_ref} booked for "
        f"{pickup.scheduled_on} ({pickup.window})"
    )
    try:
        service.invoke_tool(
            "book_pickup", {"case_id": "CASE-2003", "scheduled_on": today},
            case_id="CASE-2003", called_on=today,
        )
    except _CarrierError as exc:
        print(f"  CASE-2003 pickup refused — {exc}")

    print("\n--- warehouse grading: the parcel comes back, the cascade runs (R7) ---")
    returned_on = _date(2026, 10, 30)  # the carrier's received day for the label above
    service.record_warehouse_receipt(
        "CASE-2003", received_by="w.chen", received_on=returned_on
    )
    report = service.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="used",
        graded_on=_date(2026, 10, 31), damage_notes="heel counter scuffed",
    )
    (recovery,) = service.list_adjustments("CASE-2003")
    print(
        "  CASE-2003 boots declared like_new ($130.50 already paid above) graded used: "
        "disposition RESTOCK → OUTLET (basis: graded), refund re-priced to $116.00, "
        f"delta {report.refund_delta}"
    )
    print(
        f"  recovery position {recovery.adjustment_id}: {recovery.amount} "
        f"({recovery.status}) — a recorded receivable; no clawback is executed"
    )
    print(f"  re-issued summary: {service.get_case('CASE-2003').summary}")

    _issue("CASE-2006", today)  # the held lamp's label (idempotent if present)
    service.record_warehouse_receipt(
        "CASE-2006", received_by="w.chen", received_on=returned_on
    )
    try:
        service.record_grading(
            "CASE-2006", grader="p.novak", graded_condition="used",
            graded_on=_date(2026, 10, 31),
        )
    except _GradingError as exc:
        print(f"  CASE-2006 grading refused — {exc}")
    lamp_report = service.record_grading(
        "CASE-2006", grader="p.novak", graded_condition="used",
        graded_on=_date(2026, 10, 31),
        functional_test_result="pass", data_wipe_status="verified",
    )
    lamp_decision = service.get_disposition("CASE-2006")
    print(
        f"  CASE-2006 held electronics graded functional + wiped → "
        f"{lamp_decision.disposition} via {lamp_decision.channel} "
        f"(grading-pack rule {lamp_decision.cell_id}); "
        f"the money does not move (delta {lamp_report.refund_delta})"
    )

    print("\n--- case timeline (derived from the artefacts of record) ---")
    for case_id, as_of in (("CASE-2003", _date(2026, 10, 30)), ("CASE-3008", _date(2026, 11, 10))):
        view = service.get_timeline(case_id, as_of)
        print(f"  {case_id} as of {as_of}: {len(view.entries)} entries, {len(view.gaps)} gaps")
        for gap in view.gaps:
            print(f"    GAP [{gap.kind}] {gap.detail}")

    print("\n--- customer messaging: templates, the verifier, the lifecycle (R8; messaging: LOCAL DOUBLE) ---")
    from returns_resolution.service import MessagingError as _MessagingError

    messaging = service.messaging_port
    standing = [
        m for m in service.list_all_messages() if m.message_type == "return_summary"
    ]
    print(
        f"  every ingested case carries its summary as a message: "
        f"{len(standing)} recorded, "
        f"{sum(1 for m in standing if m.status == 'verified')} verified, "
        f"{sum(1 for m in standing if m.status == 'refused')} refused"
    )

    def _send(case_id, message_type, on):
        draft = service.draft_message(case_id, message_type, as_of=on)
        service.verify_message(draft.message_id, as_of=on)
        service.queue_message(draft.message_id, as_of=on)
        return service.send_message(draft.message_id, as_of=on)

    sent = _send("CASE-2003", "grading_outcome_notice", _date(2026, 10, 31))
    print(f"  CASE-2003 grading-outcome notice sent via {sent.provider}:")
    print(f"    {sent.text}")
    recovery_note = _send("CASE-2003", "adjustment_notice", _date(2026, 10, 31))
    print(f"  CASE-2003 recovery notice sent ({recovery_note.message_id}); recovery due $14.50 named")
    fraud_note = _send("CASE-3008", "fraud_review_notice", _date(2026, 11, 1))
    print(f"  CASE-3008 fraud-review notice sent — no signal, weight or threshold in it:")
    print(f"    {fraud_note.text}")

    probe = service.draft_message(
        "CASE-2003", "return_summary", as_of=_date(2026, 10, 31),
        model_note="Good news — your refund is $999.99.",
    )
    refused = service.verify_message(probe.message_id, as_of=_date(2026, 10, 31))
    print(f"  an inflated draft (free text in the note slot) is {refused.status}: {refused.violations}")

    # A queued message goes stale when grading supersedes its artefacts.
    queued = [m for m in service.list_messages("CASE-2001") if m.status == "verified"][-1]
    service.queue_message(queued.message_id, as_of=_date(2026, 10, 31))
    service.record_warehouse_receipt("CASE-2001", received_by="w.chen", received_on=_date(2026, 10, 31))
    graded2001 = service.record_grading(
        "CASE-2001", grader="p.novak", graded_condition="like_new", graded_on=_date(2026, 11, 1),
    )
    try:
        service.send_message(queued.message_id, as_of=_date(2026, 11, 1))
    except _MessagingError as exc:
        withdrawn = service.get_message(queued.message_id)
        print(
            f"  CASE-2001's queued summary was withdrawn when grading superseded its artefacts "
            f"(refund delta {graded2001.refund_delta}); the send was refused — {exc}"
        )
        print("  nothing stale was sent; a fresh verified summary stands in its place")
    print(f"  deliveries on the messaging double: {messaging.delivery_count} (one per send, retries included)")

    print("\n--- customer memory (AgentCore Memory port: LOCAL DOUBLE — no real memory service) ---")
    service.sync_memory()
    records = service.retrieve_memory("CUST-CID", as_of=_date(2026, 11, 2))
    for record in records:
        print(
            f"  {record.occurred_on}  {record.event_type:<22} case {record.case_id}  "
            f"source {record.source_ref}  total={record.refund_total}  "
            f"amount={record.refund_amount}  disposition={record.disposition}  band={record.fraud_band}"
        )
    from_mem = service.score_case_from_memory("CASE-2003")
    stored = service.get_fraud_score("CASE-2003")
    print(
        f"  CASE-2003 re-scored from memory alone: {from_mem.total_score} {from_mem.band} "
        f"(source: {from_mem.history_source}) — the same artefact the store path produced: "
        f"{from_mem.score_id == stored.score_id}"
    )
    retention = service.memory_retention_status("CUST-CID", as_of=_date(2026, 11, 2))
    print(
        f"  retention (pack {retention['memory_pack_version']}, window {retention['retention_days']} days): "
        f"{retention['active_count']} active, {retention['expired_count']} expired — expired records are "
        "counted here, never deleted"
    )
    print(
        f"  every retrieval above is on the access log: "
        f"{len(service.list_memory_access_log('CUST-CID'))} entries for CUST-CID, "
        f"{len(service.list_memory_access_log())} in total"
    )

    for case_id in ("CASE-2003", "CASE-2004"):
        case = service.get_case(case_id)
        print(f"\n--- {case_id} summary (figure-verified) ---")
        print(case.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
