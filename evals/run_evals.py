"""Eval gate for Blueprint #18 (R1).

Zero-tolerance counters against planted truth:
- eligibility_outcome_mismatches: decisions that differ from the
  planted eligible/denied outcome for each feed request.
- clause_mismatches: decisions whose deny-clause set differs from the
  planted set, or that fail to record pack version + clauses at all.
- window_edge_misgraded: the one-day edge pair (REQ-2003 in,
  REQ-2004 out) graded wrong on either side.
- figure_verifier_violations_accepted: tampered summaries (wrong
  amount, wrong date) that the verifier or the service let through.

Prints the counters and GATE: PASS/FAIL; exit code follows.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from returns_resolution.intake import PLANTED_REQUESTS
from returns_resolution.modelport import OfflineComposer
from returns_resolution.service import CaseService, FigureVerificationError
from returns_resolution.store import InMemoryStore
from returns_resolution.verifier import verify_summary

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
        # The composed summary must itself verify clean.
        if verify_summary(case.summary, case.decision):
            counters["figure_verifier_violations_accepted"] += 1

    if decisions["REQ-2003"].eligible is not True or decisions["REQ-2004"].eligible is not False:
        counters["window_edge_misgraded"] += 1

    # Wrong-figure probes: direct verifier probes + a tampered service.
    good = service.get_case("CASE-2001")
    for tampered in (
        good.summary.replace("$189.00", "$198.00"),
        good.summary.replace("2026-10-20", "2026-10-21"),
    ):
        if not verify_summary(tampered, good.decision):
            counters["figure_verifier_violations_accepted"] += 1
    tampered_service = CaseService(InMemoryStore(), _TamperedComposer())
    tampered_service.seed_orders()
    try:
        tampered_service.ingest(PLANTED_REQUESTS[0])
        counters["figure_verifier_violations_accepted"] += 1  # accepted!
    except FigureVerificationError:
        stored = tampered_service.get_case("CASE-2001")
        if stored is None or stored.summary is not None or stored.status != "summary_failed":
            counters["figure_verifier_violations_accepted"] += 1

    for name, value in counters.items():
        print(f"{name}: {value} (required 0)")
    failed = [k for k, v in counters.items() if v != 0]
    print("GATE: PASS" if not failed else f"GATE: FAIL ({', '.join(failed)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
