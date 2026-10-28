"""Warehouse grading — the pack and the inspection resolution (R7).

The customer's declared condition drove everything up to the
warehouse door: eligibility's fee schedule, the disposition matrix,
the refund computation. Grading is where the warehouse states what
actually arrived. This module holds the grading pack — data, like
every other pack in the blueprint — plus the two pure pieces of the
grading cascade that are not the matrix walk or the refund engine:

- **Condition ranks.** "Worse" is a stated order over the condition
  vocabulary, not a grader's adjective. The fraud re-score trigger
  reads the rank jump off this table.
- **The inspection resolution.** A used-electronics case is HELD by
  the matrix because two facts are unknown: does it work, and is
  its data wiped? Grading establishes both; this table — versioned
  data — states what a held item becomes for each combination. A
  working, wiped device is refurbished; anything else is recycled
  (a device that does not work, or that still carries a stranger's
  data, is never resold). The successor decision is its own
  content-addressed artefact, built here rather than by the matrix
  walk — the matrix has no cell for "used, but now we know" — with
  the resolution rule's id in its `cell_id`, so the provenance of
  the decision is on the artefact, not implied.

Everything else in the cascade — the graded matrix walk, the refund
re-run, the adjustment position, the re-score — is the existing
machinery of R3/R2/R5/R4, driven by the service in a stated order.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from .disposition import (
    BASIS_GRADED,
    CHANNEL_RECYCLER,
    CHANNEL_STORE,
    RECYCLE,
    REFURBISH,
)

GRADING_PACK_VERSION = "2026.10.1"

# The condition vocabulary grading may state — the same vocabulary
# the customer declares from, plus the barred condition a customer
# may not declare but a warehouse may well find.
CONDITION_VOCABULARY = ["new_with_tags", "like_new", "used", "damaged_by_customer"]

# "Worse" as data: the severity order over the vocabulary. A graded
# condition whose rank exceeds the declared rank by at least
# WILDLY_WORSE_JUMP is "wildly worse" for the fraud trigger.
CONDITION_RANKS = {
    "new_with_tags": 0,
    "like_new": 1,
    "used": 2,
    "damaged_by_customer": 3,
}
WILDLY_WORSE_JUMP = 2

# The inspection outcomes grading may record.
FUNCTIONAL_RESULTS = {"pass", "fail"}
DATA_WIPE_STATES = {"verified", "not_verified"}


class InspectionRule(BaseModel):
    """One row of the inspection resolution: the established facts
    and the disposition they resolve a held item to."""

    rule_id: str
    functional_test_result: str
    data_wipe_status: str  # verified | not_verified | any
    disposition: str
    channel: str
    rationale: str


# The resolution table of record. Order matters: the first rule
# whose facts match decides, so the one resalable combination is
# stated first and everything else falls to certified recycling.
INSPECTION_RULES: list[InspectionRule] = [
    InspectionRule(
        rule_id="GR-ELEC-PASS-WIPED",
        functional_test_result="pass",
        data_wipe_status="verified",
        disposition=REFURBISH,
        channel=CHANNEL_STORE,
        rationale="the device works and its data is verified wiped: it may be refurbished for resale",
    ),
    InspectionRule(
        rule_id="GR-ELEC-FAIL",
        functional_test_result="fail",
        data_wipe_status="any",
        disposition=RECYCLE,
        channel=CHANNEL_RECYCLER,
        rationale="the device does not work: it is recycled, never resold as-is",
    ),
    InspectionRule(
        rule_id="GR-ELEC-PASS-UNWIPED",
        functional_test_result="pass",
        data_wipe_status="not_verified",
        disposition=RECYCLE,
        channel=CHANNEL_RECYCLER,
        rationale=(
            "the device works but its data wipe is not verified: a data-bearing device "
            "is never resold — it goes to certified recycling"
        ),
    ),
]


def resolve_inspection(
    functional_test_result: str | None, data_wipe_status: str | None
) -> InspectionRule | None:
    """The rule that resolves a held item, or None when the facts do
    not resolve it (either fact missing — the report must not
    finalise — or a combination the table does not state)."""
    if functional_test_result is None or data_wipe_status is None:
        return None
    for rule in INSPECTION_RULES:
        if rule.functional_test_result != functional_test_result:
            continue
        if rule.data_wipe_status != "any" and rule.data_wipe_status != data_wipe_status:
            continue
        return rule
    return None


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def inspection_successor_decision(
    *,
    case_id: str,
    computation_id: str | None,
    matrix_version: str,
    channel_table_version: str,
    category: str | None,
    condition_declared: str,
    graded_condition: str,
    age_days: int | None,
    age_band: str | None,
    rule: InspectionRule,
    supersedes: str,
):
    """Build the disposition a held case resolves to, as a
    content-addressed artefact in the same shape as a matrix
    decision: basis "graded", the resolution rule's id as its cell,
    both conditions recorded, and `supersedes` linking it to the
    HOLD decision it replaces — which is never edited.

    The id hashes the full fact set (including the grading pack
    version and the rule), so the artefact is exactly reproducible
    from the grading report it rests on.
    """
    from .models import DispositionDecision

    facts = {
        "case_id": case_id,
        "computation_id": computation_id,
        "matrix_version": matrix_version,
        "channel_table_version": channel_table_version,
        "grading_pack_version": GRADING_PACK_VERSION,
        "category": category,
        "condition_declared": condition_declared,
        "graded_condition": graded_condition,
        "age_days": age_days,
        "age_band": age_band,
        "basis": BASIS_GRADED,
        "rule_id": rule.rule_id,
        "supersedes": supersedes,
    }
    disposition_id = "DD-" + hashlib.sha256(_canonical(facts).encode()).hexdigest()[:16]
    return DispositionDecision(
        disposition_id=disposition_id,
        case_id=case_id,
        computation_id=computation_id,
        matrix_version=matrix_version,
        channel_table_version=channel_table_version,
        status="decided",
        disposition=rule.disposition,
        basis=BASIS_GRADED,
        cell_id=rule.rule_id,
        channel=rule.channel,
        category=category,
        condition_declared=condition_declared,
        graded_condition=graded_condition,
        age_days=age_days,
        age_band=age_band,
        missing_dimension=None,
        reroutes=[],
        inspection_unknowns=[],
        supersedes=supersedes,
    )


def verify_inspection_decision(decision, *, report, held_decision) -> list[str]:
    """Re-perform an inspection-resolution decision from the grading
    report it rests on; [] = intact. The rule is re-resolved from
    the report's established facts and the artefact rebuilt — a
    tampered disposition, channel or link is named."""
    rule = resolve_inspection(report.functional_test_result, report.data_wipe_status)
    if rule is None:
        return ["the grading report's inspection facts resolve to no rule"]
    expected = inspection_successor_decision(
        case_id=decision.case_id,
        computation_id=decision.computation_id,
        matrix_version=decision.matrix_version,
        channel_table_version=decision.channel_table_version,
        category=decision.category,
        condition_declared=decision.condition_declared,
        graded_condition=decision.graded_condition or report.graded_condition,
        age_days=decision.age_days,
        age_band=decision.age_band,
        rule=rule,
        supersedes=held_decision.disposition_id,
    )
    problems: list[str] = []
    if decision.disposition_id != expected.disposition_id:
        problems.append(
            f"disposition_id {decision.disposition_id} != recomputed {expected.disposition_id} "
            "(facts or pack versions changed since this artefact was written)"
        )
    if decision.model_dump(mode="json") != expected.model_dump(mode="json"):
        problems.append("stored disposition differs from a fresh resolution over the same facts")
    return problems
