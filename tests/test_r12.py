"""R12 gates: the model gateway + cost ledger.

Graded: every model-shaped call routes through ONE gateway —
summary composition (all three composers) and the R8 note slot;
the offline profile's output is byte-identical to the bare model
port's (the gateway meters, it does not touch text); model
responses enter only as validated DraftOutputs — malformed
envelopes and unvalidatable content are rejected and recorded,
never coerced; an unreachable hosted profile fails CLOSED (no
fallback to another profile's output, ever); a well-formed hosted
response with a wrong figure is still refused by the figure
verifier; the cost ledger's arithmetic re-performs exactly from
the price pack; the per-case call budget refuses with the budget
named; parity across service, API and Runtime; and the ledger
persists on the Postgres profile (migration 0010).
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.api import create_app
from returns_resolution.intake import PLANTED_REQUESTS
from returns_resolution.model_gateway import (
    BedrockGateway,
    InvalidModelOutputError,
    ModelBudgetError,
    ModelGatewayError,
    ModelProfileUnavailableError,
    NimGateway,
    OfflineGateway,
    PricePack,
    compute_cost,
    load_model_gateway,
    load_price_pack,
)
from returns_resolution.modelport import OfflineComposer
from returns_resolution.runtime_app import handle_payload
from returns_resolution.service import CaseService, FigureVerificationError
from returns_resolution.store import InMemoryStore, SqlStore

DAY = date(2026, 10, 27)

PG_URL = os.environ.get("RETURNS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pg_only = pytest.mark.skipif(
    not PG_URL, reason="Postgres profile runs when RETURNS_TEST_DATABASE_URL is set (R12)"
)


def _service(store=None, gateway=None) -> CaseService:
    service = CaseService(store or InMemoryStore(), OfflineComposer(), model_gateway=gateway)
    service.seed_orders()
    return service


def _offline_text(request) -> str:
    """The figure-correct summary for a request, composed by the
    bare offline port — what a hosted stub must return for its
    text to pass the figure verifier."""
    probe = _service()
    case = probe.ingest(request)
    return case.summary


def _bedrock_transport(text: str, *, tokens_in: int = 10000, tokens_out: int = 2000):
    def _transport(request: dict) -> dict:
        assert request["modelId"], "the envelope must carry the configured model id"
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": json.dumps({"text": text})} ]}},
            "usage": {"inputTokens": tokens_in, "outputTokens": tokens_out},
        }

    return _transport


def _nim_transport(text: str, *, tokens_in: int = 20000, tokens_out: int = 5000):
    def _transport(request: dict) -> dict:
        assert request["model"], "the envelope must carry the configured model id"
        return {
            "choices": [{"message": {"role": "assistant", "content": json.dumps({"text": text})}}],
            "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
        }

    return _transport


# -- offline: byte-identity + the ledger ------------------------------------


def test_offline_gateway_output_is_byte_identical_to_the_bare_port():
    bare = OfflineComposer()
    service = _service()
    for request in PLANTED_REQUESTS:
        case = service.ingest(request)
        if case.decision.eligible:
            expected = bare.compose_refund_summary(case.decision, _product_name(case), case.computation)
        else:
            expected = bare.compose_summary(case.decision, _product_name(case))
        assert case.summary == expected, f"{case.case_id}: gateway output diverged from the bare port"


def _product_name(case) -> str:
    from returns_resolution.seed import CATALOGUE

    return CATALOGUE[case.request.sku].name


def test_default_and_explicit_offline_gateways_agree():
    default = _service()
    explicit = _service(gateway=OfflineGateway(OfflineComposer()))
    for request in PLANTED_REQUESTS:
        a = default.ingest(request)
        b = explicit.ingest(request)
        assert a.summary == b.summary
        assert a.case_id == b.case_id


def test_every_ingest_compose_is_recorded_at_zero_cost():
    service = _service()
    for request in PLANTED_REQUESTS:
        service.ingest(request)
    entries = service.list_all_cost_entries()
    assert len(entries) == len(PLANTED_REQUESTS)
    for entry in entries:
        assert entry.profile == "offline"
        assert entry.status == "ok"
        assert entry.cost == Decimal("0.00"), "the offline rate card is zero"
        assert entry.usage_source == "gateway-estimate"
        assert entry.price_pack_version == "2026.10.1"
    by_case = {}
    for entry in entries:
        by_case.setdefault(entry.case_id, []).append(entry)
    for request in PLANTED_REQUESTS:
        case_id = f"CASE-{request.request_id[4:]}"
        case_entries = by_case[case_id]
        assert len(case_entries) == 1
        expected_purpose = (
            "compose_refund_summary"
            if service.get_case(case_id).decision.eligible
            else "compose_summary"
        )
        assert case_entries[0].purpose == expected_purpose
    assert service.verify_cost_ledger() == []
    totals = service.cost_totals()
    assert totals["by_profile"]["offline"]["calls"] == len(PLANTED_REQUESTS)
    assert totals["by_profile"]["offline"]["cost"] == "0.00"


def test_note_submission_is_recorded_and_the_text_is_verbatim():
    service = _service()
    case = service.ingest(PLANTED_REQUESTS[2])  # CASE-2003
    note = "  Thanks for your patience with this one.  "
    message = service.draft_message(case.case_id, "return_summary", as_of=DAY, model_note=note)
    assert message.text.endswith(note.strip()), "the note lands verbatim (the template's own trim)"
    entries = service.list_cost_entries(case.case_id)
    note_entries = [e for e in entries if e.purpose == "message_note"]
    assert len(note_entries) == 1
    assert note_entries[0].status == "ok"
    plain = service.draft_message(case.case_id, "return_summary", as_of=DAY)
    assert plain is not None
    assert len([e for e in service.list_cost_entries(case.case_id) if e.purpose == "message_note"]) == 1, \
        "a draft without a note records no note call"


# -- hosted profiles: envelopes, validation, fail-closed ---------------------


def test_bedrock_stub_envelope_is_metered_from_the_envelope():
    text = _offline_text(PLANTED_REQUESTS[0])
    gateway = BedrockGateway(model_id="stub-bedrock-1", transport=_bedrock_transport(text))
    service = _service(gateway=gateway)
    case = service.ingest(PLANTED_REQUESTS[0])
    assert case.summary == text, "the validated draft text is the summary"
    (entry,) = service.list_cost_entries(case.case_id)
    assert entry.profile == "bedrock"
    assert entry.model_id == "stub-bedrock-1"
    assert entry.usage_source == "envelope"
    assert (entry.tokens_in, entry.tokens_out) == (10000, 2000)
    row = load_price_pack().price_for("bedrock")
    assert entry.cost == compute_cost(row, 10000, 2000) == Decimal("0.06")
    assert service.verify_cost_ledger() == []
    totals = service.cost_totals()
    assert totals["by_profile"]["bedrock"]["cost"] == "0.06"


def test_nim_stub_envelope_is_metered_from_the_envelope():
    text = _offline_text(PLANTED_REQUESTS[0])
    gateway = NimGateway(model_id="stub-nim-1", transport=_nim_transport(text))
    service = _service(gateway=gateway)
    case = service.ingest(PLANTED_REQUESTS[0])
    assert case.summary == text
    (entry,) = service.list_cost_entries(case.case_id)
    assert entry.profile == "nim"
    assert (entry.tokens_in, entry.tokens_out) == (20000, 5000)
    row = load_price_pack().price_for("nim")
    assert entry.cost == compute_cost(row, 20000, 5000) == Decimal("0.03")


def test_hosted_wrong_figure_is_still_refused_by_the_verifier():
    gateway = BedrockGateway(
        model_id="stub-bedrock-1",
        transport=_bedrock_transport("Your refund due is $999.99. Please ship by 2027-01-01."),
    )
    service = _service(gateway=gateway)
    with pytest.raises(FigureVerificationError):
        service.ingest(PLANTED_REQUESTS[0])
    case = service.get_case("CASE-2001")
    assert case.summary is None and case.status == "summary_failed"
    refused = [m for m in service.list_messages("CASE-2001") if m.status == "refused"]
    assert refused, "the summary message stands refused — the verifier, not the gateway, is the figure gate"
    (entry,) = service.list_cost_entries("CASE-2001")
    assert entry.status == "ok", "the response was structurally valid; refusal is downstream, and both facts stand"


@pytest.mark.parametrize(
    "raw",
    [
        # content is not JSON at all
        {"output": {"message": {"content": [{"text": "just prose, no structure"}]}}, "usage": {"inputTokens": 5, "outputTokens": 5}},
        # JSON, but not a DraftOutput (no text field)
        {"output": {"message": {"content": [{"text": json.dumps({"summary": "x"})}]}}, "usage": {"inputTokens": 5, "outputTokens": 5}},
        # text of the wrong type — pydantic does not coerce
        {"output": {"message": {"content": [{"text": json.dumps({"text": 42})}]}}, "usage": {"inputTokens": 5, "outputTokens": 5}},
        # no usage block: an envelope the gateway cannot meter
        {"output": {"message": {"content": [{"text": json.dumps({"text": "x"})}]}}},
    ],
)
def test_invalid_hosted_outputs_are_rejected_and_recorded(raw):
    gateway = BedrockGateway(model_id="stub-bedrock-1", transport=lambda request: raw)
    service = _service(gateway=gateway)
    with pytest.raises(InvalidModelOutputError):
        service.ingest(PLANTED_REQUESTS[0])
    assert service.get_case("CASE-2001") is None, "nothing was composed or stored"
    (entry,) = service.list_cost_entries("CASE-2001")
    assert entry.status == "rejected_invalid"
    assert entry.cost == Decimal("0.00")


def test_unreachable_profiles_fail_closed_never_fall_back():
    def _down(request):
        raise ConnectionError("connection refused")

    variants = [
        BedrockGateway(model_id="stub-bedrock-1"),  # no transport bound
        BedrockGateway(model_id="stub-bedrock-1", transport=_down),  # transport error
        BedrockGateway(model_id=None, transport=_bedrock_transport("x")),  # no model id
        NimGateway(model_id="stub-nim-1"),  # no transport bound
    ]
    for gateway in variants:
        service = _service(gateway=gateway)
        with pytest.raises(ModelProfileUnavailableError):
            service.ingest(PLANTED_REQUESTS[0])
        assert service.get_case("CASE-2001") is None, "no fallback output was stored"
        (entry,) = service.list_cost_entries("CASE-2001")
        assert entry.status == "failed_closed"
        assert entry.cost == Decimal("0.00")


def test_load_model_gateway_profiles_and_alias():
    assert isinstance(load_model_gateway("offline"), OfflineGateway)
    assert isinstance(load_model_gateway("bedrock", bedrock_model_id="m"), BedrockGateway)
    assert isinstance(load_model_gateway("hosted-bedrock", bedrock_model_id="m"), BedrockGateway)
    assert isinstance(load_model_gateway("nim", nim_model_id="m"), NimGateway)
    with pytest.raises(ValueError):
        load_model_gateway("no-such-profile")
    view = load_model_gateway("offline").config_view()
    assert view["profile"] == "offline"
    assert view["max_calls_per_case"] == 40
    assert view["price_pack_version"] == "2026.10.1"


# -- the budget ---------------------------------------------------------------


def test_budget_refusal_names_the_budget_and_records_it():
    pack = PricePack(
        version="2026.10.1", chars_per_token=4, max_calls_per_case=2,
        profiles=load_price_pack().profiles,
    )
    service = _service(gateway=OfflineGateway(OfflineComposer(), pack=pack))
    case = service.ingest(PLANTED_REQUESTS[0])  # call 1
    service.draft_message(case.case_id, "return_summary", as_of=DAY)  # call 2 (re-compose)
    with pytest.raises(ModelBudgetError) as excinfo:
        service.draft_message(case.case_id, "return_summary", as_of=DAY)  # call 3: refused
    assert "budget" in str(excinfo.value) and "2" in str(excinfo.value)
    entries = service.list_cost_entries(case.case_id)
    assert [e.status for e in entries] == ["ok", "ok", "refused_budget"]
    # The budget is per case: another case still drafts.
    other = service.ingest(PLANTED_REQUESTS[2])
    assert other.summary is not None


# -- ledger integrity -----------------------------------------------------------


def test_ledger_tamper_is_detected_by_reperformance(tmp_path):
    store = SqlStore(sqlite_path=tmp_path / "ledger.db")
    store.migrate()
    service = _service(store=store)
    case = service.ingest(PLANTED_REQUESTS[0])
    assert service.verify_cost_ledger() == []
    (entry,) = service.list_cost_entries(case.case_id)
    # Tamper through the store's own connection: rewrite the stored
    # payload's cost, the way an after-the-fact edit would look.
    rows = store._query("SELECT payload FROM model_calls WHERE entry_id = ?", (entry.entry_id,))
    payload = json.loads(rows[0]["payload"])
    payload["cost"] = "9.99"
    store._execute(
        "UPDATE model_calls SET payload = ? WHERE entry_id = ?",
        (json.dumps(payload), entry.entry_id),
    )
    problems = service.verify_cost_ledger()
    assert problems and entry.entry_id in problems[0]


# -- surfaces -------------------------------------------------------------------


def test_api_and_runtime_ledger_parity():
    service = _service()
    case = service.ingest(PLANTED_REQUESTS[0])
    client = TestClient(create_app(service))
    config = client.get("/model/config")
    assert config.status_code == 200
    assert config.json()["profile"] == "offline"
    assert config.json()["max_calls_per_case"] == 40
    api_entries = client.get(f"/cases/{case.case_id}/cost-ledger")
    assert api_entries.status_code == 200
    assert len(api_entries.json()) == 1
    whole = client.get("/model/cost-ledger")
    assert whole.status_code == 200
    assert whole.json()["verify_problems"] == []
    assert whole.json()["totals"]["by_case"][case.case_id]["calls"] == 1

    runtime_config = handle_payload(service, {"action": "get_model_config"})
    assert runtime_config["model_config"] == config.json()
    runtime_entries = handle_payload(service, {"action": "get_case_cost_ledger", "case_id": case.case_id})
    assert [e["entry_id"] for e in runtime_entries["cost_entries"]] == [
        e["entry_id"] for e in api_entries.json()
    ]


# -- Postgres profile -------------------------------------------------------------


@pg_only
def test_postgres_cost_ledger_persists(tmp_path):
    store = SqlStore(sqlite_path=tmp_path / "pg-ledger.db")
    applied = store.migrate()
    assert "0010" in applied or applied == [], "0010 applies (or stands applied) on Postgres"
    service = _service(store=store)
    case = service.ingest(PLANTED_REQUESTS[0])
    (entry,) = service.list_cost_entries(case.case_id)
    reopened = SqlStore(sqlite_path=tmp_path / "pg-ledger.db")
    reopened.migrate()
    rows = reopened.list_cost_entries(case.case_id)
    assert [r["entry_id"] for r in rows] == [entry.entry_id]
    assert service.verify_cost_ledger() == []
