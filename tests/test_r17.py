"""R17 gates: management reporting over planted truth.

Every expectation here was hand-derived from the planted record
before the code path ran:

- the seeded store holds 19 orders and, after both feeds, 19
  cases; October 2026 holds 7 orders placed and 13 returns
  requested, 10 of them decided, 5 eligible and 5 denied — the
  return rate is 13/7 = 1.8571, and the denominator is the
  orders table because that is the order population that exists;
- the October disposition mix decides exactly 5 cases (RESTOCK
  x3, REFURBISH x1, HOLD_FOR_INSPECTION x1) and its shares sum
  to the decided population exactly;
- fraud truth is planted two ways: CASE-3008's review confirms
  the flag that fired (a true positive), and CASE-2001's grading
  (declared new_with_tags, graded used, wear evidence, 100 g
  against an 820 g catalogue weight) proves misrepresentation
  the score never flagged (a false negative). Precision 1.0000,
  recall 0.5000, over a known-outcome population of exactly 2 —
  the other 8 scored cases are excluded, and true negatives are
  exactly 0: excluded is never a negative;
- the money walk is the console check's own: CASE-2003 executed
  at $130.50 and graded down to a $14.50 open recovery; CASE-2006
  executed at $29.05 and graded up to a $4.50 top-up, executed.
  The summary must read 159.55 / 4.50 / 14.50 / 0.00 — and equal
  the sums over the console payload's own rows;
- reports are artefacts: recompute over unmoved inputs returns
  the standing report; a moved input mints a successor linked by
  supersedes, and the superseded report stops re-performing —
  the drift the verifier names is why it was superseded;
- a customer principal cannot read a report at all: the API's
  response for a real report id is byte-for-byte the response
  for an invented one.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.access import AccessDeniedError, Principal, load_access_pack
from returns_resolution.api import create_app
from returns_resolution.console import build_console_data
from returns_resolution.intake import FRAUD_FEED_REQUESTS, ReferenceFeed
from returns_resolution.modelport import OfflineComposer
from returns_resolution.reporting import ReportError, load_report_pack
from returns_resolution.runtime_app import handle_payload
from returns_resolution.service import CaseService
from returns_resolution.store import MIGRATIONS, InMemoryStore

OCT = {"window_start": date(2026, 10, 1), "window_end": date(2026, 10, 31)}
AS_OF = date(2026, 11, 5)
DAY = date(2026, 10, 27)

SUPERVISOR = Principal(id="s.super", role="staff_supervisor")
AGENT = Principal(id="j.agent", role="staff_agent")
AUDITOR = Principal(id="a.auditor", role="auditor")


def _planted(store=None) -> CaseService:
    service = CaseService(store or InMemoryStore(), OfflineComposer())
    service.seed_orders()
    service.seed_history()
    for request in list(ReferenceFeed().fetch_requests()) + list(FRAUD_FEED_REQUESTS):
        service.ingest(request)
    return service


def _grade(service: CaseService, case_id: str, graded: str, **kwargs) -> None:
    service.invoke_tool(
        "create_return_label",
        {"case_id": case_id, "issued_on": DAY},
        case_id=case_id,
        called_on=DAY,
    )
    service.record_warehouse_receipt(
        case_id, received_by="w.chen", received_on=date(2026, 10, 30)
    )
    service.record_grading(
        case_id, grader="p.novak", graded_condition=graded,
        graded_on=date(2026, 10, 31), **kwargs,
    )


def _confirm_serial(service: CaseService) -> None:
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez", outcome="confirmed_fraud",
        reason_code="pattern_verified",
    )


def _fraud_fixture() -> CaseService:
    service = _planted()
    _confirm_serial(service)
    _grade(service, "CASE-2001", "used", wear_evidence=True, received_weight_grams=100)
    return service


def _money_fixture() -> CaseService:
    service = _planted()
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY)
    service.execute_refund("CASE-2003", idempotency_key="r17-2003", as_of=DAY)
    _grade(service, "CASE-2003", "used")
    service.record_approval("CASE-2006", approver="d.okafor", role="returns_lead", as_of=DAY)
    service.execute_refund("CASE-2006", idempotency_key="r17-2006", as_of=DAY)
    _grade(
        service, "CASE-2006", "like_new",
        functional_test_result="pass", data_wipe_status="verified",
    )
    service.execute_top_up("CASE-2006", idempotency_key="r17-2006-topup", as_of=date(2026, 11, 1))
    return service


def _all_reports(service: CaseService) -> dict:
    return {
        "return_rate": service.compute_report("return_rate", as_of=AS_OF, **OCT),
        "disposition_mix": service.compute_report("disposition_mix", as_of=AS_OF, **OCT),
        "fraud_outcomes": service.compute_report("fraud_outcomes", as_of=AS_OF),
        "money_summary": service.compute_report("money_summary", as_of=AS_OF),
        "cost_summary": service.compute_report("cost_summary", as_of=AS_OF),
    }


# -- the pack ---------------------------------------------------------------

def test_report_pack_is_versioned_data():
    pack = load_report_pack()
    assert pack.version == "2026.10.1"
    assert [d.kind for d in pack.definitions] == [
        "return_rate",
        "disposition_mix",
        "fraud_outcomes",
        "money_summary",
        "cost_summary",
    ]
    for definition in pack.definitions:
        assert definition.population_rule and definition.window_rule and definition.measures
    with pytest.raises(ValueError):
        load_report_pack("1999.01.1")
    service = _planted()
    report = service.compute_report("return_rate", as_of=AS_OF, **OCT)
    assert report.pack_version == "2026.10.1"


def test_report_requests_are_validated():
    service = _planted()
    with pytest.raises(ReportError):
        service.compute_report("nonsense", as_of=AS_OF)
    with pytest.raises(ReportError):  # a windowed kind without its window
        service.compute_report("return_rate", as_of=AS_OF)
    with pytest.raises(ReportError):  # a window that ends before it starts
        service.compute_report(
            "return_rate", as_of=AS_OF,
            window_start=date(2026, 10, 31), window_end=date(2026, 10, 1),
        )


# -- return rate --------------------------------------------------------------

def test_return_rate_hand_counts():
    report = _planted().compute_report("return_rate", as_of=AS_OF, **OCT)
    assert report.population == {
        "orders_placed_in_window": 7,
        "returns_requested_in_window": 13,
        "returns_decided_in_window": 10,
        "returns_eligible_in_window": 5,
        "returns_denied_in_window": 5,
        "orders_on_record": 19,
        "cases_on_record": 19,
    }
    assert report.figures == {"return_rate": "1.8571", "return_rate_percent": "185.71"}


def test_return_rate_over_an_empty_window_is_not_computable_not_zero():
    report = _planted().compute_report(
        "return_rate", as_of=AS_OF,
        window_start=date(2025, 1, 1), window_end=date(2025, 1, 31),
    )
    assert report.population["orders_placed_in_window"] == 0
    assert report.figures["return_rate"] == "not_computable"
    assert report.figures["return_rate_percent"] == "not_computable"


# -- disposition mix ----------------------------------------------------------

def test_disposition_mix_sums_to_its_population_exactly():
    report = _planted().compute_report("disposition_mix", as_of=AS_OF, **OCT)
    assert report.population == {
        "cases_in_window": 13,
        "decided_cases": 5,
        "not_decidable_cases": 0,
        "cases_without_disposition": 8,
    }
    assert {row["disposition"]: row["cases"] for row in report.breakdown} == {
        "HOLD_FOR_INSPECTION": "1",
        "REFURBISH": "1",
        "RESTOCK": "3",
    }
    assert sum(int(row["cases"]) for row in report.breakdown) == 5
    assert sum(Decimal(row["share"]) for row in report.breakdown) == Decimal("1.0000")
    assert report.figures["mix_total_share"] == "1.0000"


# -- fraud precision / recall on planted truth --------------------------------

def test_fraud_precision_recall_over_known_outcomes_only():
    report = _fraud_fixture().compute_report("fraud_outcomes", as_of=AS_OF)
    assert report.population == {
        "scored_cases": 10,
        "known_outcome_cases": 2,
        "excluded_unknown_outcome": 8,
        "flagged_cases": 1,
    }
    assert report.figures == {
        "true_positives": "1",
        "false_positives": "0",
        "false_negatives": "1",
        "true_negatives": "0",  # excluded cases are NOT negatives
        "precision": "1.0000",
        "recall": "0.5000",
    }
    assert {row["source"]: row for row in report.breakdown} == {
        "review_decision": {"source": "review_decision", "cases": "1", "positives": "1", "negatives": "0"},
        "grading_misrepresentation": {"source": "grading_misrepresentation", "cases": "1", "positives": "1", "negatives": "0"},
    }


def test_fraud_cleared_flag_is_a_false_positive():
    service = _planted()
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez", outcome="cleared_with_reason",
        reason_code="legitimate_pattern_explained",
    )
    report = service.compute_report("fraud_outcomes", as_of=AS_OF)
    assert report.population["known_outcome_cases"] == 1
    assert report.figures["false_positives"] == "1"
    assert report.figures["precision"] == "0.0000"
    # No known positive exists, so recall has no denominator:
    # stated as not computable, never as a flattering zero or one.
    assert report.figures["recall"] == "not_computable"


def test_fraud_with_no_known_outcomes_states_not_computable():
    report = _planted().compute_report("fraud_outcomes", as_of=AS_OF)
    assert report.population["known_outcome_cases"] == 0
    assert report.population["excluded_unknown_outcome"] == 10
    assert report.figures["precision"] == "not_computable"
    assert report.figures["recall"] == "not_computable"


# -- money and cost -----------------------------------------------------------

def test_money_summary_matches_the_console_arithmetic():
    service = _money_fixture()
    report = service.compute_report("money_summary", as_of=AS_OF)
    assert report.figures == {
        "executed_refunds": "159.55",
        "executed_top_ups": "4.50",
        "recoveries_outstanding": "14.50",
        "top_ups_outstanding": "0.00",
        # R18's fifth line: no claims stand on this fixture, and
        # the four customer totals above are exactly R17's.
        "carrier_claims_settled": "0.00",
    }
    rows = build_console_data(service)["money"]
    top_up_ids = {a["top_up_receipt_id"] for a in rows["adjustments"] if a["top_up_receipt_id"]}
    console = {
        "executed_refunds": sum(
            Decimal(r["amount"]) for r in rows["receipts"] if r["receipt_id"] not in top_up_ids
        ),
        "executed_top_ups": sum(
            Decimal(r["amount"]) for r in rows["receipts"] if r["receipt_id"] in top_up_ids
        ),
        "recoveries_outstanding": sum(
            Decimal(a["amount"]) for a in rows["adjustments"]
            if a["kind"] == "recovery_due" and a["status"] == "open"
        ),
        "top_ups_outstanding": sum(
            Decimal(a["amount"]) for a in rows["adjustments"]
            if a["kind"] == "top_up_due" and a["status"] == "open"
        ),
    }
    for name, total in console.items():
        assert report.figures[name] == format(total, ".2f")


def test_cost_summary_states_the_offline_zero():
    service = _planted()
    report = service.compute_report("cost_summary", as_of=AS_OF)
    entries = service.list_all_cost_entries()
    assert report.population["entries"] == len(entries) > 0
    assert report.figures["total_cost"] == "0.00"
    offline = [row for row in report.breakdown if row["profile"] == "offline"]
    assert len(offline) == 1 and offline[0]["total_cost"] == "0.00"
    assert report.figures["total_tokens_in"] == str(sum(e.tokens_in for e in entries))
    assert report.figures["total_tokens_out"] == str(sum(e.tokens_out for e in entries))


# -- artefacts: verification, idempotence, supersession -----------------------

def test_every_report_re_performs():
    service = _money_fixture()
    _confirm_serial(service)
    for report in _all_reports(service).values():
        assert service.verify_management_report(report) == []


def test_tampered_report_is_caught_by_re_performance():
    service = _fraud_fixture()
    report = service.compute_report("fraud_outcomes", as_of=AS_OF)
    forged = report.model_copy(update={"figures": {**report.figures, "precision": "0.9999"}})
    problems = service.verify_management_report(forged)
    assert problems  # the stored artefact differs from the one presented
    shrunk = report.model_copy(
        update={"population": {**report.population, "known_outcome_cases": 10}}
    )
    assert service.verify_management_report(shrunk)


def test_recompute_is_idempotent_and_supersession_is_a_new_artefact():
    service = _fraud_fixture()
    first = service.compute_report("fraud_outcomes", as_of=AS_OF)
    again = service.compute_report("fraud_outcomes", as_of=AS_OF)
    assert again.report_id == first.report_id and again.supersedes is None
    assert len(service.list_reports(kind="fraud_outcomes")) == 1
    # Move an input: a second case's outcome becomes known.
    _grade(service, "CASE-3103", "damaged_by_customer",
           wear_evidence=True, received_weight_grams=100)
    successor = service.compute_report("fraud_outcomes", as_of=AS_OF)
    assert successor.report_id != first.report_id
    assert successor.supersedes == first.report_id
    assert successor.population["known_outcome_cases"] == 3
    assert len(service.list_reports(kind="fraud_outcomes")) == 2  # nothing edited away
    assert service.verify_management_report(successor) == []
    # The superseded report no longer re-performs against the
    # moved record — the drift is named, which is why it was
    # superseded rather than edited.
    assert service.verify_management_report(first) != []


def test_reports_are_insert_only_in_the_store():
    store = InMemoryStore()
    service = _planted(store)
    report = service.compute_report("money_summary", as_of=AS_OF)
    store.put_management_report(report.model_dump(mode="json"))  # replay: ignored
    assert len(store.list_management_reports()) == 1
    assert service.get_report(report.report_id) == report
    assert service.get_report("RPT-0000000000000000") is None


# -- access -------------------------------------------------------------------

def test_access_pack_reports_surface_and_compute_act():
    pack = load_access_pack()
    assert "reports" in pack.grant("staff_supervisor").read_surfaces
    assert "reports" in pack.grant("auditor").read_surfaces
    assert "reports" not in pack.grant("customer").read_surfaces
    assert "reports" not in pack.grant("staff_agent").read_surfaces
    assert pack.required_roles("compute_report") == ["staff_supervisor", "system"]
    assert "compute_report" in pack.grant("staff_supervisor").acts
    assert pack.grant("auditor").acts == []


def test_guarded_report_access():
    service = _fraud_fixture()
    report = service.compute_report("fraud_outcomes", as_of=AS_OF)
    assert service.get_report_as(SUPERVISOR, report.report_id) == report
    assert service.get_report_as(AUDITOR, report.report_id) == report
    with pytest.raises(AccessDeniedError) as agent_read:
        service.get_report_as(AGENT, report.report_id)
    assert agent_read.value.reason == "surface_not_readable"
    customer = Principal(id="CUST-ADA", role="customer")
    with pytest.raises(AccessDeniedError) as hidden:
        service.get_report_as(customer, report.report_id)
    assert hidden.value.reason == "case_not_visible"  # existence-hiding
    with pytest.raises(AccessDeniedError) as hidden_missing:
        service.get_report_as(customer, "RPT-0000000000000000")
    assert hidden_missing.value.reason == "case_not_visible"  # same for a real id
    denials = [d for d in service.list_access_denials() if d.surface == "reports"]
    assert len(denials) == 3  # the agent's named refusal + both customer probes
    assert any(d.principal_id == "j.agent" for d in denials)
    with pytest.raises(AccessDeniedError) as auditor_compute:
        service.compute_report_as(AUDITOR, "money_summary", as_of=AS_OF)
    assert auditor_compute.value.reason == "read_only_role"
    computed = service.compute_report_as(SUPERVISOR, "money_summary", as_of=AS_OF)
    assert computed.kind == "money_summary"


def test_api_report_surfaces_and_customer_hiding():
    service = _fraud_fixture()
    report = service.compute_report("fraud_outcomes", as_of=AS_OF)
    client = TestClient(create_app(service))
    pack = client.get("/reports/pack")
    assert pack.status_code == 200 and pack.json()["version"] == "2026.10.1"
    customer = {"X-Principal-Id": "CUST-ADA", "X-Principal-Role": "customer"}
    supervisor = {"X-Principal-Id": "s.super", "X-Principal-Role": "staff_supervisor"}
    auditor = {"X-Principal-Id": "a.auditor", "X-Principal-Role": "auditor"}
    probed = client.get(f"/reports/{report.report_id}", headers=customer)
    missing = client.get("/reports/RPT-0000000000000000", headers=customer)
    assert probed.status_code == missing.status_code == 404
    assert probed.content == missing.content  # byte-for-byte: existence hidden
    assert client.get("/reports", headers=customer).status_code == 404
    assert client.get(f"/reports/{report.report_id}", headers=supervisor).status_code == 200
    assert client.get(f"/reports/{report.report_id}/verify", headers=auditor).json() == {
        "report_id": report.report_id, "verified": True, "problems": [],
    }
    assert client.get(f"/reports/{report.report_id}").status_code == 200  # trusted-local
    refused = client.post(
        "/reports/compute", headers=auditor,
        json={"kind": "money_summary", "as_of": "2026-11-05"},
    )
    assert refused.status_code == 403
    computed = client.post(
        "/reports/compute", headers=supervisor,
        json={"kind": "return_rate", "window_start": "2026-10-01",
              "window_end": "2026-10-31", "as_of": "2026-11-05"},
    )
    assert computed.status_code == 201
    assert computed.json()["figures"]["return_rate"] == "1.8571"
    bad = client.post(
        "/reports/compute", json={"kind": "return_rate", "as_of": "2026-11-05"}
    )
    assert bad.status_code == 422  # the windowed kind without its window


def test_runtime_report_actions_and_customer_hiding():
    service = _fraud_fixture()
    computed = handle_payload(
        service, {"action": "compute_report", "kind": "fraud_outcomes", "as_of": "2026-11-05"}
    )
    report_id = computed["report"]["report_id"]
    assert computed["report"]["figures"]["precision"] == "1.0000"
    me = {"id": "CUST-ADA", "role": "customer"}
    probed = handle_payload(
        service, {"action": "get_report", "report_id": report_id, "principal": me}
    )
    missing = handle_payload(
        service, {"action": "get_report", "report_id": "RPT-0000000000000000", "principal": me}
    )
    assert probed == missing == {"report": None}
    verified = handle_payload(service, {"action": "verify_report", "report_id": report_id})
    assert verified == {"report_id": report_id, "verified": True, "problems": []}
    refused = handle_payload(
        service,
        {"action": "compute_report", "kind": "money_summary", "as_of": "2026-11-05",
         "principal": {"id": "a.auditor", "role": "auditor"}},
    )
    assert refused["access_reason"] == "read_only_role"


# -- the migration --------------------------------------------------------------

def test_migration_0013_is_in_the_ledger():
    # R18 appended 0014 after this round's migration; 0013 stands
    # in the ledger, no longer the tail.
    assert "0013" in [version for version, _, _ in MIGRATIONS]
    store = InMemoryStore()
    assert "0013" in store.migrate()
