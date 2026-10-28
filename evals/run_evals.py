"""Eval gate for Blueprint #18 (R1–R10).

Profiles (R10): the full scenario body runs once per store profile
— in-memory and SQLite always, PostgreSQL when DATABASE_URL (or
RETURNS_DATABASE_URL) is set. Every profile must produce the same
zero counters, and the parity counters at the end compare the
content-addressed artefact ids (receipts, fraud scores,
computations, dispositions, labels, messages, memory records)
across profiles: identical planted work must mint identical
artefact ids on every store, or the gate fails.

Zero-tolerance counters against planted truth:
- eligibility_outcome_mismatches: decisions that differ from the
  planted eligible/denied outcome for each feed request.
- clause_mismatches: decisions whose deny-clause set differs from the
  planted set, or that fail to record pack version + clauses at all.
- window_edge_misgraded: the one-day edge pair (REQ-2003 in,
  REQ-2004 out) graded wrong on either side.
- figure_verifier_violations_accepted: tampered summaries (wrong
  amount, wrong date) that the verifier or the service let through.
- refund_computation_mismatches: computation lines/totals that differ
  from the hand-computed fixtures, to the cent.
- refund_tamper_undetected: stored computations altered after the
  fact that the re-performance verifier did not catch.
- silent_zero_fills: not-computable components carrying a filled
  amount, or the missing-rate probe resolving to a zero fee.
- partial_state_mislabeled: computations whose is_final/refund_total
  do not honestly state partiality.
- disposition_mismatches: feed-case dispositions that differ from
  the hand-derived cell/disposition, or ineligible cases carrying a
  disposition at all.
- wrong_dispositions: matrix sweep outcomes (all 36 primary rows,
  plus the fallback row) that differ from the hand-derived map.
- matrix_rows_unexercised: primary matrix rows the sweep never
  evaluated.
- undecidable_but_decided: fact sets the matrix cannot cover that
  came back with a disposition instead of a named missing dimension.
- silent_reroutes: decisions that landed on a non-primary cell with
  no reroute record, or through a channel the table closes.
- hold_unknowns_unnamed: HOLD_FOR_INSPECTION decisions that do not
  name exactly what inspection must establish.
- disposition_tamper_undetected: stored dispositions altered after
  the fact that the re-performance verifier did not catch.
- fraud_band_mismatches: scores that differ from the hand-derived
  planted truth (serial returner 95.00 FRAUD_REVIEW, wardrobing
  36.00 MONITOR, every feed case 0.00 CLEAR), per-signal
  contributions included, or insufficient/not-computable signals
  that fail to name their situation.
- fraud_block_bypasses: attempts to move a FRAUD_REVIEW case
  forward without the named review act (tampered score, off-menu
  outcome, uncoded reason, unnamed reviewer, second decision, a
  decision on a case not in review) that were not refused.
- fraud_score_irreproducibility: re-scores over unchanged inputs
  that produce a different artefact, stored scores the
  re-performance verifier cannot reproduce, or identical seeds
  scored differently by two services.
- fraud_lifted_without_decision: a blocked case found unblocked
  (or carrying a review artefact) after the bypass probes, before
  any legitimate decision act.
- double_refunds: a computation whose money moved more than once —
  same-key retries must replay the original receipt (provider
  movement count stays 1), and a different key on an executed
  computation must be refused as duplicate_refund.
- unapproved_executions: executions that landed without the
  approval tier satisfied — the planted ladder walks auto (no
  acts), single (one approver) and dual (two DIFFERENT approvers;
  a same-person second act is refused at record time).
- stale_computation_approvals_accepted: approval acts pinning a
  computation other than the case's current one, or stored
  approvals pinned to a superseded/forged computation id that the
  gates counted toward satisfaction.
- fraud_blocked_executions: approval or execution acts on the
  FRAUD_REVIEW case (CASE-3008) that were not refused by the R4
  guard before its named review decision.
- failed_execution_partial_receipts: receipts (or money movements)
  left behind by an injected provider failure; the failed case
  must stay executable and land exactly one receipt on retry.
- duplicate_labels: a case issued more than one active label —
  re-issuing through the gateway must return the existing artefact
  and the carrier port must have issued exactly one label.
- label_for_ineligible_or_blocked_accepted: label issuances that
  landed for an ineligible case (CASE-2004) or the fraud-blocked
  case (CASE-3008, before its named review decision).
- label_cost_mismatches_hidden: labels whose recorded
  reconciliation does not state the exact variance between the
  label's cost and the computation's return-shipping deduction,
  or that call a nonzero variance "matched".
- timeline_gaps_unnamed: expected-but-absent artefacts the derived
  timeline failed to name (a refund executed with no label, a
  ship-by date passed with no label, a fraud block awaiting its
  review decision), gaps claimed on a fully walked case, or
  timeline entries carrying no artefact id.
- non_bulky_pickups_accepted: pickups booked for categories the
  pack does not flag as bulky; the bulky booking beside it must
  land against the case's active label.
- grading_delta_mismatches: grading cascades whose re-priced
  totals, recorded deltas or adjustment positions differ from the
  hand-computed truth (boots like_new→used −$14.50; the under-
  refunded top-up +$14.50; the held lamp resolving to REFURBISH).
- in_place_artefact_edits: superseded computations, dispositions
  or fraud scores whose stored payload changed during a grading
  cascade — supersession must be by new artefact, never by edit.
- ungated_top_ups: top-up executions that landed without the
  approval gate re-checked against the NEW computation, full
  re-executions of an already-settled case that landed, or a
  second top-up under a fresh key.
- holds_finalised_without_fields: grading reports on the held
  electronics case that finalised without BOTH inspection facts
  (functional test result, data-wipe status) on the record.
- grading_gate_bypasses: warehouse/grading acts that landed
  without their gates — receipt before the carrier's received
  event, the receiver grading their own parcel, grading before
  any warehouse receipt, or a second grading act.
- unverified_message_sends: customer messages delivered by the
  messaging port that were not verified, queued, stored texts —
  or deliveries beyond the sends performed.
- stale_message_sends: messages delivered after their underlying
  artefacts were superseded — a stale send must instead refuse
  with the message withdrawn and a verified replacement standing.
- forbidden_content_sent: delivered message texts carrying
  disclosure-pack content — fraud signal names, scores,
  thresholds, or promises the policy pack does not make.
- figure_fact_violations_sent: message drafts whose figures or
  stated facts disagreed with the artefacts of record and were
  not refused — an inflated refund, a wrong disposition.
- cross_customer_leaks: memory retrievals returning one
  customer's records to another (or to a stranger probe), access-
  log entries whose returned ids belong to a different customer,
  or a message quoting ANOTHER customer's memory figure that the
  verifier let through.
- memory_store_score_divergences: fraud scores recomputed from
  memory records that differ from the store-path artefact for
  the same case (id, total, band), milestone records missing or
  doubled, or the customer's own live memory figure refused in
  their own message — the memory view must agree with the store.
- expired_memory_surfaces: records past the pack's retention
  window returned by retrieval, or an expired record's figure
  verified in a message.
- unlogged_memory_retrievals: memory port reads that never
  reached the access log, or log entries missing their purpose,
  actor or customer scope.

Prints the counters and GATE: PASS/FAIL; exit code follows.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from returns_resolution.carrier import LocalCarrierDouble
from returns_resolution.intake import FRAUD_FEED_REQUESTS, PLANTED_REQUESTS
from returns_resolution.memory import MemoryPack
from returns_resolution.messaging import LocalMessagingDouble
from returns_resolution.modelport import OfflineComposer
from returns_resolution.models import Order, OrderLine, RefundApproval, ReturnRequest
from returns_resolution.payments import LocalPaymentsDouble
from returns_resolution.seed import CATALOGUE
from returns_resolution.service import (
    ApprovalError,
    CarrierError,
    CaseService,
    DuplicateRefundError,
    FigureVerificationError,
    FraudHoldError,
    FraudReviewError,
    GradingError,
    MessagingError,
    PaymentsError,
)
from returns_resolution.store import InMemoryStore, SqlStore
from returns_resolution.verifier import verify_summary

REFUND_FIXTURES = {
    "CASE-2001": {"item_amount": "189.00", "restocking_fee": "0.00", "return_shipping": "0.00", "refund_total": "189.00"},
    "CASE-2003": {"item_amount": "145.00", "restocking_fee": "14.50", "return_shipping": "0.00", "refund_total": "130.50"},
    "CASE-2006": {"item_amount": "45.00", "restocking_fee": "9.00", "return_shipping": "6.95", "refund_total": "29.05"},
    "CASE-2004": {"refund_total": "0.00"},  # ineligible: final zero
}

TRUTH = {
    "REQ-2001": (True, []),
    "REQ-2002": (False, ["WINDOW_EXPIRED"]),
    "REQ-2003": (True, []),
    "REQ-2004": (False, ["WINDOW_EXPIRED"]),
    "REQ-2005": (False, ["FINAL_SALE"]),
    "REQ-2006": (True, []),
    "REQ-2007": (False, ["CONDITION_BARRED"]),
    "REQ-2008": (False, ["CATEGORY_EXCLUDED"]),
}


class _TamperedComposer:
    profile_name = "tampered"

    def compose_summary(self, decision, product_name):
        return "Your refund due is $999.99. Please ship by 2027-01-01."


def run_scenarios(make_store) -> tuple[dict, dict]:
    counters = {
        "eligibility_outcome_mismatches": 0,
        "clause_mismatches": 0,
        "window_edge_misgraded": 0,
        "figure_verifier_violations_accepted": 0,
        "refund_computation_mismatches": 0,
        "refund_tamper_undetected": 0,
        "silent_zero_fills": 0,
        "partial_state_mislabeled": 0,
        "disposition_mismatches": 0,
        "wrong_dispositions": 0,
        "matrix_rows_unexercised": 0,
        "undecidable_but_decided": 0,
        "silent_reroutes": 0,
        "hold_unknowns_unnamed": 0,
        "disposition_tamper_undetected": 0,
        "fraud_band_mismatches": 0,
        "fraud_block_bypasses": 0,
        "fraud_score_irreproducibility": 0,
        "fraud_lifted_without_decision": 0,
        "double_refunds": 0,
        "unapproved_executions": 0,
        "stale_computation_approvals_accepted": 0,
        "fraud_blocked_executions": 0,
        "failed_execution_partial_receipts": 0,
        "duplicate_labels": 0,
        "label_for_ineligible_or_blocked_accepted": 0,
        "label_cost_mismatches_hidden": 0,
        "timeline_gaps_unnamed": 0,
        "non_bulky_pickups_accepted": 0,
        "grading_delta_mismatches": 0,
        "in_place_artefact_edits": 0,
        "ungated_top_ups": 0,
        "holds_finalised_without_fields": 0,
        "grading_gate_bypasses": 0,
        "unverified_message_sends": 0,
        "stale_message_sends": 0,
        "forbidden_content_sent": 0,
        "figure_fact_violations_sent": 0,
        "cross_customer_leaks": 0,
        "memory_store_score_divergences": 0,
        "expired_memory_surfaces": 0,
        "unlogged_memory_retrievals": 0,
    }

    service = CaseService(make_store(), OfflineComposer())
    service.seed_orders()
    decisions = {}
    for request in PLANTED_REQUESTS:
        case = service.ingest(request)
        decisions[request.request_id] = case.decision
        expected_eligible, expected_denies = TRUTH[request.request_id]
        if case.decision.eligible is not expected_eligible:
            counters["eligibility_outcome_mismatches"] += 1
        denies = [c.clause_id for c in case.decision.clauses if c.effect == "deny"]
        if denies != expected_denies:
            counters["clause_mismatches"] += 1
        if not case.decision.policy_pack_version or not case.decision.clauses:
            counters["clause_mismatches"] += 1
        # The composed summary must itself verify clean (R2: against
        # the decision + computation universe).
        if verify_summary(case.summary, case.decision, case.computation):
            counters["figure_verifier_violations_accepted"] += 1

    if decisions["REQ-2003"].eligible is not True or decisions["REQ-2004"].eligible is not False:
        counters["window_edge_misgraded"] += 1

    # Wrong-figure probes: direct verifier probes + a tampered service.
    good = service.get_case("CASE-2001")
    for tampered in (
        good.summary.replace("$189.00", "$198.00"),
        good.summary.replace("2026-10-20", "2026-10-21"),
    ):
        if not verify_summary(tampered, good.decision, good.computation):
            counters["figure_verifier_violations_accepted"] += 1

    # --- R2: refund computations against hand-computed fixtures --------
    for case_id, expected in REFUND_FIXTURES.items():
        computation = service.get_computation(case_id)
        if computation is None:
            counters["refund_computation_mismatches"] += 1
            continue
        amounts = {line.line_id: line.amount for line in computation.lines}
        for key, expected_amount in expected.items():
            actual = computation.refund_total if key == "refund_total" else amounts.get(key)
            if actual != Decimal(expected_amount):
                counters["refund_computation_mismatches"] += 1
        if service.verify_stored_computation(case_id):
            counters["refund_computation_mismatches"] += 1

    # Tamper probes: alter a stored line and a stored total; the
    # re-performance verifier must catch both.
    from returns_resolution.policy import load_policy_pack
    from returns_resolution.refunds import verify_computation
    from returns_resolution.seed import CATALOGUE, ORDERS

    stored = service.get_computation("CASE-2003")
    case2003 = service.get_case("CASE-2003")
    pack = load_policy_pack()
    for mutate in (
        lambda c: c.lines[1].__setattr__("amount", Decimal("1.00")),
        lambda c: c.__setattr__("refund_total", Decimal("145.00")),
    ):
        tampered = stored.model_copy(deep=True)
        mutate(tampered)
        problems = verify_computation(
            tampered, pack, ORDERS["ORD-1003"], CATALOGUE["BOOT-TRL"],
            case2003.request, case2003.decision,
        )
        if not problems:
            counters["refund_tamper_undetected"] += 1

    # Zero-fill + partial-state probes: a declared condition with no
    # rate in the pack must come back not_computable with amount None,
    # an honestly open total, and no silent zero anywhere.
    probe = ReturnRequest(
        request_id="REQ-2098", order_id="ORD-1007", sku="STOVE-PKT", qty=1,
        reason_code="changed_mind", condition_declared="waterlogged",
        requested_on=date(2026, 10, 25),
    )
    service.ingest(probe)
    probe_computation = service.get_computation("CASE-2098")
    fee = next(line for line in probe_computation.lines if line.line_id == "restocking_fee")
    if fee.status != "not_computable" or fee.amount is not None:
        counters["silent_zero_fills"] += 1
    if probe_computation.is_final is not False or probe_computation.refund_total is not None:
        counters["partial_state_mislabeled"] += 1
    for computation in service.list_computations():
        for line in computation.lines:
            if line.status == "not_computable" and line.amount is not None:
                counters["silent_zero_fills"] += 1
        if computation.is_final and computation.refund_total is None:
            counters["partial_state_mislabeled"] += 1
        named = {c.component for c in computation.not_computable}
        if computation.eligible and "tender_allocation" not in named and not computation.tender_allocations:
            counters["partial_state_mislabeled"] += 1
    tampered_service = CaseService(make_store(), _TamperedComposer())
    tampered_service.seed_orders()
    try:
        tampered_service.ingest(PLANTED_REQUESTS[0])
        counters["figure_verifier_violations_accepted"] += 1  # accepted!
    except FigureVerificationError:
        stored = tampered_service.get_case("CASE-2001")
        if stored is None or stored.summary is not None or stored.status != "summary_failed":
            counters["figure_verifier_violations_accepted"] += 1

    # --- R3: dispositions on the feed cases -----------------------------
    from returns_resolution.disposition import (
        decide_disposition,
        load_channel_table,
        load_matrix_pack,
        verify_disposition,
    )

    matrix = load_matrix_pack()
    channels = load_channel_table()
    FEED_DISPOSITIONS = {
        "CASE-2001": ("RESTOCK", "M-APP-NWT-FRESH"),
        "CASE-2003": ("RESTOCK", "M-FTW-LN-SEASON"),
        "CASE-2006": ("HOLD_FOR_INSPECTION", "M-ELEC-USED-FRESH"),
    }
    for case_id, (disposition, cell_id) in FEED_DISPOSITIONS.items():
        stored_disp = service.get_disposition(case_id)
        if stored_disp is None or (stored_disp.disposition, stored_disp.cell_id) != (disposition, cell_id):
            counters["disposition_mismatches"] += 1
        elif service.verify_stored_disposition(case_id):
            counters["disposition_mismatches"] += 1
    for request_id, (eligible, _denies) in TRUTH.items():
        if not eligible:
            case_id = f"CASE-{request_id[4:]}"
            if service.get_disposition(case_id) is not None:
                counters["disposition_mismatches"] += 1

    # The held electronics case must name its unknowns exactly.
    held = service.get_disposition("CASE-2006")
    if held is None or held.inspection_unknowns != ["functional_test_result", "data_wipe_status"]:
        counters["hold_unknowns_unnamed"] += 1

    # Tamper probe: an altered stored disposition must be caught.
    stored_disp = service.get_disposition("CASE-2003")
    case2003b = service.get_case("CASE-2003")
    tampered_disp = stored_disp.model_copy(deep=True)
    tampered_disp.disposition = "RECYCLE"
    problems = verify_disposition(
        tampered_disp, matrix, channels,
        case_id="CASE-2003", computation_id=stored_disp.computation_id,
        category="footwear", condition_declared="like_new", age_days=60,
    )
    if not problems:
        counters["disposition_tamper_undetected"] += 1
    _ = case2003b

    # --- R3: the full matrix sweep against the hand-derived map --------
    # (category, condition, age representative) -> (final cell,
    # disposition, channel). Age representatives: fresh=10, season=60,
    # aged=120. Hand-derived from the matrix's stated retail logic;
    # the apparel aged-used row lands on its fallback cell because the
    # shipped channel table closes the apparel liquidator.
    SWEEP = {
        ("apparel", "new_with_tags", 10): ("M-APP-NWT-FRESH", "RESTOCK", "store"),
        ("apparel", "new_with_tags", 60): ("M-APP-NWT-SEASON", "RESTOCK", "store"),
        ("apparel", "new_with_tags", 120): ("M-APP-NWT-AGED", "OUTLET", "outlet"),
        ("apparel", "like_new", 10): ("M-APP-LN-FRESH", "RESTOCK", "store"),
        ("apparel", "like_new", 60): ("M-APP-LN-SEASON", "OUTLET", "outlet"),
        ("apparel", "like_new", 120): ("M-APP-LN-AGED", "OUTLET", "outlet"),
        ("apparel", "used", 10): ("M-APP-USED-FRESH", "REFURBISH", "store"),
        ("apparel", "used", 60): ("M-APP-USED-SEASON", "OUTLET", "outlet"),
        ("apparel", "used", 120): ("M-APP-USED-AGED-FB", "RECYCLE", "recycler"),
        ("footwear", "new_with_tags", 10): ("M-FTW-NWT-FRESH", "RESTOCK", "store"),
        ("footwear", "new_with_tags", 60): ("M-FTW-NWT-SEASON", "RESTOCK", "store"),
        ("footwear", "new_with_tags", 120): ("M-FTW-NWT-AGED", "OUTLET", "outlet"),
        ("footwear", "like_new", 10): ("M-FTW-LN-FRESH", "RESTOCK", "store"),
        ("footwear", "like_new", 60): ("M-FTW-LN-SEASON", "RESTOCK", "store"),
        ("footwear", "like_new", 120): ("M-FTW-LN-AGED", "OUTLET", "outlet"),
        ("footwear", "used", 10): ("M-FTW-USED-FRESH", "REFURBISH", "store"),
        ("footwear", "used", 60): ("M-FTW-USED-SEASON", "OUTLET", "outlet"),
        ("footwear", "used", 120): ("M-FTW-USED-AGED", "LIQUIDATE", "liquidator"),
        ("gear", "new_with_tags", 10): ("M-GEAR-NWT-FRESH", "RESTOCK", "store"),
        ("gear", "new_with_tags", 60): ("M-GEAR-NWT-SEASON", "RESTOCK", "store"),
        ("gear", "new_with_tags", 120): ("M-GEAR-NWT-AGED", "RESTOCK", "store"),
        ("gear", "like_new", 10): ("M-GEAR-LN-FRESH", "RESTOCK", "store"),
        ("gear", "like_new", 60): ("M-GEAR-LN-SEASON", "OUTLET", "outlet"),
        ("gear", "like_new", 120): ("M-GEAR-LN-AGED", "OUTLET", "outlet"),
        ("gear", "used", 10): ("M-GEAR-USED-FRESH", "REFURBISH", "store"),
        ("gear", "used", 60): ("M-GEAR-USED-SEASON", "REFURBISH", "store"),
        ("gear", "used", 120): ("M-GEAR-USED-AGED", "LIQUIDATE", "liquidator"),
        ("electronics", "new_with_tags", 10): ("M-ELEC-NWT-FRESH", "RESTOCK", "store"),
        ("electronics", "new_with_tags", 60): ("M-ELEC-NWT-SEASON", "RESTOCK", "store"),
        ("electronics", "new_with_tags", 120): ("M-ELEC-NWT-AGED", "RETURN_TO_VENDOR", "vendor"),
        ("electronics", "like_new", 10): ("M-ELEC-LN-FRESH", "REFURBISH", "store"),
        ("electronics", "like_new", 60): ("M-ELEC-LN-SEASON", "REFURBISH", "store"),
        ("electronics", "like_new", 120): ("M-ELEC-LN-AGED", "OUTLET", "outlet"),
        ("electronics", "used", 10): ("M-ELEC-USED-FRESH", "HOLD_FOR_INSPECTION", "store"),
        ("electronics", "used", 60): ("M-ELEC-USED-SEASON", "HOLD_FOR_INSPECTION", "store"),
        ("electronics", "used", 120): ("M-ELEC-USED-AGED", "RECYCLE", "recycler"),
    }
    primaries_seen: set[str] = set()
    for (category, condition, age_days), (cell_id, disposition, channel) in SWEEP.items():
        band = matrix.band_for(age_days).band_id
        primary = matrix.primary_cell(category, condition, band)
        primaries_seen.add(primary.cell_id)
        outcome = decide_disposition(
            matrix, channels, case_id="CASE-SWEEP", computation_id=None,
            category=category, condition_declared=condition, age_days=age_days,
        )
        if outcome.status != "decided" or (outcome.cell_id, outcome.disposition, outcome.channel) != (
            cell_id, disposition, channel,
        ):
            counters["wrong_dispositions"] += 1
            continue
        # A reroute that happened must be on the record, and the
        # channel finally used must be open in the shipped table.
        if outcome.cell_id != primary.cell_id:
            if not outcome.reroutes or outcome.reroutes[-1].to_cell_id != outcome.cell_id:
                counters["silent_reroutes"] += 1
        elif outcome.reroutes:
            counters["silent_reroutes"] += 1
        if channels.state(category, outcome.channel) != "open":
            counters["silent_reroutes"] += 1
        if outcome.disposition == "HOLD_FOR_INSPECTION" and outcome.inspection_unknowns != [
            "functional_test_result", "data_wipe_status",
        ]:
            counters["hold_unknowns_unnamed"] += 1
    all_primaries = {c.cell_id for c in matrix.cells if not c.fallback_only}
    counters["matrix_rows_unexercised"] += len(all_primaries - primaries_seen)

    # Not-decidable probes: each must come back not_decidable naming
    # its dimension — a disposition here is a guess, and counts.
    for facts, dimension in (
        (dict(category="gear", condition_declared="waterlogged", age_days=10), "condition_declared"),
        (dict(category="furniture", condition_declared="used", age_days=10), "category"),
        (dict(category=None, condition_declared="used", age_days=10), "category"),
        (dict(category="gear", condition_declared="used", age_days=None), "item_age_band"),
        (dict(category="gear", condition_declared="used", age_days=-3), "item_age_band"),
    ):
        outcome = decide_disposition(matrix, channels, case_id="CASE-PROBE", computation_id=None, **facts)
        if outcome.status != "not_decidable" or outcome.missing_dimension != dimension or outcome.disposition is not None:
            counters["undecidable_but_decided"] += 1
    # The service-level probe from R2 (REQ-2098, waterlogged) must be
    # stored as a not_decidable artefact, not a decided one.
    probe_disp = service.get_disposition("CASE-2098")
    if probe_disp is None or probe_disp.status != "not_decidable" or probe_disp.missing_dimension != "condition_declared":
        counters["undecidable_but_decided"] += 1

    # --- R4: fraud scoring against hand-derived planted truth ----------
    # Hand-derived from the seed history (see seed.py): CUST-SER
    # velocity (7-2)x8=40 at cap + share 8/8=1.0000 -> 35 + churn 4
    # cards -> 20 = 95.00 FRAUD_REVIEW; CUST-WARD wardrobing 3x12=36
    # at cap, everything else named-insufficient or zero = 36.00
    # MONITOR; every R1 feed customer scores 0.00 CLEAR.
    fraud = CaseService(make_store(), OfflineComposer())
    fraud.seed_orders()
    fraud.seed_history()
    for request in PLANTED_REQUESTS:
        clean = fraud.ingest(request)
        if clean.fraud_score.total_score != Decimal("0.00") or clean.fraud_score.band != "CLEAR":
            counters["fraud_band_mismatches"] += 1
    ser = fraud.ingest(FRAUD_FEED_REQUESTS[0])
    ward = fraud.ingest(FRAUD_FEED_REQUESTS[1])

    def _sig(score, signal_id):
        return next(s for s in score.signals if s.signal_id == signal_id)

    ser_expected = {
        "return_velocity": ("computed", "7", Decimal("40.00")),
        "serial_returner_share": ("computed", "1.0000", Decimal("35.00")),
        "wardrobing_indicator": ("computed", "0", Decimal("0.00")),
        "weight_mismatch": ("not_computable", None, Decimal("0")),
        "card_churn": ("computed", "4", Decimal("20.00")),
    }
    if ser.fraud_score.total_score != Decimal("95.00") or ser.fraud_score.band != "FRAUD_REVIEW":
        counters["fraud_band_mismatches"] += 1
    for signal_id, (status, raw, contribution) in ser_expected.items():
        got = _sig(ser.fraud_score, signal_id)
        if (got.status, got.raw_value, got.contribution) != (status, raw, contribution):
            counters["fraud_band_mismatches"] += 1
        if got.status != "computed" and not got.missing_input:
            counters["fraud_band_mismatches"] += 1
    if ser.fraud_state != "fraud_review" or not fraud.refund_path_blocked("CASE-3008"):
        counters["fraud_band_mismatches"] += 1

    ward_expected = {
        "return_velocity": ("computed", "2", Decimal("0.00")),
        "serial_returner_share": ("insufficient_data", None, Decimal("0")),
        "wardrobing_indicator": ("computed", "3", Decimal("36.00")),
        "weight_mismatch": ("not_computable", None, Decimal("0")),
        "card_churn": ("insufficient_data", None, Decimal("0")),
    }
    if ward.fraud_score.total_score != Decimal("36.00") or ward.fraud_score.band != "MONITOR":
        counters["fraud_band_mismatches"] += 1
    for signal_id, (status, raw, contribution) in ward_expected.items():
        got = _sig(ward.fraud_score, signal_id)
        if (got.status, got.raw_value, got.contribution) != (status, raw, contribution):
            counters["fraud_band_mismatches"] += 1
        if got.status != "computed" and not got.missing_input:
            counters["fraud_band_mismatches"] += 1
    if ward.fraud_state != "monitor" or fraud.refund_path_blocked("CASE-3103"):
        counters["fraud_band_mismatches"] += 1

    # Reproducibility: every stored score re-performs clean, a
    # re-score over unchanged inputs returns the same artefact, and
    # an independently seeded twin service agrees byte for byte.
    for scored_case in fraud.list_cases():
        if scored_case.fraud_score is not None and fraud.verify_stored_fraud_score(scored_case.case_id):
            counters["fraud_score_irreproducibility"] += 1
    if fraud.rescore_case("CASE-3008").score_id != ser.fraud_score.score_id:
        counters["fraud_score_irreproducibility"] += 1
    twin = CaseService(make_store(), OfflineComposer())
    twin.seed_orders()
    twin.seed_history()
    twin_ser = twin.ingest(FRAUD_FEED_REQUESTS[0])
    if twin_ser.fraud_score.model_dump(mode="json") != ser.fraud_score.model_dump(mode="json"):
        counters["fraud_score_irreproducibility"] += 1

    # Bypass probes: none of these may move CASE-3008 forward.
    stored_score = fraud._store.get_fraud_score(ser.fraud_score.score_id)
    tampered_score = {**stored_score, "band": "CLEAR", "total_score": "0.00"}
    fraud._store.put_fraud_score(tampered_score)
    if not fraud.verify_stored_fraud_score("CASE-3008"):
        counters["fraud_block_bypasses"] += 1  # tamper went undetected
    fraud._store.put_fraud_score(stored_score)
    for kwargs in (
        dict(reviewer="r.alvarez", outcome="edit_score", reason_code="legitimate_pattern_explained"),
        dict(reviewer="r.alvarez", outcome="cleared_with_reason", reason_code="pattern_verified"),
        dict(reviewer=" ", outcome="cleared_with_reason", reason_code="legitimate_pattern_explained"),
    ):
        try:
            fraud.record_fraud_review("CASE-3008", **kwargs)
            counters["fraud_block_bypasses"] += 1  # a refused act landed
        except FraudReviewError:
            pass
    try:
        fraud.record_fraud_review("CASE-3103", reviewer="r.alvarez",
                                  outcome="confirmed_fraud", reason_code="pattern_verified")
        counters["fraud_block_bypasses"] += 1
    except FraudReviewError:
        pass
    try:
        fraud.assert_refund_path_open("CASE-3008")
        counters["fraud_block_bypasses"] += 1  # the guard let a blocked case through
    except FraudHoldError:
        pass
    # After every probe: still blocked, and no review artefact exists.
    if not fraud.refund_path_blocked("CASE-3008"):
        counters["fraud_lifted_without_decision"] += 1
    if fraud.get_fraud_review("CASE-3008") is not None:
        counters["fraud_lifted_without_decision"] += 1
    if fraud.get_case("CASE-3008").fraud_state != "fraud_review":
        counters["fraud_lifted_without_decision"] += 1

    # The one legitimate path: a named decision lifts the block, a
    # second decision on the same score is refused, and the score
    # artefact itself is untouched by the act.
    fraud.record_fraud_review("CASE-3008", reviewer="r.alvarez",
                              outcome="cleared_with_reason",
                              reason_code="legitimate_pattern_explained")
    if fraud.refund_path_blocked("CASE-3008") or fraud.get_case("CASE-3008").fraud_state != "fraud_cleared":
        counters["fraud_band_mismatches"] += 1
    if fraud.get_fraud_score("CASE-3008").band != "FRAUD_REVIEW" or fraud.verify_stored_fraud_score("CASE-3008"):
        counters["fraud_band_mismatches"] += 1
    try:
        fraud.record_fraud_review("CASE-3008", reviewer="m.osei",
                                  outcome="confirmed_fraud", reason_code="pattern_verified")
        counters["fraud_block_bypasses"] += 1
    except FraudReviewError:
        pass

    # --- R5: approval gates + the idempotent payments port -------------
    # The planted ladder against the pack caps (auto $50 / dual $150):
    # CASE-2006 $29.05 auto, CASE-2003 $130.50 single, CASE-2001
    # $189.00 dual. The double counts actual money movements, so a
    # double refund is a count, not an opinion.
    DAY5 = date(2026, 10, 27)
    double = LocalPaymentsDouble()
    money = CaseService(make_store(), OfflineComposer(), payments_port=double)
    money.seed_orders()
    for request in PLANTED_REQUESTS:
        money.ingest(request)

    # Auto tier: executes with no approval acts at all.
    r2006 = money.execute_refund("CASE-2006", idempotency_key="eval-2006", as_of=DAY5)
    if r2006.amount != Decimal("29.05") or double.movement_count != 1:
        counters["unapproved_executions"] += 1
    # Same key retried: the original receipt replays, money unmoved.
    replay = money.execute_refund("CASE-2006", idempotency_key="eval-2006", as_of=DAY5)
    if replay.receipt_id != r2006.receipt_id or double.movement_count != 1:
        counters["double_refunds"] += 1
    # A different key on the executed computation: duplicate_refund.
    try:
        money.execute_refund("CASE-2006", idempotency_key="eval-2006-again", as_of=DAY5)
        counters["double_refunds"] += 1  # a second refund landed
    except DuplicateRefundError:
        pass
    if double.movement_count != 1 or len(money.list_receipts()) != 1:
        counters["double_refunds"] += 1

    # Single tier: refused with no approver, lands with one.
    try:
        money.execute_refund("CASE-2003", idempotency_key="eval-2003", as_of=DAY5)
        counters["unapproved_executions"] += 1
    except ApprovalError:
        pass
    money.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY5)
    r2003 = money.execute_refund("CASE-2003", idempotency_key="eval-2003", as_of=DAY5)
    if r2003.amount != Decimal("130.50"):
        counters["unapproved_executions"] += 1

    # Dual tier: one approver is not enough, the same person twice is
    # refused at record time, two different approvers land it.
    money.record_approval("CASE-2001", approver="d.okafor", role="returns_lead", as_of=DAY5)
    try:
        money.execute_refund("CASE-2001", idempotency_key="eval-2001", as_of=DAY5)
        counters["unapproved_executions"] += 1
    except ApprovalError:
        pass
    try:
        money.record_approval("CASE-2001", approver="d.okafor", role="finance_manager", as_of=DAY5)
        counters["unapproved_executions"] += 1  # same-person second act accepted
    except ApprovalError:
        pass
    money.record_approval("CASE-2001", approver="m.osei", role="finance_manager", as_of=DAY5)
    r2001 = money.execute_refund("CASE-2001", idempotency_key="eval-2001", as_of=DAY5)
    if r2001.amount != Decimal("189.00"):
        counters["unapproved_executions"] += 1

    # Stale pins: an act naming a non-current computation is refused,
    # and a stored approval pinned to a forged computation id does
    # not count toward the gate.
    case2003 = money.get_case("CASE-2003")
    try:
        money.record_approval(
            "CASE-2003", approver="m.osei", role="finance_manager", as_of=DAY5,
            computation_id="RC-not-the-current-one",
        )
        counters["stale_computation_approvals_accepted"] += 1
    except ApprovalError:
        pass
    forged = RefundApproval(
        approval_id="RA-evalforged0000", case_id="CASE-2003",
        computation_id="RC-not-the-current-one", approver="x.y",
        role="finance_manager", as_of=DAY5, amount=Decimal("130.50"),
        tier="single", policy_pack_version=case2003.computation.policy_pack_version,
    )
    money._store.put_approval(forged.model_dump(mode="json"))
    if money.approval_status("CASE-2003")["approvers"] != ["d.okafor"]:
        counters["stale_computation_approvals_accepted"] += 1

    # Fraud-blocked: CASE-3008 (fresh service, history loaded, the
    # FRAUD_REVIEW case from R4) refuses approval AND execution
    # through the R4 guard; after the named clearance it executes
    # through the ordinary gates.
    blocked_double = LocalPaymentsDouble()
    blocked = CaseService(make_store(), OfflineComposer(), payments_port=blocked_double)
    blocked.seed_orders()
    blocked.seed_history()
    blocked.ingest(FRAUD_FEED_REQUESTS[0])
    try:
        blocked.record_approval("CASE-3008", approver="d.okafor", role="returns_lead", as_of=DAY5)
        counters["fraud_blocked_executions"] += 1
    except FraudHoldError:
        pass
    try:
        blocked.execute_refund("CASE-3008", idempotency_key="eval-3008", as_of=DAY5)
        counters["fraud_blocked_executions"] += 1
    except FraudHoldError:
        pass
    if blocked_double.movement_count != 0 or blocked.list_receipts():
        counters["fraud_blocked_executions"] += 1
    blocked.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez", outcome="cleared_with_reason",
        reason_code="legitimate_pattern_explained",
    )
    blocked.record_approval("CASE-3008", approver="d.okafor", role="returns_lead", as_of=DAY5)
    r3008 = blocked.execute_refund("CASE-3008", idempotency_key="eval-3008", as_of=DAY5)
    if r3008.amount != Decimal("116.00") or blocked_double.movement_count != 1:
        counters["fraud_blocked_executions"] += 1

    # Failure path: an injected mid-execution failure leaves no
    # receipt and no movement; the retry lands exactly one of each,
    # and the execution record carries the whole trail.
    flaky_double = LocalPaymentsDouble(fail_times={"eval-flaky": 1})
    flaky = CaseService(make_store(), OfflineComposer(), payments_port=flaky_double)
    flaky.seed_orders()
    for request in PLANTED_REQUESTS:
        flaky.ingest(request)
    try:
        flaky.execute_refund("CASE-2006", idempotency_key="eval-flaky", as_of=DAY5)
        counters["failed_execution_partial_receipts"] += 1  # the failure did not raise
    except PaymentsError:
        pass
    if flaky.get_receipt("CASE-2006") is not None or flaky.list_receipts():
        counters["failed_execution_partial_receipts"] += 1
    if flaky_double.movement_count != 0:
        counters["failed_execution_partial_receipts"] += 1
    failed_execution = flaky.get_execution_by_key("eval-flaky")
    if failed_execution is None or failed_execution.status != "failed" or failed_execution.receipt_id is not None:
        counters["failed_execution_partial_receipts"] += 1
    landed = flaky.execute_refund("CASE-2006", idempotency_key="eval-flaky", as_of=DAY5)
    if landed.amount != Decimal("29.05") or flaky_double.movement_count != 1 or len(flaky.list_receipts()) != 1:
        counters["failed_execution_partial_receipts"] += 1
    trail = [t.state for t in flaky.get_execution_by_key("eval-flaky").transitions]
    if trail != ["requested", "executing", "failed", "executing", "executed"]:
        counters["failed_execution_partial_receipts"] += 1

    # --- R6: carrier tools behind the gateway + the timeline ---------
    # Labels issue only through the gateway, against the carrier
    # double (which counts what it actually issued). The timeline is
    # graded against the record: gaps that exist must be named, and
    # a fully walked case must claim none.
    DAY6 = date(2026, 10, 27)
    carrier = LocalCarrierDouble()
    ship = CaseService(make_store(), OfflineComposer(), carrier_port=carrier)
    ship.seed_orders()
    for request in PLANTED_REQUESTS:
        ship.ingest(request)

    def _issue(svc, case_id, on=DAY6):
        return svc.invoke_tool(
            "create_return_label", {"case_id": case_id, "issued_on": on},
            case_id=case_id, called_on=on,
        )

    # Idempotency: a second issue returns the same artefact, the
    # store holds one label, and the port issued exactly one.
    first2003 = _issue(ship, "CASE-2003")
    again2003 = _issue(ship, "CASE-2003")
    if (
        again2003.label_id != first2003.label_id
        or len(ship.list_labels("CASE-2003")) != 1
        or carrier.label_count != 1
    ):
        counters["duplicate_labels"] += 1

    # The ineligible case refuses, and nothing is stored.
    try:
        _issue(ship, "CASE-2004")
        counters["label_for_ineligible_or_blocked_accepted"] += 1
    except CarrierError:
        pass
    if ship.list_labels("CASE-2004"):
        counters["label_for_ineligible_or_blocked_accepted"] += 1

    # The fraud-blocked case refuses through the R4 guard — and its
    # timeline names the pending review decision as the gap.
    blocked_ship = CaseService(make_store(), OfflineComposer())
    blocked_ship.seed_orders()
    blocked_ship.seed_history()
    blocked_ship.ingest(FRAUD_FEED_REQUESTS[0])
    try:
        _issue(blocked_ship, "CASE-3008")
        counters["label_for_ineligible_or_blocked_accepted"] += 1
    except FraudHoldError:
        pass
    if blocked_ship.list_labels("CASE-3008"):
        counters["label_for_ineligible_or_blocked_accepted"] += 1
    blocked_view = blocked_ship.get_timeline("CASE-3008", DAY6)
    if [g.kind for g in blocked_view.gaps] != ["fraud_review_decision"]:
        counters["timeline_gaps_unnamed"] += 1

    # A ship-by date passed with no label is named (CASE-2001 has no
    # label on this service yet — its label is issued below).
    shipby_view = ship.get_timeline("CASE-2001", date(2026, 12, 1))
    if [g.kind for g in shipby_view.gaps] != ["return_label"]:
        counters["timeline_gaps_unnamed"] += 1

    # Reconciliation honesty: for every label issued here, the
    # artefact must state the exact variance against the
    # computation's return-shipping line — a nonzero variance may
    # never be filed as "matched", and none may be absent.
    for label in (first2003, _issue(ship, "CASE-2006"), _issue(ship, "CASE-2001")):
        computation6 = ship.get_computation(label.case_id)
        shipping_line = next(
            ln for ln in computation6.lines if ln.line_id == "return_shipping"
        )
        rec = label.reconciliation
        if shipping_line.amount is None:
            if rec.status != "not_reconcilable":
                counters["label_cost_mismatches_hidden"] += 1
            continue
        expected_variance = label.cost - shipping_line.amount
        if rec.deducted_amount != shipping_line.amount or rec.variance != expected_variance:
            counters["label_cost_mismatches_hidden"] += 1
        if expected_variance == 0 and rec.status != "matched":
            counters["label_cost_mismatches_hidden"] += 1
        if expected_variance != 0 and rec.status == "matched":
            counters["label_cost_mismatches_hidden"] += 1
    # The planted electronics variance, exactly: $8.50 charged,
    # $6.95 deducted, $1.55 standing on the record.
    label2006 = ship.get_active_label("CASE-2006")
    if (
        label2006.reconciliation.status != "variance"
        or label2006.reconciliation.variance != Decimal("1.55")
    ):
        counters["label_cost_mismatches_hidden"] += 1

    # A refund executed with no label of record (the R5 `money`
    # service executed CASE-2006 and never issued one) is named.
    money_view = money.get_timeline("CASE-2006", DAY5)
    if [g.kind for g in money_view.gaps] != ["return_label"]:
        counters["timeline_gaps_unnamed"] += 1

    # The fully walked case — label, approval, execution, carrier
    # events — claims no gaps, and every entry names its artefact.
    ship.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY6)
    ship.execute_refund("CASE-2003", idempotency_key="eval-r6-walk", as_of=DAY6)
    walked = ship.get_timeline("CASE-2003", date(2026, 10, 30))
    if walked.gaps:
        counters["timeline_gaps_unnamed"] += 1
    if any(not e.artefact_id for e in walked.entries):
        counters["timeline_gaps_unnamed"] += 1
    walked_stages = [e.stage for e in walked.entries]
    for expected_stage in ("label", "carrier_event", "approval", "receipt"):
        if expected_stage not in walked_stages:
            counters["timeline_gaps_unnamed"] += 1

    # Pickups: a non-bulky category refuses naming the pack rule;
    # the bulky tent case books against its active (freight) label.
    try:
        ship.invoke_tool(
            "book_pickup", {"case_id": "CASE-2003", "scheduled_on": DAY6},
            case_id="CASE-2003", called_on=DAY6,
        )
        counters["non_bulky_pickups_accepted"] += 1
    except CarrierError:
        pass
    tent = ship.ingest(
        ReturnRequest(
            request_id="REQ-2010", order_id="ORD-1002", sku="TENT-2P", qty=1,
            reason_code="changed_mind", condition_declared="used",
            requested_on=date(2026, 9, 15),
        )
    )
    tent_label = _issue(ship, tent.case_id, on=date(2026, 9, 16))
    if tent_label.service_level != "freight" or tent_label.cost != Decimal("24.00"):
        counters["label_cost_mismatches_hidden"] += 1
    booking = ship.invoke_tool(
        "book_pickup", {"case_id": tent.case_id, "scheduled_on": date(2026, 9, 17)},
        case_id=tent.case_id, called_on=date(2026, 9, 17),
    )
    if booking.label_id != tent_label.label_id or booking.status != "booked":
        counters["non_bulky_pickups_accepted"] += 1

    # --- R7: warehouse grading + the supersession cascade ------------
    # Hand-computed truth: CASE-2003 boots, declared like_new —
    # $145.00 at a 10% fee = $130.50; graded used — a 20% fee =
    # $116.00, delta −$14.50, disposition RESTOCK → OUTLET. CASE-2099
    # (same boots, declared used) executes at $116.00 and grades
    # like_new: +$14.50 owed back through the gated top-up. CASE-2006
    # is the held lamp: no grading finalises without both inspection
    # facts, and pass + verified wipe resolves it to REFURBISH.
    DAY7 = date(2026, 10, 27)
    RECEIVED7 = date(2026, 10, 30)  # the carrier script's received day
    GRADED7 = date(2026, 10, 31)

    def _grade_service():
        svc = CaseService(make_store(), OfflineComposer())
        svc.seed_orders()
        return svc

    def _label7(svc, case_id):
        return svc.invoke_tool(
            "create_return_label", {"case_id": case_id, "issued_on": DAY7},
            case_id=case_id, called_on=DAY7,
        )

    def _snapshot(svc, case_id):
        store = svc._store
        return (
            {p["computation_id"]: p for p in store.list_computations_for_case(case_id)},
            {p["disposition_id"]: p for p in store.list_dispositions_for_case(case_id)},
            {p["score_id"]: p for p in store.list_fraud_scores(case_id)},
        )

    def _check_unedited(svc, case_id, before):
        after = _snapshot(svc, case_id)
        for kind in range(3):
            for artefact_id, payload in before[kind].items():
                if after[kind].get(artefact_id) != payload:
                    counters["in_place_artefact_edits"] += 1

    # The gates: no receipt before the carrier's word, grading only
    # after a receipt, never by the receiver, never twice.
    gated = _grade_service()
    gated.ingest(PLANTED_REQUESTS[2])  # REQ-2003
    try:
        gated.record_grading(
            "CASE-2003", grader="p.novak", graded_condition="used", graded_on=GRADED7
        )
        counters["grading_gate_bypasses"] += 1  # graded with nothing received
    except GradingError:
        pass
    _label7(gated, "CASE-2003")
    try:
        gated.record_warehouse_receipt(
            "CASE-2003", received_by="w.chen", received_on=date(2026, 10, 28)
        )
        counters["grading_gate_bypasses"] += 1  # carrier shows it in transit still
    except GradingError:
        pass
    gated.record_warehouse_receipt("CASE-2003", received_by="w.chen", received_on=RECEIVED7)
    try:
        gated.record_grading(
            "CASE-2003", grader="w.chen", graded_condition="used", graded_on=GRADED7
        )
        counters["grading_gate_bypasses"] += 1  # the receiver grading their own parcel
    except GradingError:
        pass

    # The cascade itself, against hand-computed truth.
    before = _snapshot(gated, "CASE-2003")
    report = gated.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="used", graded_on=GRADED7,
        damage_notes="heel counter scuffed",
    )
    new_computation = gated.get_computation("CASE-2003")
    new_disposition = gated.get_disposition("CASE-2003")
    if (
        report.refund_delta != Decimal("-14.50")
        or new_computation.refund_total != Decimal("116.00")
        or new_computation.condition_basis != "graded"
        or new_disposition.disposition != "OUTLET"
        or new_disposition.basis != "graded"
        or new_disposition.supersedes != report.superseded_disposition_id
    ):
        counters["grading_delta_mismatches"] += 1
    if gated.list_adjustments("CASE-2003"):
        counters["grading_delta_mismatches"] += 1  # nothing executed: no position
    if gated.verify_stored_computation("CASE-2003") or gated.verify_stored_disposition("CASE-2003"):
        counters["grading_delta_mismatches"] += 1
    _check_unedited(gated, "CASE-2003", before)
    try:
        gated.record_grading(
            "CASE-2003", grader="g.osei", graded_condition="like_new", graded_on=GRADED7
        )
        counters["grading_gate_bypasses"] += 1  # a second grading act landed
    except GradingError:
        pass

    # The held lamp: nothing finalises without both inspection facts.
    held = _grade_service()
    held.ingest(PLANTED_REQUESTS[5])  # REQ-2006
    _label7(held, "CASE-2006")
    held.record_warehouse_receipt("CASE-2006", received_by="w.chen", received_on=RECEIVED7)
    for kwargs in (
        {},
        {"functional_test_result": "pass"},
        {"data_wipe_status": "verified"},
    ):
        try:
            held.record_grading(
                "CASE-2006", grader="p.novak", graded_condition="used",
                graded_on=GRADED7, **kwargs,
            )
            counters["holds_finalised_without_fields"] += 1
        except GradingError:
            pass
    if held.list_grading_reports("CASE-2006"):
        counters["holds_finalised_without_fields"] += 1
    held_before = _snapshot(held, "CASE-2006")
    held_report = held.record_grading(
        "CASE-2006", grader="p.novak", graded_condition="used", graded_on=GRADED7,
        functional_test_result="pass", data_wipe_status="verified",
    )
    resolved = held.get_disposition("CASE-2006")
    if (
        resolved.disposition != "REFURBISH"
        or resolved.cell_id != "GR-ELEC-PASS-WIPED"
        or resolved.supersedes != held_report.superseded_disposition_id
        or held_report.new_computation_id is not None
    ):
        counters["grading_delta_mismatches"] += 1
    _check_unedited(held, "CASE-2006", held_before)

    # Executed, then graded WORSE: a recovery position, and no
    # surface through which money can move again.
    settled = _grade_service()
    settled.ingest(PLANTED_REQUESTS[2])  # REQ-2003
    settled.record_approval(
        "CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY7
    )
    settled.execute_refund("CASE-2003", idempotency_key="eval-r7-rec", as_of=DAY7)
    _label7(settled, "CASE-2003")
    settled.record_warehouse_receipt("CASE-2003", received_by="w.chen", received_on=RECEIVED7)
    settled.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="used", graded_on=GRADED7
    )
    (recovery,) = settled.list_adjustments("CASE-2003")
    if (
        recovery.kind != "recovery_due"
        or recovery.amount != Decimal("14.50")
        or recovery.status != "open"
    ):
        counters["grading_delta_mismatches"] += 1
    try:
        settled.execute_top_up("CASE-2003", idempotency_key="tp-rec", as_of=GRADED7)
        counters["ungated_top_ups"] += 1  # a recovery is not a top-up
    except PaymentsError:
        pass
    settled.record_approval(
        "CASE-2003", approver="m.osei", role="finance_manager", as_of=GRADED7
    )
    try:
        settled.execute_refund("CASE-2003", idempotency_key="eval-r7-again", as_of=GRADED7)
        counters["ungated_top_ups"] += 1  # the settled refund executed twice
    except PaymentsError:
        pass

    # Executed, then graded BETTER: the top-up, gated on the NEW
    # computation's approvals, in the topup: key domain, exactly once.
    topup_double = LocalPaymentsDouble()
    owing = CaseService(make_store(), OfflineComposer(), payments_port=topup_double)
    owing.seed_orders()
    owing.ingest(
        ReturnRequest(
            request_id="REQ-2099", order_id="ORD-1003", sku="BOOT-TRL", qty=1,
            reason_code="wrong_size", condition_declared="used",
            requested_on=date(2026, 10, 26),
        )
    )
    owing.record_approval(
        "CASE-2099", approver="d.okafor", role="returns_lead", as_of=DAY7
    )
    owing.execute_refund("CASE-2099", idempotency_key="eval-r7-orig", as_of=DAY7)
    _label7(owing, "CASE-2099")
    owing.record_warehouse_receipt("CASE-2099", received_by="w.chen", received_on=RECEIVED7)
    owing_before = _snapshot(owing, "CASE-2099")
    owing_report = owing.record_grading(
        "CASE-2099", grader="p.novak", graded_condition="like_new", graded_on=GRADED7
    )
    (top_up,) = owing.list_adjustments("CASE-2099")
    if (
        owing_report.refund_delta != Decimal("14.50")
        or top_up.kind != "top_up_due"
        or top_up.amount != Decimal("14.50")
    ):
        counters["grading_delta_mismatches"] += 1
    try:
        owing.execute_top_up("CASE-2099", idempotency_key="tp-1", as_of=GRADED7)
        counters["ungated_top_ups"] += 1  # landed on the OLD computation's approval
    except ApprovalError:
        pass
    owing.record_approval(
        "CASE-2099", approver="m.osei", role="finance_manager", as_of=GRADED7
    )
    top_receipt = owing.execute_top_up("CASE-2099", idempotency_key="tp-1", as_of=GRADED7)
    if (
        top_receipt.amount != Decimal("14.50")
        or top_receipt.idempotency_key != "topup:tp-1"
        or topup_double.movement_count != 2
    ):
        counters["ungated_top_ups"] += 1
    try:
        owing.execute_top_up("CASE-2099", idempotency_key="tp-2", as_of=GRADED7)
        counters["ungated_top_ups"] += 1  # a second top-up under a fresh key
    except DuplicateRefundError:
        pass
    _check_unedited(owing, "CASE-2099", owing_before)

    # The fraud trigger: the jacket declared new-with-tags, graded
    # used (two rank steps) with wear evidence and a 100 g weigh-in
    # against an 820 g product — the re-score lands the empty-box
    # band (30.00 → MONITOR) on a NEW artefact; without the full
    # trigger (boots, one rank step) nothing re-scores.
    fraud7 = _grade_service()
    fraud7.ingest(PLANTED_REQUESTS[0])  # REQ-2001
    score_before = fraud7.get_fraud_score("CASE-2001")
    _label7(fraud7, "CASE-2001")
    fraud7.record_warehouse_receipt("CASE-2001", received_by="w.chen", received_on=RECEIVED7)
    fraud_before = _snapshot(fraud7, "CASE-2001")
    fraud_report = fraud7.record_grading(
        "CASE-2001", grader="p.novak", graded_condition="used", graded_on=GRADED7,
        received_weight_grams=100, wear_evidence=True,
    )
    rescored = fraud7.get_fraud_score("CASE-2001")
    if (
        not fraud_report.fraud_rescore_triggered
        or rescored.score_id == score_before.score_id
        or rescored.supersedes != score_before.score_id
        or rescored.total_score != Decimal("30.00")
        or rescored.band != "MONITOR"
    ):
        counters["grading_delta_mismatches"] += 1
    _check_unedited(fraud7, "CASE-2001", fraud_before)

    # --- R8: customer messaging — templates, verifier, lifecycle ------
    # Every ingested case's summary stands as a verified message.
    # The happy path walks three message types to the messaging
    # double and proves the delivered texts are exactly the stored
    # verified texts. The four planted probes — an inflated figure,
    # a wrong disposition fact, a fraud notice leaking its internals,
    # a queued message going stale after grading — must never reach
    # the customer.
    DAY8 = date(2026, 10, 27)
    RECEIVED8 = date(2026, 10, 30)
    GRADED8 = date(2026, 10, 31)
    msg_double = LocalMessagingDouble()
    msg = CaseService(make_store(), OfflineComposer(), messaging_port=msg_double)
    msg.seed_orders()
    msg.seed_history()
    for request in PLANTED_REQUESTS:
        msg.ingest(request)
    msg.ingest(FRAUD_FEED_REQUESTS[0])  # CASE-3008, the serial returner

    summaries = [m for m in msg.list_all_messages() if m.message_type == "return_summary"]
    if len(summaries) != len(PLANTED_REQUESTS) + 1 or any(
        m.status != "verified" for m in summaries
    ):
        counters["figure_fact_violations_sent"] += 1  # an honest summary failed its own verifier

    def _lifecycle8(case_id, message_type, on=DAY8, note=None):
        draft = msg.draft_message(case_id, message_type, as_of=on, model_note=note)
        msg.verify_message(draft.message_id, as_of=on)
        msg.queue_message(draft.message_id, as_of=on)
        return msg.send_message(draft.message_id, as_of=on)

    _label7(msg, "CASE-2003")
    sent_label = _lifecycle8("CASE-2003", "label_instructions")
    msg.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY8)
    msg.execute_refund("CASE-2003", idempotency_key="eval-r8-exec", as_of=DAY8)
    sent_exec = _lifecycle8("CASE-2003", "refund_executed_notice")
    msg.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez", outcome="cleared_with_reason",
        reason_code="legitimate_pattern_explained",
    )
    sent_fraud = _lifecycle8("CASE-3008", "fraud_review_notice")
    for sent in (sent_label, sent_exec, sent_fraud):
        if sent.status != "sent" or msg_double.deliveries.get(sent.message_id) != sent.text:
            counters["unverified_message_sends"] += 1
    if msg_double.delivery_count != 3:
        counters["unverified_message_sends"] += 1
    # A repeat send is the same act — the count must not move.
    msg.send_message(sent_label.message_id, as_of=DAY8)
    if msg_double.delivery_count != 3:
        counters["unverified_message_sends"] += 1

    # Probe 1 — figure: a note slot inflating the refund is refused.
    probe_fig = msg.draft_message(
        "CASE-2003", "return_summary", as_of=DAY8, model_note="Your refund is $999.99."
    )
    refused_fig = msg.verify_message(probe_fig.message_id, as_of=DAY8)
    if refused_fig.status != "refused" or not refused_fig.violations:
        counters["figure_fact_violations_sent"] += 1
    try:
        msg.queue_message(probe_fig.message_id, as_of=DAY8)
        counters["figure_fact_violations_sent"] += 1  # a refused message queued
    except MessagingError:
        pass
    if probe_fig.message_id in msg_double.deliveries:
        counters["figure_fact_violations_sent"] += 1

    # Probe 2 — fact: CASE-2006 is HOLD_FOR_INSPECTION; a note
    # claiming restock states a disposition the record does not hold.
    probe_fact = msg.draft_message(
        "CASE-2006", "return_summary", as_of=DAY8,
        model_note="Good news: the item will be restocked right away.",
    )
    refused_fact = msg.verify_message(probe_fact.message_id, as_of=DAY8)
    if refused_fact.status != "refused" or not any(
        "fact" in v for v in refused_fact.violations
    ):
        counters["figure_fact_violations_sent"] += 1
    if probe_fact.message_id in msg_double.deliveries:
        counters["figure_fact_violations_sent"] += 1

    # Probe 3 — forbidden: a fraud notice leaking the signal and the
    # score is refused; and no delivered text anywhere may carry the
    # disclosure pack's vocabulary.
    probe_leak = msg.draft_message(
        "CASE-3008", "fraud_review_notice", as_of=DAY8,
        model_note="Your return_velocity pattern scored 95 points, over the review threshold.",
    )
    refused_leak = msg.verify_message(probe_leak.message_id, as_of=DAY8)
    if refused_leak.status != "refused" or not any(
        "forbidden" in v for v in refused_leak.violations
    ):
        counters["forbidden_content_sent"] += 1
    if probe_leak.message_id in msg_double.deliveries:
        counters["forbidden_content_sent"] += 1
    for delivered_text in msg_double.deliveries.values():
        if any(
            term in delivered_text
            for term in (
                "return_velocity", "serial_returner_share", "wardrobing_indicator",
                "weight_mismatch", "card_churn", "threshold", "FRAUD_REVIEW",
            )
        ):
            counters["forbidden_content_sent"] += 1

    # Probe 4 — stale: the lamp's summary is queued, then grading
    # supersedes the artefacts it renders. The send must refuse, the
    # message must stand withdrawn, nothing stale may be delivered,
    # and the verified replacement must send cleanly.
    _label7(msg, "CASE-2006")
    queued_lamp = [m for m in msg.list_messages("CASE-2006") if m.status == "verified"][-1]
    msg.queue_message(queued_lamp.message_id, as_of=DAY8)
    msg.record_warehouse_receipt("CASE-2006", received_by="w.chen", received_on=RECEIVED8)
    msg.record_grading(
        "CASE-2006", grader="p.novak", graded_condition="used", graded_on=GRADED8,
        functional_test_result="pass", data_wipe_status="verified",
    )
    try:
        msg.send_message(queued_lamp.message_id, as_of=GRADED8)
        counters["stale_message_sends"] += 1  # a stale send landed
    except MessagingError:
        pass
    if msg.get_message(queued_lamp.message_id).status != "withdrawn":
        counters["stale_message_sends"] += 1
    if queued_lamp.message_id in msg_double.deliveries:
        counters["stale_message_sends"] += 1
    replacement = [m for m in msg.list_messages("CASE-2006") if m.status == "verified"][-1]
    msg.queue_message(replacement.message_id, as_of=GRADED8)
    sent_replacement = msg.send_message(replacement.message_id, as_of=GRADED8)
    if msg_double.deliveries.get(sent_replacement.message_id) != sent_replacement.text:
        counters["stale_message_sends"] += 1

    # ================= R9 — customer memory ==========================================
    # Milestone derivation, customer-scoped logged retrieval,
    # retention tombstones, store/memory score parity, and the
    # messaging universe extended to the case's OWN customer only.
    mem = CaseService(make_store(), OfflineComposer())
    mem.seed_orders()
    mem.seed_history()
    if mem.sync_memory() != 9:
        counters["memory_store_score_divergences"] += 1  # the history backfill contract
    for request in PLANTED_REQUESTS:
        mem.ingest(request)
    for request in FRAUD_FEED_REQUESTS:
        mem.ingest(request)
    _label7(mem, "CASE-2003")
    mem.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY7)
    mem.execute_refund("CASE-2003", idempotency_key="eval-r9-2003", as_of=DAY7)
    mem.record_warehouse_receipt("CASE-2003", received_by="w.chen", received_on=RECEIVED7)
    mem.record_grading("CASE-2003", grader="p.novak", graded_condition="used", graded_on=GRADED7)
    mem.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez",
        outcome="cleared_with_reason", reason_code="legitimate_pattern_explained",
    )
    mem.record_approval("CASE-3008", approver="d.okafor", role="returns_lead", as_of=DAY7)
    mem.execute_refund("CASE-3008", idempotency_key="eval-r9-3008", as_of=DAY7)

    # Milestones: the walked case carries all three of its events,
    # the reviewed case carries its closure record.
    cid_events = {r.event_type for r in mem.retrieve_memory("CUST-CID", as_of=GRADED7)}
    if cid_events != {"case_decided", "refund_executed", "case_graded"}:
        counters["memory_store_score_divergences"] += 1
    ser_events = {r.event_type for r in mem.retrieve_memory("CUST-SER", as_of=GRADED7)}
    if "fraud_review_closed" not in ser_events or "refund_executed" not in ser_events:
        counters["memory_store_score_divergences"] += 1

    # Scoping: no retrieval — and no access-log line — ever crosses
    # a customer boundary; a stranger probe returns nothing.
    index9 = {
        row["record_id"]: row["customer_id"]
        for row in mem._store.list_memory_record_index()
    }
    for customer_id in ("CUST-ANA", "CUST-BEN", "CUST-CID", "CUST-DIA", "CUST-SER", "CUST-WARD"):
        for record in mem.retrieve_memory(customer_id, as_of=GRADED7):
            if record.customer_id != customer_id:
                counters["cross_customer_leaks"] += 1
    if mem.retrieve_memory("CUST-NOBODY", as_of=GRADED7) != []:
        counters["cross_customer_leaks"] += 1
    for entry in mem.list_memory_access_log():
        for rid in entry.returned_record_ids:
            if index9.get(rid) != entry.customer_id:
                counters["cross_customer_leaks"] += 1

    # Parity: every scored case re-scores from memory alone to the
    # very artefact the store path produced.
    for case_id in (
        "CASE-2001", "CASE-2002", "CASE-2003", "CASE-2004", "CASE-2005",
        "CASE-2006", "CASE-2007", "CASE-2008", "CASE-3008", "CASE-3103",
    ):
        stored = mem.get_fraud_score(case_id)
        from_memory = mem.score_case_from_memory(case_id)
        if (
            stored is None
            or from_memory is None
            or from_memory.score_id != stored.score_id
            or from_memory.total_score != stored.total_score
            or from_memory.band != stored.band
        ):
            counters["memory_store_score_divergences"] += 1

    # Messaging: the second boots case may quote CID's OWN earlier
    # $130.50 (memory's record of it) — and never SER's $116.00.
    def _plant_second_boots(svc):
        svc._store.put_order(
            Order(
                order_id="ORD-1009", customer_id="CUST-CID",
                placed_on=date(2026, 10, 26), delivered_on=date(2026, 10, 28),
                lines=[OrderLine(sku="BOOT-TRL", qty=1, unit_price=CATALOGUE["BOOT-TRL"].unit_price)],
                shipping_paid=Decimal("0.00"),
            ).model_dump(mode="json")
        )
        svc.ingest(
            ReturnRequest(
                request_id="REQ-2009", order_id="ORD-1009", sku="BOOT-TRL", qty=1,
                reason_code="changed_mind", condition_declared="new_with_tags",
                requested_on=date(2026, 11, 2),
            )
        )

    _plant_second_boots(mem)
    own_note = mem.draft_message(
        "CASE-2009", "return_summary", as_of=date(2026, 11, 2),
        model_note="As with your earlier return, the refund of $130.50 was "
                   "sent to your original payment method.",
    )
    if mem.verify_message(own_note.message_id, as_of=date(2026, 11, 2)).status != "verified":
        counters["memory_store_score_divergences"] += 1
    alien_note = mem.draft_message(
        "CASE-2009", "return_summary", as_of=date(2026, 11, 2),
        model_note="Your earlier refund of $116.00 has been processed.",
    )
    if mem.verify_message(alien_note.message_id, as_of=date(2026, 11, 2)).status != "refused":
        counters["cross_customer_leaks"] += 1

    # Expiry: under a 20-day pack, December retrieval surfaces
    # nothing, the tombstone counts the expired records, and the
    # expired figure is refused in a message.
    short = CaseService(
        make_store(), OfflineComposer(),
        memory_pack=MemoryPack(version="2026.10.1", retention_days=20),
    )
    short.seed_orders()
    short.seed_history()
    short.ingest(PLANTED_REQUESTS[2])  # CASE-2003
    _label7(short, "CASE-2003")
    short.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY7)
    short.execute_refund("CASE-2003", idempotency_key="eval-r9-short", as_of=DAY7)
    if short.retrieve_memory("CUST-CID", as_of=date(2026, 12, 5)):
        counters["expired_memory_surfaces"] += 1
    status9 = short.memory_retention_status("CUST-CID", as_of=date(2026, 12, 5))
    if status9["active_count"] != 0 or status9["expired_count"] != 2:
        counters["expired_memory_surfaces"] += 1
    _plant_second_boots(short)
    stale_note = short.draft_message(
        "CASE-2009", "return_summary", as_of=date(2026, 12, 5),
        model_note="As with your earlier return, the refund of $130.50 was sent on.",
    )
    if short.verify_message(stale_note.message_id, as_of=date(2026, 12, 5)).status != "refused":
        counters["expired_memory_surfaces"] += 1

    # Logging: four reads, four access-log entries, each carrying
    # its purpose, actor and customer scope.
    before9 = len(mem.list_memory_access_log())
    mem.retrieve_memory("CUST-CID", as_of=GRADED7)
    mem.retrieve_memory("CUST-SER", as_of=GRADED7, purpose="eval_probe")
    mem.memory_retention_status("CUST-WARD", as_of=GRADED7)
    mem.score_case_from_memory("CASE-3008")
    new_entries = mem.list_memory_access_log()[before9:]
    if len(new_entries) != 4:
        counters["unlogged_memory_retrievals"] += 1
    for entry in new_entries:
        if (
            not entry.purpose
            or not entry.actor
            or entry.customer_id not in ("CUST-CID", "CUST-SER", "CUST-WARD")
        ):
            counters["unlogged_memory_retrievals"] += 1

    # The cross-profile fingerprint (R10): the content-addressed
    # artefact ids the planted work minted on this profile's store.
    # Identical planted work must mint identical ids on every store.
    fingerprint = {
        "computations": sorted(
            c["computation_id"] for c in service._store.list_computations()
        ),
        "dispositions": sorted(
            d["disposition_id"] for d in service._store.list_dispositions()
        ),
        "fraud_scores": sorted(
            sc["score_id"] for sc in fraud._store.list_all_fraud_scores()
        ),
        "receipts": sorted(
            r["receipt_id"]
            for svc in (money, flaky, owing)
            for r in svc._store.list_receipts()
        ),
        "labels": sorted(
            lbl["label_id"] for lbl in ship._store.list_all_labels()
        ),
        "messages": sorted(
            m["message_id"] for m in msg._store.list_all_messages()
        ),
        "memory_records": sorted(
            row["record_id"] for row in mem._store.list_memory_record_index()
        ),
    }
    return counters, fingerprint


def _sqlite_factory(tmp_root: Path):
    import itertools

    counter = itertools.count()

    def make():
        store = SqlStore(sqlite_path=tmp_root / f"eval-{next(counter)}.db")
        store.migrate()
        return store

    return make


def _postgres_factory(base_url: str):
    import itertools

    import psycopg

    counter = itertools.count()

    def make():
        schema = f"eval_{next(counter)}"
        with psycopg.connect(base_url, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        sep = "&" if "?" in base_url else "?"
        store = SqlStore(database_url=f"{base_url}{sep}options=-csearch_path%3D{schema}")
        store.migrate()
        return store

    return make


def main() -> int:
    import tempfile

    tmp_root = Path(tempfile.mkdtemp(prefix="returns-evals-"))
    profiles: list[tuple[str, object]] = [
        ("in-memory", lambda: InMemoryStore()),
        ("sqlite", _sqlite_factory(tmp_root)),
    ]
    pg_url = os.environ.get("DATABASE_URL") or os.environ.get("RETURNS_DATABASE_URL")
    if pg_url:
        profiles.append(("postgres", _postgres_factory(pg_url)))

    results: dict[str, tuple[dict, dict]] = {}
    for label, factory in profiles:
        counters, fingerprint = run_scenarios(factory)
        results[label] = (counters, fingerprint)
        print(f"--- profile: {label} ---")
        for name, value in counters.items():
            print(f"{name}: {value} (required 0)")

    # Parity (R10): every profile's counters equal the first
    # profile's, and the artefact-id fingerprints are identical.
    labels = [label for label, _ in profiles]
    base_counters, base_fingerprint = results[labels[0]]
    parity = {"profile_counter_differences": 0, "cross_profile_artefact_id_mismatches": 0}
    for label in labels[1:]:
        counters, fingerprint = results[label]
        for name, value in base_counters.items():
            if counters.get(name) != value:
                parity["profile_counter_differences"] += 1
        for kind, ids in base_fingerprint.items():
            if fingerprint.get(kind) != ids:
                parity["cross_profile_artefact_id_mismatches"] += 1
    print("--- parity across profiles ---")
    for name, value in parity.items():
        print(f"{name}: {value} (required 0)")

    failed = [
        f"{label}:{name}"
        for label, (counters, _) in results.items()
        for name, value in counters.items()
        if value != 0
    ]
    failed += [name for name, value in parity.items() if value != 0]
    print("GATE: PASS" if not failed else f"GATE: FAIL ({', '.join(failed)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
