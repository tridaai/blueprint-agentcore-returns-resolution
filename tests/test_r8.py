"""R8 — customer messaging: typed templates, the messaging
verifier (figures, facts, forbidden content), the lifecycle
(draft → verified → queued → sent, each a recorded act), the
withdraw-and-re-draft rule for messages that go stale, and parity
across the service, the API and the Runtime dispatch."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from returns_resolution.api import create_app
from returns_resolution.intake import FRAUD_FEED_REQUESTS, PLANTED_REQUESTS
from returns_resolution.messaging import LocalMessagingDouble, MessagingProviderError
from returns_resolution.modelport import OfflineComposer
from returns_resolution.runtime_app import handle_payload
from returns_resolution.service import CaseService, FigureVerificationError, MessagingError
from returns_resolution.store import InMemoryStore, SqlStore
from returns_resolution.verifier import MessageContext, verify_message_text

DAY = date(2026, 10, 27)
RECEIVED = date(2026, 10, 30)  # the carrier's scripted received day
GRADED = date(2026, 10, 31)


def _service(messaging_port=None):
    double = messaging_port or LocalMessagingDouble()
    service = CaseService(InMemoryStore(), OfflineComposer(), messaging_port=double)
    service.seed_orders()
    service.seed_history()
    return service, double


def _feed(service) -> None:
    for request in PLANTED_REQUESTS:
        service.ingest(request)


def _label(service, case_id, on=DAY):
    return service.invoke_tool(
        "create_return_label", {"case_id": case_id, "issued_on": on},
        case_id=case_id, called_on=on,
    )


def _walk_to_sent(service, case_id, message_type, on=DAY, note=None):
    draft = service.draft_message(case_id, message_type, as_of=on, model_note=note)
    service.verify_message(draft.message_id, as_of=on)
    service.queue_message(draft.message_id, as_of=on)
    return service.send_message(draft.message_id, as_of=on)


def _grade_boots_2003(service):
    """CASE-2003 end to end: label, execute, receipt, grade used."""
    _label(service, "CASE-2003")
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY)
    service.execute_refund("CASE-2003", idempotency_key="r8-2003", as_of=DAY)
    service.record_warehouse_receipt("CASE-2003", received_by="w.chen", received_on=RECEIVED)
    return service.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="used", graded_on=GRADED
    )


# -- the summary path, migrated -----------------------------------------


def test_summary_is_recorded_as_a_verified_message_at_ingest():
    service, _ = _service()
    _feed(service)
    (message,) = service.list_messages("CASE-2003")
    assert message.message_type == "return_summary"
    assert message.status == "verified"
    assert message.text == service.get_case("CASE-2003").summary
    assert [t.state for t in message.transitions] == ["draft", "verified"]
    assert message.violations == []


def test_label_issuance_supersedes_the_ingest_summary_message():
    service, _ = _service()
    _feed(service)
    first = service.list_messages("CASE-2003")[0]
    _label(service, "CASE-2003")
    messages = service.list_messages("CASE-2003")
    assert len(messages) == 2
    by_id = {m.message_id: m for m in messages}
    assert by_id[first.message_id].status == "withdrawn"
    current = messages[-1]
    assert current.status == "verified"
    assert current.text == service.get_case("CASE-2003").summary
    assert "label" in current.text


def test_tampered_summary_is_a_refused_message_not_a_sendable_one():
    class TamperedComposer(OfflineComposer):
        def compose_summary(self, decision, product_name):
            text = super().compose_summary(decision, product_name)
            return text.replace("$130.50", "$999.99")

        def compose_refund_summary(self, decision, computation, product_name):
            text = super().compose_refund_summary(decision, computation, product_name)
            return text.replace("$130.50", "$999.99")

    service = CaseService(InMemoryStore(), TamperedComposer())
    service.seed_orders()
    service.seed_history()
    request = next(r for r in PLANTED_REQUESTS if r.request_id == "REQ-2003")
    with pytest.raises(FigureVerificationError):
        service.ingest(request)
    (message,) = service.list_messages("CASE-2003")
    assert message.status == "refused"
    assert message.violations  # the figure violation is on the record
    assert service.list_refused_messages("CASE-2003") == [message]


# -- templates + lifecycle ------------------------------------------------


def test_label_instructions_walk_the_full_lifecycle():
    service, double = _service()
    _feed(service)
    label = _label(service, "CASE-2003")
    sent = _walk_to_sent(service, "CASE-2003", "label_instructions")
    assert label.carrier_ref in sent.text
    assert "2026-11-09" in sent.text  # the decision's ship-by date
    assert sent.status == "sent"
    assert sent.provider == "local-double (messaging stand-in)"
    assert sent.provider_ref.startswith("double-msg-")
    assert [t.state for t in sent.transitions] == ["draft", "verified", "queued", "sent"]
    assert double.delivery_count == 1
    assert double.deliveries[sent.message_id] == sent.text
    # A repeat send is the same act, not a second delivery.
    again = service.send_message(sent.message_id, as_of=DAY)
    assert again.provider_ref == sent.provider_ref
    assert double.delivery_count == 1


def test_refund_executed_notice_names_the_receipt():
    service, _ = _service()
    _feed(service)
    with pytest.raises(MessagingError, match="no executed refund receipt"):
        service.draft_message("CASE-2003", "refund_executed_notice", as_of=DAY)
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY)
    service.execute_refund("CASE-2003", idempotency_key="r8-exec", as_of=DAY)
    sent = _walk_to_sent(service, "CASE-2003", "refund_executed_notice")
    assert "$130.50" in sent.text
    assert "2026-10-27" in sent.text  # the receipt's execution date, from the artefact


def test_grading_outcome_notice_after_the_cascade():
    service, _ = _service()
    _feed(service)
    _grade_boots_2003(service)
    sent = _walk_to_sent(service, "CASE-2003", "grading_outcome_notice", on=GRADED)
    assert "$130.50" in sent.text and "$116.00" in sent.text and "$14.50" in sent.text
    assert "condition as used" in sent.text
    assert "outlet" in sent.text  # the graded disposition, as a fact


def test_adjustment_notice_recovery_wording():
    service, _ = _service()
    _feed(service)
    _grade_boots_2003(service)
    sent = _walk_to_sent(service, "CASE-2003", "adjustment_notice", on=GRADED)
    assert "$14.50 more than that was refunded to you" in sent.text
    assert "stands as an amount owed on the case record" in sent.text


def test_adjustment_notice_top_up_after_execution():
    # CASE-2099 is the eval suite's owing case: boots declared used
    # ($116.00 paid), graded like_new — a $14.50 top-up falls due.
    from returns_resolution.models import ReturnRequest

    service, _ = _service()
    service.ingest(
        ReturnRequest(
            request_id="REQ-2099", order_id="ORD-1003", sku="BOOT-TRL", qty=1,
            reason_code="wrong_size", condition_declared="used",
            requested_on=date(2026, 10, 26),
        )
    )
    service.record_approval("CASE-2099", approver="d.okafor", role="returns_lead", as_of=DAY)
    service.execute_refund("CASE-2099", idempotency_key="r8-2099", as_of=DAY)
    _label(service, "CASE-2099")
    service.record_warehouse_receipt("CASE-2099", received_by="w.chen", received_on=RECEIVED)
    service.record_grading(
        "CASE-2099", grader="p.novak", graded_condition="like_new", graded_on=GRADED
    )
    service.record_approval("CASE-2099", approver="d.okafor", role="returns_lead", as_of=GRADED)
    service.execute_top_up("CASE-2099", idempotency_key="r8-topup", as_of=GRADED)
    sent = _walk_to_sent(service, "CASE-2099", "adjustment_notice", on=GRADED)
    assert "The additional $14.50 was sent on 2026-10-31." in sent.text


def test_fraud_notice_cleared_discloses_nothing_internal():
    service, _ = _service()
    service.ingest(FRAUD_FEED_REQUESTS[0])
    with pytest.raises(MessagingError, match="no fraud-review decision"):
        service.draft_message("CASE-3008", "fraud_review_notice", as_of=DAY)
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez", outcome="cleared_with_reason",
        reason_code="legitimate_pattern_explained",
    )
    sent = _walk_to_sent(service, "CASE-3008", "fraud_review_notice")
    assert "$116.00" in sent.text
    for leaked in ("return_velocity", "95", "score", "threshold", "FRAUD_REVIEW", "legitimate_pattern"):
        assert leaked not in sent.text


def test_fraud_notice_confirmed_promises_no_refund():
    service, _ = _service()
    service.ingest(FRAUD_FEED_REQUESTS[0])
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez", outcome="confirmed_fraud",
        reason_code="pattern_verified",
    )
    sent = _walk_to_sent(service, "CASE-3008", "fraud_review_notice")
    assert "unable to process a refund for this return" in sent.text
    assert "$145.00" in sent.text  # the item amount, from the decision of record


def test_free_form_text_lives_only_in_the_note_slot():
    service, _ = _service()
    _feed(service)
    draft = service.draft_message(
        "CASE-2003", "return_summary", as_of=DAY,
        model_note="Please keep the original box if you still have it.",
    )
    verified = service.verify_message(draft.message_id, as_of=DAY)
    assert verified.status == "verified"
    assert draft.text.endswith("Please keep the original box if you still have it.")


# -- the verifier's three checks, as probes -------------------------------


def test_probe_inflated_figure_is_refused():
    service, _ = _service()
    _feed(service)
    draft = service.draft_message(
        "CASE-2003", "return_summary", as_of=DAY, model_note="Your refund is $999.99."
    )
    refused = service.verify_message(draft.message_id, as_of=DAY)
    assert refused.status == "refused"
    assert any("$999.99" in v for v in refused.violations)
    with pytest.raises(MessagingError, match="only a verified message can be queued"):
        service.queue_message(draft.message_id, as_of=DAY)


def test_probe_wrong_disposition_fact_is_refused():
    service, _ = _service()
    _feed(service)  # CASE-2006 is HOLD_FOR_INSPECTION
    draft = service.draft_message(
        "CASE-2006", "return_summary", as_of=DAY,
        model_note="Good news: the item will be restocked right away.",
    )
    refused = service.verify_message(draft.message_id, as_of=DAY)
    assert refused.status == "refused"
    assert any("RESTOCK" in v and "fact" in v for v in refused.violations)


def test_probe_fraud_threshold_leak_is_refused():
    service, _ = _service()
    service.ingest(FRAUD_FEED_REQUESTS[0])
    service.record_fraud_review(
        "CASE-3008", reviewer="r.alvarez", outcome="cleared_with_reason",
        reason_code="legitimate_pattern_explained",
    )
    draft = service.draft_message(
        "CASE-3008", "fraud_review_notice", as_of=DAY,
        model_note="Your return_velocity pattern scored 95 points, over the review threshold.",
    )
    refused = service.verify_message(draft.message_id, as_of=DAY)
    assert refused.status == "refused"
    assert any("forbidden" in v for v in refused.violations)


def test_probe_other_customer_and_internal_ids_are_refused():
    service, _ = _service()
    _feed(service)
    computation = service.get_computation("CASE-2003")
    other = service.draft_message(
        "CASE-2003", "return_summary", as_of=DAY,
        model_note="This matches the outcome on CASE-3008.",
    )
    assert service.verify_message(other.message_id, as_of=DAY).status == "refused"
    internal = service.draft_message(
        "CASE-2003", "return_summary", as_of=DAY,
        model_note=f"Your computation reference is {computation.computation_id}.",
    )
    refused = service.verify_message(internal.message_id, as_of=DAY)
    assert refused.status == "refused"
    assert any("internal artefact id" in v for v in refused.violations)


def test_probe_unstated_refund_timing_promise_is_refused():
    service, _ = _service()
    _feed(service)
    service.record_approval("CASE-2003", approver="d.okafor", role="returns_lead", as_of=DAY)
    service.execute_refund("CASE-2003", idempotency_key="r8-timing", as_of=DAY)
    draft = service.draft_message(
        "CASE-2003", "refund_executed_notice", as_of=DAY,
        model_note="The money will arrive within 3 business days.",
    )
    refused = service.verify_message(draft.message_id, as_of=DAY)
    assert refused.status == "refused"
    assert any("forbidden" in v for v in refused.violations)


def test_verifier_unit_checks_percent_and_disposition():
    context = MessageContext(
        case_id="CASE-1", order_id="ORD-1",
        allowed_amounts={Decimal("10.00")}, allowed_percents={Decimal("10")},
        disposition_facts={"RESTOCK"},
    )
    assert verify_message_text("A fee of $10.00 at 10% applies. The item will be restocked.", context) == []
    violations = verify_message_text(
        "A fee of $10.00 at 25% applies. The item will be sent to our outlet.", context
    )
    assert any("25%" in v for v in violations)
    assert any("OUTLET" in v for v in violations)


# -- lifecycle discipline ---------------------------------------------------


def test_lifecycle_order_is_enforced():
    service, _ = _service()
    _feed(service)
    _label(service, "CASE-2003")
    draft = service.draft_message("CASE-2003", "label_instructions", as_of=DAY)
    with pytest.raises(MessagingError, match="only a verified message can be queued"):
        service.queue_message(draft.message_id, as_of=DAY)
    service.verify_message(draft.message_id, as_of=DAY)
    with pytest.raises(MessagingError, match="only a queued message can be sent"):
        service.send_message(draft.message_id, as_of=DAY)
    with pytest.raises(MessagingError, match="only a draft can be verified"):
        service.verify_message(draft.message_id, as_of=DAY)
    with pytest.raises(MessagingError, match="unknown message type"):
        service.draft_message("CASE-2003", "carrier_pigeon", as_of=DAY)
    with pytest.raises(LookupError):
        service.draft_message("CASE-9999", "label_instructions", as_of=DAY)


def test_provider_failure_leaves_the_message_queued():
    class FailingDouble:
        provider_name = "failing-double (messaging stand-in)"

        def send(self, *, message_id, case_id, text):
            raise MessagingProviderError("provider unavailable (scripted)")

    service, _ = _service(messaging_port=FailingDouble())
    _feed(service)
    _label(service, "CASE-2003")
    draft = service.draft_message("CASE-2003", "label_instructions", as_of=DAY)
    service.verify_message(draft.message_id, as_of=DAY)
    service.queue_message(draft.message_id, as_of=DAY)
    with pytest.raises(MessagingError, match="stays queued"):
        service.send_message(draft.message_id, as_of=DAY)
    assert service.get_message(draft.message_id).status == "queued"


# -- staleness: withdraw + re-draft, never send stale -----------------------


def test_queued_summary_goes_stale_after_grading():
    service, double = _service()
    _feed(service)
    _label(service, "CASE-2003")
    queued = [m for m in service.list_messages("CASE-2003") if m.status == "verified"][-1]
    service.queue_message(queued.message_id, as_of=DAY)
    service.record_warehouse_receipt("CASE-2003", received_by="w.chen", received_on=RECEIVED)
    service.record_grading(
        "CASE-2003", grader="p.novak", graded_condition="used", graded_on=GRADED
    )
    with pytest.raises(MessagingError):
        service.send_message(queued.message_id, as_of=GRADED)
    assert service.get_message(queued.message_id).status == "withdrawn"
    assert queued.message_id not in double.deliveries
    replacement = [m for m in service.list_messages("CASE-2003") if m.status == "verified"][-1]
    assert replacement.message_id != queued.message_id
    assert "$116.00" in replacement.text  # re-drafted against the graded artefacts
    service.queue_message(replacement.message_id, as_of=GRADED)
    sent = service.send_message(replacement.message_id, as_of=GRADED)
    assert sent.status == "sent"


def test_send_time_fingerprint_catches_a_voided_label():
    service, double = _service()
    _feed(service)
    label = _label(service, "CASE-2003")
    draft = service.draft_message("CASE-2003", "label_instructions", as_of=DAY)
    service.verify_message(draft.message_id, as_of=DAY)
    service.queue_message(draft.message_id, as_of=DAY)
    service.invoke_tool(
        "void_return_label",
        {"case_id": "CASE-2003", "label_id": label.label_id, "voided_by": "d.okafor",
         "reason": "wrong service level", "voided_on": DAY},
        case_id="CASE-2003", called_on=DAY,
    )
    with pytest.raises(MessagingError, match="withdrawn|stale"):
        service.send_message(draft.message_id, as_of=DAY)
    assert service.get_message(draft.message_id).status == "withdrawn"
    assert double.delivery_count == 0


# -- timeline, persistence, parity -------------------------------------------


def test_timeline_names_message_stages_and_the_queued_gap():
    service, _ = _service()
    _feed(service)
    _label(service, "CASE-2003")
    draft = service.draft_message("CASE-2003", "label_instructions", as_of=DAY)
    service.verify_message(draft.message_id, as_of=DAY)
    service.queue_message(draft.message_id, as_of=DAY)
    view = service.get_timeline("CASE-2003", DAY)
    assert any(entry.stage == "message" for entry in view.entries)
    assert "customer_message" in [gap.kind for gap in view.gaps]


def test_messages_survive_a_store_round_trip(tmp_path):
    db = tmp_path / "returns.db"
    store = SqlStore(sqlite_path=db)
    assert "0008" in store.migrate()
    double = LocalMessagingDouble()
    service = CaseService(store, OfflineComposer(), messaging_port=double)
    service.seed_orders()
    service.seed_history()
    _feed(service)
    _label(service, "CASE-2003")
    sent = _walk_to_sent(service, "CASE-2003", "label_instructions")
    reopened = CaseService(SqlStore(sqlite_path=db), OfflineComposer())
    loaded = reopened.get_message(sent.message_id)
    assert loaded is not None and loaded.status == "sent"
    assert loaded.text == sent.text
    assert [t.state for t in loaded.transitions] == ["draft", "verified", "queued", "sent"]


def test_api_messaging_parity():
    service, _ = _service()
    _feed(service)
    _label(service, "CASE-2003")
    client = TestClient(create_app(service=service))
    pack = client.get("/messaging/disclosure-pack")
    assert pack.status_code == 200 and pack.json()["version"] == "2026.10.1"
    created = client.post(
        "/cases/CASE-2003/messages",
        json={"message_type": "label_instructions", "as_of": "2026-10-27"},
    )
    assert created.status_code == 201
    message_id = created.json()["message_id"]
    for act in ("verify", "queue", "send"):
        response = client.post(
            f"/cases/CASE-2003/messages/{message_id}/{act}", json={"as_of": "2026-10-27"}
        )
        assert response.status_code == 200, act
    assert response.json()["status"] == "sent"
    listed = client.get("/cases/CASE-2003/messages")
    assert listed.status_code == 200 and any(m["message_id"] == message_id for m in listed.json())
    bad = client.post(
        "/cases/CASE-2003/messages",
        json={"message_type": "return_summary", "as_of": "2026-10-27",
              "model_note": "Your refund is $999.99."},
    )
    refused_id = bad.json()["message_id"]
    client.post(f"/cases/CASE-2003/messages/{refused_id}/verify", json={"as_of": "2026-10-27"})
    refused = client.get("/cases/CASE-2003/messages/refused")
    assert any(m["message_id"] == refused_id for m in refused.json())
    assert client.post(
        "/cases/CASE-9999/messages",
        json={"message_type": "label_instructions", "as_of": "2026-10-27"},
    ).status_code == 404
    assert client.post(
        "/cases/CASE-2003/messages",
        json={"message_type": "smoke_signal", "as_of": "2026-10-27"},
    ).status_code == 409


def test_runtime_messaging_parity():
    service, _ = _service()
    _feed(service)
    _label(service, "CASE-2003")
    out = handle_payload(service, {
        "action": "draft_message", "case_id": "CASE-2003",
        "message_type": "label_instructions", "as_of": "2026-10-27",
    })
    message_id = out["message"]["message_id"]
    for action in ("verify_message", "queue_message", "send_message"):
        out = handle_payload(service, {"action": action, "message_id": message_id, "as_of": "2026-10-27"})
        assert "error" not in out, action
    assert out["message"]["status"] == "sent"
    listed = handle_payload(service, {"action": "list_messages", "case_id": "CASE-2003"})
    assert any(m["message_id"] == message_id for m in listed["messages"])
    pack = handle_payload(service, {"action": "get_disclosure_pack"})
    assert pack["disclosure_pack"]["version"] == "2026.10.1"
    error = handle_payload(service, {
        "action": "draft_message", "case_id": "CASE-2003",
        "message_type": "smoke_signal", "as_of": "2026-10-27",
    })
    assert "no template exists" in error["error"]
