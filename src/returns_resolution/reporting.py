"""Management reporting (R17): aggregates over artefacts of record.

Every round before this one produced artefacts a case at a time —
decisions, computations, dispositions, scores, receipts, ledger
entries. R17 is the layer management reads: a small set of named
reports, each computed in code from those artefacts, each carrying
the version of the pack that defines it, the population it covers
and the window it speaks for — and each re-performable by an
independent verifier, so a number on a report is a claim the record
can be made to prove again, not a figure someone typed.

- **The report pack (2026.10.1, below) states the definitions as
  data**: each report's population rule, its window rule and its
  measures. A definition change is a new pack version, never an
  edit — a report names the pack that produced it, so last
  quarter's return rate and this quarter's are only compared when
  the pack says they mean the same thing.
- **Reports are insert-only artefacts** (migration 0013). A
  report's id is content-addressed over its kind, its window, its
  as-of and a fingerprint of the input artefacts it was computed
  from. Recomputing a report whose inputs have not moved returns
  the standing artefact; recomputing after an input is superseded
  mints a NEW report linked by `supersedes` — the old figure
  stands on the record, never edited, exactly like every other
  artefact in this blueprint.
- **The denominators are stated, never invented.** The return
  rate divides returns of record by the orders table — the order
  population that actually exists in this system. The fraud
  report's population is cases with a KNOWN outcome (below); a
  case whose outcome nobody has established is excluded and
  counted as excluded — it is never silently treated as a
  negative, which is how precision figures get flattered.
- **A fraud outcome is known from adjudication, not from the
  score.** The pack states the truth rule: the latest fraud-review
  decision of record decides (confirmed_fraud is an actual
  positive, cleared_with_reason an actual negative); failing any
  decision, a warehouse grading that establishes misrepresentation
  on its own — graded condition at least the grading pack's
  wildly-worse rank jump below the declared condition, with wear
  evidence recorded, the same trigger that re-scores a case in
  R7 — is an actual positive. The prediction graded against that
  truth is the case's CURRENT fraud score band: FRAUD_REVIEW is
  the flag.
- **The money summary forks nothing.** Its four totals are sums
  over the same receipt and adjustment artefacts the operations
  console sums (R13) — computed here from the artefacts of record
  and graded equal to the console payload's rows, so the board
  and the report can never quietly disagree.
- **The cost summary is cumulative, and says why.** Cost ledger
  entries carry no timestamps (R12), so a per-period cost report
  cannot be honestly cut; the report covers every entry of record
  through its as-of, grouped by profile — the offline profile's
  total stated as the $0.00 it is.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel

from .fraud import BAND_FRAUD_REVIEW
from .grading import CONDITION_RANKS, WILDLY_WORSE_JUMP
from .models import money

REPORT_PACK_VERSION = "2026.10.1"

KIND_RETURN_RATE = "return_rate"
KIND_DISPOSITION_MIX = "disposition_mix"
KIND_FRAUD_OUTCOMES = "fraud_outcomes"
KIND_MONEY_SUMMARY = "money_summary"
KIND_COST_SUMMARY = "cost_summary"
REPORT_KINDS = [
    KIND_RETURN_RATE,
    KIND_DISPOSITION_MIX,
    KIND_FRAUD_OUTCOMES,
    KIND_MONEY_SUMMARY,
    KIND_COST_SUMMARY,
]

# The kinds that speak for a stated window; the rest are
# cumulative through their as-of (their inputs carry no dates to
# window by — cost entries have none at all).
WINDOWED_KINDS = [KIND_RETURN_RATE, KIND_DISPOSITION_MIX]

NOT_COMPUTABLE = "not_computable"

_RATIO_QUANT = Decimal("0.0001")


def _ratio(numerator: int, denominator: int) -> str:
    """A ratio as a stated figure: four places, HALF_UP — or the
    named not-computable state when there is nothing to divide
    by. A rate over an empty population is not zero; it is
    undefined, and the report says so."""
    if denominator == 0:
        return NOT_COMPUTABLE
    value = (Decimal(numerator) / Decimal(denominator)).quantize(
        _RATIO_QUANT, rounding=ROUND_HALF_UP
    )
    return format(value, ".4f")


def _money_str(value: Decimal) -> str:
    return format(money(value), ".2f")


class ReportDefinition(BaseModel):
    """One report's definition, as pack data: what population it
    covers, how its window binds, and the measures it states."""

    kind: str
    title: str
    population_rule: str
    window_rule: str
    measures: list[str]


class ReportPack(BaseModel):
    """The report pack: the definitions a number's meaning is
    versioned by. Every report artefact names its pack version."""

    version: str
    definitions: list[ReportDefinition]

    def definition(self, kind: str) -> ReportDefinition:
        for definition in self.definitions:
            if definition.kind == kind:
                return definition
        raise ValueError(f"unknown report kind: {kind!r}")


REPORT_PACK_V1 = ReportPack(
    version=REPORT_PACK_VERSION,
    definitions=[
        ReportDefinition(
            kind=KIND_RETURN_RATE,
            title="Return rate",
            population_rule=(
                "Every order and every case of record in the store: "
                "orders placed in the window are the denominator; "
                "cases requested in the window are the numerator. "
                "The orders table is the order population — no "
                "other order population exists in this system, and "
                "none is invented. The eligible/denied split counts "
                "only cases in the window carrying a decision of "
                "record (seeded closed history carries none, and "
                "says so by its absence from the split)."
            ),
            window_rule=(
                "An explicit [window_start, window_end] pair, both "
                "bounds inclusive: orders by placed_on, cases by "
                "requested_on."
            ),
            measures=[
                "return_rate",
                "return_rate_percent",
                "orders_placed_in_window",
                "returns_requested_in_window",
                "returns_decided_in_window",
                "returns_eligible_in_window",
                "returns_denied_in_window",
            ],
        ),
        ReportDefinition(
            kind=KIND_DISPOSITION_MIX,
            title="Disposition mix",
            population_rule=(
                "Cases requested in the window whose current "
                "(chain-head) disposition decision of record is "
                "decided. Cases whose head decision is "
                "not_decidable, and cases carrying no disposition "
                "at all (the ineligible), are counted in the "
                "population — named, outside the mix. Shares are "
                "of the decided population and sum to it exactly."
            ),
            window_rule=(
                "An explicit [window_start, window_end] pair, both "
                "bounds inclusive, applied to the case's "
                "requested_on — a disposition decision carries no "
                "date of its own, so the return's period is the "
                "period the mix speaks for."
            ),
            measures=[
                "decided_cases",
                "mix_total_share",
                "per-disposition cases and share (breakdown)",
            ],
        ),
        ReportDefinition(
            kind=KIND_FRAUD_OUTCOMES,
            title="Fraud precision and recall on known outcomes",
            population_rule=(
                "Cases with a current (chain-head) fraud score of "
                "record on or before the as-of. A case's outcome "
                "is KNOWN when the latest fraud-review decision of "
                "record adjudicates it (confirmed_fraud = actual "
                "positive; cleared_with_reason = actual negative), "
                "or — no decision standing — when a grading report "
                "of record establishes misrepresentation on its "
                "own: graded condition at least the grading pack's "
                "wildly-worse rank jump below the declared "
                "condition, with wear evidence. Every other scored "
                "case is EXCLUDED and counted as excluded — never "
                "treated as a negative. The prediction is the "
                "current score's band: FRAUD_REVIEW is flagged."
            ),
            window_rule=(
                "Cumulative through the as-of: every scored case "
                "of record whose score stands on or before it."
            ),
            measures=[
                "precision",
                "recall",
                "true_positives",
                "false_positives",
                "false_negatives",
                "true_negatives",
                "known_outcome_cases",
                "excluded_unknown_outcome",
            ],
        ),
        ReportDefinition(
            kind=KIND_MONEY_SUMMARY,
            title="Money summary",
            population_rule=(
                "Every receipt and every adjustment of record "
                "through the as-of. Executed refunds and executed "
                "top-ups are the receipt sums, split by the "
                "adjustments' top-up receipt links — the same "
                "split, over the same artefacts, that the "
                "operations console's money view sums (R13). "
                "Outstanding positions are the open adjustments, "
                "by kind."
            ),
            window_rule=(
                "Cumulative through the as-of: receipts by "
                "executed_on, adjustments by created_on."
            ),
            measures=[
                "executed_refunds",
                "executed_top_ups",
                "recoveries_outstanding",
                "top_ups_outstanding",
            ],
        ),
        ReportDefinition(
            kind=KIND_COST_SUMMARY,
            title="Model cost summary",
            population_rule=(
                "Every cost-ledger entry of record (R12), grouped "
                "by profile. Costs are the entries' own computed "
                "figures; the offline profile's rate card is zero, "
                "and its total is stated as the $0.00 it is."
            ),
            window_rule=(
                "Cumulative through the as-of. Cost entries carry "
                "no timestamps, so no honest period cut exists; "
                "the report covers the whole ledger of record and "
                "says so."
            ),
            measures=[
                "total_cost",
                "total_tokens_in",
                "total_tokens_out",
                "per-profile entries, tokens and cost (breakdown)",
            ],
        ),
    ],
)


def load_report_pack(version: str | None = None) -> ReportPack:
    """The loader, fail-closed like every pack loader in this
    blueprint: an unknown version is an error, never a silent
    fall back to v1."""
    if version is None or version == REPORT_PACK_V1.version:
        return REPORT_PACK_V1
    raise ValueError(f"unknown report pack version: {version!r}")


class ManagementReport(BaseModel):
    """One computed report: an insert-only artefact.

    `figures` are strings throughout — Decimals and ratios in
    their stated formats, counts as integers in `population` — so
    the stored JSON is the figure of record, byte for byte.
    `input_fingerprint` hashes the sorted ids of the artefacts the
    figures derive from; `report_id` is content-addressed over
    kind, window, as-of and that fingerprint, so identical inputs
    mint the identical report and a moved input mints a new one
    linked by `supersedes`.
    """

    report_id: str
    kind: str
    pack_version: str
    window_start: date | None = None
    window_end: date | None = None
    as_of: date
    population: dict[str, int] = {}
    figures: dict[str, str] = {}
    breakdown: list[dict[str, str]] = []
    input_fingerprint: str
    supersedes: str | None = None


class ReportError(RuntimeError):
    """A report that cannot be computed as asked — an unknown
    kind, a windowed kind without its window, a window whose end
    precedes its start. Named, never guessed around."""


# ---------------------------------------------------------------------------
# Computation. Each builder reads the service's artefacts of record
# and returns (population, figures, breakdown, input artefact ids).
# The builders take the service's PUBLIC list reads only — stored
# artefacts, never the on-demand paths that mint missing ones.
# ---------------------------------------------------------------------------


def _in_window(day: date, start: date, end: date) -> bool:
    return start <= day <= end


def _build_return_rate(service, window_start: date, window_end: date, as_of: date):
    orders = [
        o for o in service.list_orders() if _in_window(o.placed_on, window_start, window_end)
    ]
    cases = [
        c
        for c in service.list_cases()
        if _in_window(c.request.requested_on, window_start, window_end)
    ]
    decided = [c for c in cases if c.decision is not None]
    eligible = [c for c in decided if c.decision.eligible]
    population = {
        "orders_placed_in_window": len(orders),
        "returns_requested_in_window": len(cases),
        "returns_decided_in_window": len(decided),
        "returns_eligible_in_window": len(eligible),
        "returns_denied_in_window": len(decided) - len(eligible),
        "orders_on_record": len(service.list_orders()),
        "cases_on_record": len(service.list_cases()),
    }
    rate = _ratio(len(cases), len(orders))
    figures = {
        "return_rate": rate,
        "return_rate_percent": (
            NOT_COMPUTABLE
            if rate == NOT_COMPUTABLE
            else format(Decimal(rate) * 100, ".2f")
        ),
    }
    inputs = sorted([o.order_id for o in orders] + [c.case_id for c in cases])
    return population, figures, [], inputs


def _head_by_case(artefacts, id_field: str, case_field: str = "case_id"):
    """Chain heads: the artefact of each case that no other
    artefact in the list names as its `supersedes`."""
    superseded = {a.supersedes for a in artefacts if a.supersedes}
    heads: dict[str, Any] = {}
    for artefact in artefacts:
        if getattr(artefact, id_field) in superseded:
            continue
        heads[getattr(artefact, case_field)] = artefact
    return heads


def _build_disposition_mix(service, window_start: date, window_end: date, as_of: date):
    cases = [
        c
        for c in service.list_cases()
        if _in_window(c.request.requested_on, window_start, window_end)
    ]
    heads = _head_by_case(service.list_dispositions(), "disposition_id")
    decided: dict[str, int] = {}
    not_decidable = 0
    without = 0
    input_ids: list[str] = []
    for case in cases:
        head = heads.get(case.case_id)
        if head is None:
            without += 1
            continue
        input_ids.append(head.disposition_id)
        if head.status != "decided" or head.disposition is None:
            not_decidable += 1
            continue
        decided[head.disposition] = decided.get(head.disposition, 0) + 1
    total = sum(decided.values())
    population = {
        "cases_in_window": len(cases),
        "decided_cases": total,
        "not_decidable_cases": not_decidable,
        "cases_without_disposition": without,
    }
    breakdown = [
        {
            "disposition": disposition,
            "cases": str(count),
            "share": _ratio(count, total),
        }
        for disposition, count in sorted(decided.items())
    ]
    figures = {"mix_total_share": _ratio(total, total) if total else NOT_COMPUTABLE}
    return population, figures, breakdown, sorted(input_ids)


def _fraud_truth(service, case_id: str) -> tuple[str, str] | None:
    """The case's known outcome, by the pack's truth rule:
    (outcome, source) with outcome positive|negative, or None
    when nobody has established one. A review decision of record
    outranks grading evidence — a human adjudicated; the latest
    decision stands. Grading misrepresentation is the R7 trigger:
    rank jump at least WILDLY_WORSE_JUMP with wear evidence."""
    reviews = service.list_fraud_reviews(case_id)
    if reviews:
        latest = reviews[-1]
        if latest.outcome == "confirmed_fraud":
            return ("positive", "review_decision")
        return ("negative", "review_decision")
    for report in service.list_grading_reports(case_id):
        declared = CONDITION_RANKS.get(report.declared_condition)
        graded = CONDITION_RANKS.get(report.graded_condition)
        if declared is None or graded is None:
            continue
        if graded - declared >= WILDLY_WORSE_JUMP and report.wear_evidence:
            return ("positive", "grading_misrepresentation")
    return None


def _build_fraud_outcomes(service, window_start, window_end, as_of: date):
    heads = _head_by_case(service.list_fraud_scores(), "score_id")
    tp = fp = fn = tn = 0
    known = 0
    excluded = 0
    sources: dict[str, dict[str, int]] = {}
    input_ids: list[str] = []
    scored = 0
    for case_id, score in sorted(heads.items()):
        if score.as_of > as_of:
            continue
        scored += 1
        input_ids.append(score.score_id)
        truth = _fraud_truth(service, case_id)
        if truth is None:
            excluded += 1
            continue
        known += 1
        outcome, source = truth
        bucket = sources.setdefault(source, {"cases": 0, "positives": 0, "negatives": 0})
        bucket["cases"] += 1
        bucket["positives" if outcome == "positive" else "negatives"] += 1
        flagged = score.band == BAND_FRAUD_REVIEW
        if flagged and outcome == "positive":
            tp += 1
        elif flagged:
            fp += 1
        elif outcome == "positive":
            fn += 1
        else:
            tn += 1
    population = {
        "scored_cases": scored,
        "known_outcome_cases": known,
        "excluded_unknown_outcome": excluded,
        "flagged_cases": tp + fp,
    }
    figures = {
        "true_positives": str(tp),
        "false_positives": str(fp),
        "false_negatives": str(fn),
        "true_negatives": str(tn),
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
    }
    breakdown = [
        {
            "source": source,
            "cases": str(counts["cases"]),
            "positives": str(counts["positives"]),
            "negatives": str(counts["negatives"]),
        }
        for source, counts in sorted(sources.items())
    ]
    return population, figures, breakdown, sorted(input_ids)


def money_totals(service, as_of: date | None = None) -> dict[str, Decimal]:
    """The money view's four totals, from the artefacts of
    record: executed refunds, executed top-ups (the receipts the
    adjustments name as their top-up settlements), outstanding
    recoveries and outstanding top-ups (the open adjustments, by
    kind). This is the console money view's arithmetic — the
    console sums these same artefacts in its payload rows, and
    the eval gate grades the report against those rows so the two
    surfaces cannot fork."""
    receipts = service.list_receipts()
    adjustments = service.list_all_adjustments()
    if as_of is not None:
        receipts = [r for r in receipts if r.executed_on <= as_of]
        adjustments = [a for a in adjustments if a.created_on <= as_of]
    top_up_receipt_ids = {a.top_up_receipt_id for a in adjustments if a.top_up_receipt_id}
    return {
        "executed_refunds": sum(
            (r.amount for r in receipts if r.receipt_id not in top_up_receipt_ids),
            Decimal("0.00"),
        ),
        "executed_top_ups": sum(
            (r.amount for r in receipts if r.receipt_id in top_up_receipt_ids),
            Decimal("0.00"),
        ),
        "recoveries_outstanding": sum(
            (a.amount for a in adjustments if a.kind == "recovery_due" and a.status == "open"),
            Decimal("0.00"),
        ),
        "top_ups_outstanding": sum(
            (a.amount for a in adjustments if a.kind == "top_up_due" and a.status == "open"),
            Decimal("0.00"),
        ),
    }


def _build_money_summary(service, window_start, window_end, as_of: date):
    totals = money_totals(service, as_of)
    receipts = [r for r in service.list_receipts() if r.executed_on <= as_of]
    adjustments = [a for a in service.list_all_adjustments() if a.created_on <= as_of]
    top_up_receipt_ids = {a.top_up_receipt_id for a in adjustments if a.top_up_receipt_id}
    population = {
        "receipts": len(receipts),
        "refund_receipts": len(
            [r for r in receipts if r.receipt_id not in top_up_receipt_ids]
        ),
        "top_up_receipts": len([r for r in receipts if r.receipt_id in top_up_receipt_ids]),
        "adjustments": len(adjustments),
        "open_recoveries": len(
            [a for a in adjustments if a.kind == "recovery_due" and a.status == "open"]
        ),
        "open_top_ups": len(
            [a for a in adjustments if a.kind == "top_up_due" and a.status == "open"]
        ),
    }
    figures = {name: _money_str(value) for name, value in totals.items()}
    inputs = sorted([r.receipt_id for r in receipts] + [a.adjustment_id for a in adjustments])
    return population, figures, [], inputs


def _build_cost_summary(service, window_start, window_end, as_of: date):
    entries = service.list_all_cost_entries()
    total_cost = sum((e.cost for e in entries), Decimal("0.00"))
    population = {
        "entries": len(entries),
        "entries_ok": len([e for e in entries if e.status == "ok"]),
        "entries_not_ok": len([e for e in entries if e.status != "ok"]),
        "cases_with_entries": len({e.case_id for e in entries}),
    }
    figures = {
        "total_cost": _money_str(total_cost),
        "total_tokens_in": str(sum(e.tokens_in for e in entries)),
        "total_tokens_out": str(sum(e.tokens_out for e in entries)),
    }
    by_profile: dict[str, list] = {}
    for entry in entries:
        by_profile.setdefault(entry.profile, []).append(entry)
    breakdown = [
        {
            "profile": profile,
            "entries": str(len(rows)),
            "tokens_in": str(sum(e.tokens_in for e in rows)),
            "tokens_out": str(sum(e.tokens_out for e in rows)),
            "total_cost": _money_str(sum((e.cost for e in rows), Decimal("0.00"))),
        }
        for profile, rows in sorted(by_profile.items())
    ]
    inputs = sorted(e.entry_id for e in entries)
    return population, figures, breakdown, inputs


_BUILDERS = {
    KIND_RETURN_RATE: _build_return_rate,
    KIND_DISPOSITION_MIX: _build_disposition_mix,
    KIND_FRAUD_OUTCOMES: _build_fraud_outcomes,
    KIND_MONEY_SUMMARY: _build_money_summary,
    KIND_COST_SUMMARY: _build_cost_summary,
}


def _fingerprint(kind: str, window_start, window_end, as_of: date, input_ids: list[str]) -> str:
    blob = json.dumps(
        {
            "kind": kind,
            "pack": REPORT_PACK_VERSION,
            "window_start": window_start.isoformat() if window_start else None,
            "window_end": window_end.isoformat() if window_end else None,
            "as_of": as_of.isoformat(),
            "inputs": input_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def build_report(
    service,
    kind: str,
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    as_of: date,
) -> ManagementReport:
    """Compute a report from the artefacts of record — computed,
    not yet stored, and carrying no supersedes link (the service
    owns storage and supersession)."""
    pack = load_report_pack()
    try:
        pack.definition(kind)
    except ValueError as exc:
        raise ReportError(str(exc)) from exc
    if kind in WINDOWED_KINDS:
        if window_start is None or window_end is None:
            raise ReportError(
                f"report {kind} requires its window (window_start and window_end)"
            )
        if window_end < window_start:
            raise ReportError(
                f"report {kind} window ends ({window_end}) before it starts ({window_start})"
            )
    population, figures, breakdown, input_ids = _BUILDERS[kind](
        service, window_start, window_end, as_of
    )
    fingerprint = _fingerprint(kind, window_start, window_end, as_of, input_ids)
    report_id = "RPT-" + fingerprint[:16]
    return ManagementReport(
        report_id=report_id,
        kind=kind,
        pack_version=pack.version,
        window_start=window_start if kind in WINDOWED_KINDS else None,
        window_end=window_end if kind in WINDOWED_KINDS else None,
        as_of=as_of,
        population=population,
        figures=figures,
        breakdown=breakdown,
        input_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Verification: an independent re-performance. The verifier does
# NOT call the builders — it re-aggregates from the raw stored
# payloads (the store's own rows, parsed here) by separately
# written code, and compares every stated figure. Verification
# speaks for the record as it stands NOW: a report whose inputs
# have since been superseded no longer re-performs, and the
# verifier says so — that drift is exactly why the recomputation
# superseded it, and the successor is the report that verifies.
# ---------------------------------------------------------------------------


def _raw_rows(store, method: str) -> list[dict]:
    return list(getattr(store, method)())


def verify_report(service, report: ManagementReport) -> list[str]:
    """Re-perform one report from the stored record and return
    its problems ([] when the report is exactly reproducible
    against the record as it stands). Checks: the pack version
    is the current pack's; the report is stored, unaltered; its
    population, figures and breakdown equal an independent
    re-aggregation over the raw stored payloads; and its
    fingerprint recomputes from the input ids that
    re-aggregation actually consumed."""
    problems: list[str] = []
    pack = load_report_pack()
    if report.pack_version != pack.version:
        problems.append(
            f"report priced under pack {report.pack_version}, current pack is {pack.version}"
        )
        return problems
    stored = service.get_report(report.report_id)
    if stored is None:
        problems.append(f"report {report.report_id} is not stored")
        return problems
    if stored.model_dump(mode="json") != report.model_dump(mode="json"):
        problems.append("the stored report differs from the artefact presented")
        return problems

    store = service._store
    start, end, as_of = report.window_start, report.window_end, report.as_of

    if report.kind == KIND_RETURN_RATE:
        orders = _raw_rows(store, "list_orders")
        cases = _raw_rows(store, "list_cases")
        in_orders = [
            o for o in orders if start.isoformat() <= o["placed_on"] <= end.isoformat()
        ]
        in_cases = [
            c
            for c in cases
            if start.isoformat() <= c["request"]["requested_on"] <= end.isoformat()
        ]
        decided = [c for c in in_cases if c.get("decision")]
        eligible = [c for c in decided if c["decision"]["eligible"]]
        expected_population = {
            "orders_placed_in_window": len(in_orders),
            "returns_requested_in_window": len(in_cases),
            "returns_decided_in_window": len(decided),
            "returns_eligible_in_window": len(eligible),
            "returns_denied_in_window": len(decided) - len(eligible),
            "orders_on_record": len(orders),
            "cases_on_record": len(cases),
        }
        rate = _ratio(len(in_cases), len(in_orders))
        expected_figures = {
            "return_rate": rate,
            "return_rate_percent": (
                NOT_COMPUTABLE if rate == NOT_COMPUTABLE else format(Decimal(rate) * 100, ".2f")
            ),
        }
        expected_breakdown: list[dict] = []
        input_ids = sorted([o["order_id"] for o in in_orders] + [c["case_id"] for c in in_cases])

    elif report.kind == KIND_DISPOSITION_MIX:
        cases = _raw_rows(store, "list_cases")
        dispositions = _raw_rows(store, "list_dispositions")
        superseded = {d.get("supersedes") for d in dispositions if d.get("supersedes")}
        heads = {
            d["case_id"]: d
            for d in dispositions
            if d["disposition_id"] not in superseded
        }
        in_cases = [
            c
            for c in cases
            if start.isoformat() <= c["request"]["requested_on"] <= end.isoformat()
        ]
        counts: dict[str, int] = {}
        not_decidable = without = 0
        input_ids = []
        for case in in_cases:
            head = heads.get(case["case_id"])
            if head is None:
                without += 1
                continue
            input_ids.append(head["disposition_id"])
            if head["status"] != "decided" or head.get("disposition") is None:
                not_decidable += 1
                continue
            counts[head["disposition"]] = counts.get(head["disposition"], 0) + 1
        total = sum(counts.values())
        expected_population = {
            "cases_in_window": len(in_cases),
            "decided_cases": total,
            "not_decidable_cases": not_decidable,
            "cases_without_disposition": without,
        }
        expected_breakdown = [
            {"disposition": name, "cases": str(n), "share": _ratio(n, total)}
            for name, n in sorted(counts.items())
        ]
        expected_figures = {
            "mix_total_share": _ratio(total, total) if total else NOT_COMPUTABLE
        }
        input_ids = sorted(input_ids)

    elif report.kind == KIND_FRAUD_OUTCOMES:
        scores = _raw_rows(store, "list_all_fraud_scores")
        superseded = {s.get("supersedes") for s in scores if s.get("supersedes")}
        heads = {
            s["case_id"]: s for s in scores if s["score_id"] not in superseded
        }
        tp = fp = fn = tn = known = excluded = scored = 0
        sources: dict[str, dict[str, int]] = {}
        input_ids = []
        for case_id, score in sorted(heads.items()):
            if score["as_of"] > as_of.isoformat():
                continue
            scored += 1
            input_ids.append(score["score_id"])
            truth = _truth_from_rows(store, case_id)
            if truth is None:
                excluded += 1
                continue
            known += 1
            outcome, source = truth
            bucket = sources.setdefault(source, {"cases": 0, "positives": 0, "negatives": 0})
            bucket["cases"] += 1
            bucket["positives" if outcome == "positive" else "negatives"] += 1
            flagged = score["band"] == BAND_FRAUD_REVIEW
            if flagged and outcome == "positive":
                tp += 1
            elif flagged:
                fp += 1
            elif outcome == "positive":
                fn += 1
            else:
                tn += 1
        expected_population = {
            "scored_cases": scored,
            "known_outcome_cases": known,
            "excluded_unknown_outcome": excluded,
            "flagged_cases": tp + fp,
        }
        expected_figures = {
            "true_positives": str(tp),
            "false_positives": str(fp),
            "false_negatives": str(fn),
            "true_negatives": str(tn),
            "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn),
        }
        expected_breakdown = [
            {
                "source": source,
                "cases": str(c["cases"]),
                "positives": str(c["positives"]),
                "negatives": str(c["negatives"]),
            }
            for source, c in sorted(sources.items())
        ]
        input_ids = sorted(input_ids)

    elif report.kind == KIND_MONEY_SUMMARY:
        receipts = [
            r for r in _raw_rows(store, "list_receipts") if r["executed_on"] <= as_of.isoformat()
        ]
        all_adjustments = [
            row
            for case in _raw_rows(store, "list_cases")
            for row in store.list_adjustments(case["case_id"])
        ]
        adjustments = [
            a for a in all_adjustments if a["created_on"] <= as_of.isoformat()
        ]
        top_up_ids = {a.get("top_up_receipt_id") for a in adjustments if a.get("top_up_receipt_id")}
        zero = Decimal("0.00")
        totals = {
            "executed_refunds": sum(
                (Decimal(r["amount"]) for r in receipts if r["receipt_id"] not in top_up_ids), zero
            ),
            "executed_top_ups": sum(
                (Decimal(r["amount"]) for r in receipts if r["receipt_id"] in top_up_ids), zero
            ),
            "recoveries_outstanding": sum(
                (
                    Decimal(a["amount"])
                    for a in adjustments
                    if a["kind"] == "recovery_due" and a["status"] == "open"
                ),
                zero,
            ),
            "top_ups_outstanding": sum(
                (
                    Decimal(a["amount"])
                    for a in adjustments
                    if a["kind"] == "top_up_due" and a["status"] == "open"
                ),
                zero,
            ),
        }
        expected_figures = {name: _money_str(value) for name, value in totals.items()}
        expected_population = {
            "receipts": len(receipts),
            "refund_receipts": len([r for r in receipts if r["receipt_id"] not in top_up_ids]),
            "top_up_receipts": len([r for r in receipts if r["receipt_id"] in top_up_ids]),
            "adjustments": len(adjustments),
            "open_recoveries": len(
                [a for a in adjustments if a["kind"] == "recovery_due" and a["status"] == "open"]
            ),
            "open_top_ups": len(
                [a for a in adjustments if a["kind"] == "top_up_due" and a["status"] == "open"]
            ),
        }
        expected_breakdown = []
        input_ids = sorted([r["receipt_id"] for r in receipts] + [a["adjustment_id"] for a in adjustments])

    elif report.kind == KIND_COST_SUMMARY:
        entries = _raw_rows(store, "list_all_cost_entries")
        expected_population = {
            "entries": len(entries),
            "entries_ok": len([e for e in entries if e["status"] == "ok"]),
            "entries_not_ok": len([e for e in entries if e["status"] != "ok"]),
            "cases_with_entries": len({e["case_id"] for e in entries}),
        }
        expected_figures = {
            "total_cost": _money_str(sum((Decimal(e["cost"]) for e in entries), Decimal("0.00"))),
            "total_tokens_in": str(sum(e["tokens_in"] for e in entries)),
            "total_tokens_out": str(sum(e["tokens_out"] for e in entries)),
        }
        by_profile: dict[str, list] = {}
        for entry in entries:
            by_profile.setdefault(entry["profile"], []).append(entry)
        expected_breakdown = [
            {
                "profile": profile,
                "entries": str(len(rows)),
                "tokens_in": str(sum(e["tokens_in"] for e in rows)),
                "tokens_out": str(sum(e["tokens_out"] for e in rows)),
                "total_cost": _money_str(
                    sum((Decimal(e["cost"]) for e in rows), Decimal("0.00"))
                ),
            }
            for profile, rows in sorted(by_profile.items())
        ]
        input_ids = sorted(e["entry_id"] for e in entries)

    else:
        return [f"unknown report kind: {report.kind!r}"]

    if report.population != expected_population:
        problems.append(
            f"population differs: report states {report.population}, "
            f"re-performance derives {expected_population}"
        )
    if report.figures != expected_figures:
        problems.append(
            f"figures differ: report states {report.figures}, "
            f"re-performance derives {expected_figures}"
        )
    if report.breakdown != expected_breakdown:
        problems.append("breakdown differs from re-performance")
    expected_fingerprint = _fingerprint(
        report.kind, start, end, as_of, input_ids
    )
    if report.input_fingerprint != expected_fingerprint:
        problems.append("input fingerprint does not recompute from the inputs of record")
    return problems


def _truth_from_rows(store, case_id: str) -> tuple[str, str] | None:
    """The pack's truth rule over raw stored payloads — the
    verifier's own reading of the record (review decisions first,
    then grading misrepresentation), written separately from the
    compute path's model-level reading."""
    reviews = store.list_fraud_reviews(case_id)
    if reviews:
        latest = reviews[-1]
        if latest["outcome"] == "confirmed_fraud":
            return ("positive", "review_decision")
        return ("negative", "review_decision")
    for report in store.list_grading_reports(case_id):
        declared = CONDITION_RANKS.get(report["declared_condition"])
        graded = CONDITION_RANKS.get(report["graded_condition"])
        if declared is None or graded is None:
            continue
        if graded - declared >= WILDLY_WORSE_JUMP and report.get("wear_evidence"):
            return ("positive", "grading_misrepresentation")
    return None
