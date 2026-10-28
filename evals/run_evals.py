"""Eval gate for Blueprint #18 (R1 + R2 + R3 + R4).

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

Prints the counters and GATE: PASS/FAIL; exit code follows.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from returns_resolution.intake import FRAUD_FEED_REQUESTS, PLANTED_REQUESTS
from returns_resolution.modelport import OfflineComposer
from returns_resolution.models import ReturnRequest
from returns_resolution.service import (
    CaseService,
    FigureVerificationError,
    FraudHoldError,
    FraudReviewError,
)
from returns_resolution.store import InMemoryStore
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


def main() -> int:
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
    }

    service = CaseService(InMemoryStore(), OfflineComposer())
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
    tampered_service = CaseService(InMemoryStore(), _TamperedComposer())
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
    fraud = CaseService(InMemoryStore(), OfflineComposer())
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
    twin = CaseService(InMemoryStore(), OfflineComposer())
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

    for name, value in counters.items():
        print(f"{name}: {value} (required 0)")
    failed = [k for k, v in counters.items() if v != 0]
    print("GATE: PASS" if not failed else f"GATE: FAIL ({', '.join(failed)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
