"""Case service — the workflow the Runtime entrypoint and API share.

One path, used by every surface: seed orders -> ingest a request ->
eligibility in code -> agent drafts the summary -> figure verifier ->
store. If the verifier rejects the draft, the case is stored as
`summary_failed` with no summary — an unverified customer message
never exists anywhere in the system.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal

from .carrier import CarrierPort, CarrierProviderError, LocalCarrierDouble
from .disposition import (
    BASIS_GRADED,
    HOLD_FOR_INSPECTION,
    ChannelTable,
    MatrixPack,
    decide_disposition,
    load_channel_table,
    load_matrix_pack,
    verify_disposition,
)
from .grading import (
    CONDITION_RANKS,
    CONDITION_VOCABULARY,
    DATA_WIPE_STATES,
    FUNCTIONAL_RESULTS,
    GRADING_PACK_VERSION,
    WILDLY_WORSE_JUMP,
    inspection_successor_decision,
    resolve_inspection,
    verify_inspection_decision,
)
from .eligibility import case_id_for, evaluate
from .evidence import EvidenceStore
from .fraud import (
    BLOCKED_STATES,
    CaseFact,
    HistoryFacts,
    ScoringPack,
    derive_fraud_state,
    load_scoring_pack,
    score_from_history,
    verify_fraud_score,
)
from .gateway import (
    GatewayRequest,
    GatewayResponse,
    PolicyDeniedError,
    ToolCallRecord,
    ToolDefinition,
    ToolError,
    ToolGateway,
)
from .gateway_policy import (
    ACTOR_STAFF,
    ACTOR_WAREHOUSE,
    GatewayPolicyPack,
    PolicyDenial,
    ToolFacts,
    evaluate_gateway_policy,
    load_gateway_policy_pack,
)
from .memory import (
    EVENT_CASE_DECIDED,
    EVENT_CASE_GRADED,
    EVENT_FRAUD_REVIEW_CLOSED,
    EVENT_REFUND_EXECUTED,
    LocalMemoryDouble,
    MemoryAccessEntry,
    MemoryPack,
    MemoryPort,
    MemoryRecord,
    assign_record_id,
    load_memory_pack,
)
from .messaging import (
    MESSAGING_PACK_VERSION,
    STATE_DRAFT,
    STATE_QUEUED,
    STATE_REFUSED,
    STATE_SENT,
    STATE_VERIFIED,
    STATE_WITHDRAWN,
    TEMPLATES,
    DisclosurePack,
    LocalMessagingDouble,
    MessageFacts,
    MessagingPort,
    MessagingProviderError,
    load_disclosure_pack,
)
from .modelport import ModelPort
from .models import (
    CarrierEvent,
    CaseTimeline,
    CustomerMessage,
    DispositionDecision,
    ExecutionTransition,
    FraudReviewDecision,
    FraudScore,
    GradingReport,
    LabelCostReconciliation,
    LabelVoid,
    MessageTransition,
    Order,
    PickupBooking,
    RefundAdjustment,
    RefundApproval,
    RefundComputation,
    RefundExecution,
    RefundReceipt,
    ReturnCase,
    ReturnLabel,
    ReturnRequest,
    TenderAllocation,
    WarehouseReceipt,
    money,
)
from .payments import (
    APPROVERS_REQUIRED,
    LocalPaymentsDouble,
    PaymentsPort,
    PaymentsProviderError,
    ProviderReceipt,
    approval_tier,
)
from .policy import PolicyPack, load_policy_pack
from .refunds import compute_refund, verify_computation
from .seed import CATALOGUE, HISTORY_CASES, HISTORY_ORDERS, ORDERS, PRODUCT_WEIGHTS_GRAMS
from .store import Store
from .timeline import build_timeline
from .verifier import MessageContext, verify_message_text, verify_summary


class FigureVerificationError(RuntimeError):
    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations))
        self.violations = violations


class FraudHoldError(RuntimeError):
    """The case's refund path is blocked by its fraud state."""


class FraudReviewError(RuntimeError):
    """A fraud-review act was refused (wrong state, unnamed reviewer,
    or an outcome/reason outside the pack's vocabulary)."""


class ApprovalError(RuntimeError):
    """A refund approval act, or an execution's approval gate, was
    refused — the message names the unmet requirement."""


class PaymentsError(RuntimeError):
    """A refund execution was refused or the provider failed — the
    message names the unmet precondition or the failure."""


class DuplicateRefundError(PaymentsError):
    """duplicate_refund: this computation's refund already executed
    under a different idempotency key. Refused, always."""


class CarrierError(RuntimeError):
    """A carrier/label tool act was refused — the message names the
    unmet precondition (ineligible case, missing schedule entry,
    non-bulky pickup, already-voided label, …)."""


class GradingError(RuntimeError):
    """A warehouse receipt or grading act was refused — the message
    names the unmet precondition (no carrier receipt yet, unnamed
    grader, the receiver grading their own parcel, a held case
    graded without its inspection facts, …)."""


class MessagingError(RuntimeError):
    """A customer-message act was refused — the message names the
    gap (an unknown message type with no template, a missing
    prerequisite artefact, a lifecycle transition the message's
    state does not allow, a stale message at send time, …)."""


def _chain_order(payloads: list[dict], id_key: str) -> list[dict]:
    """Order a supersession chain oldest-first by walking its links:
    the root is the artefact that supersedes nothing in the set, and
    each next member is the artefact whose `supersedes` names the
    previous one's id. A chain that does not walk cleanly falls back
    to id order — unreachable through the service, which only ever
    appends heads."""
    by_id = {p[id_key]: p for p in payloads}
    successor = {p["supersedes"]: p for p in payloads if p.get("supersedes") in by_id}
    roots = [p for p in payloads if p.get("supersedes") not in by_id]
    ordered: list[dict] = []
    for root in sorted(roots, key=lambda p: p[id_key]):
        current: dict | None = root
        while current is not None:
            ordered.append(current)
            current = successor.get(current[id_key])
    if len(ordered) != len(payloads):
        return sorted(payloads, key=lambda p: p[id_key])
    return ordered


class CaseService:
    def __init__(
        self,
        store: Store,
        model_port: ModelPort,
        pack: PolicyPack | None = None,
        evidence_store: EvidenceStore | None = None,
        matrix: MatrixPack | None = None,
        channels: ChannelTable | None = None,
        scoring_pack: ScoringPack | None = None,
        payments_port: PaymentsPort | None = None,
        gateway_policy_pack: GatewayPolicyPack | None = None,
        carrier_port: CarrierPort | None = None,
        messaging_port: MessagingPort | None = None,
        disclosure_pack: DisclosurePack | None = None,
        memory_pack: MemoryPack | None = None,
        memory_port: MemoryPort | None = None,
    ):
        self._store = store
        self._model = model_port
        self._pack = pack or load_policy_pack()
        self._evidence = evidence_store
        self._matrix = matrix or load_matrix_pack()
        self._channels = channels or load_channel_table()
        self._scoring = scoring_pack or load_scoring_pack()
        # The payments port defaults to the LOCAL DOUBLE (labelled as
        # such on every artefact it produces): no real money moves
        # until a deployment binds the configured AgentCore Payments
        # provider to this port.
        self._payments = payments_port or LocalPaymentsDouble()
        # The carrier port likewise defaults to the LOCAL DOUBLE
        # (labelled on every artefact): no real label prints until a
        # deployment binds the retailer's carrier integration.
        self._carrier = carrier_port or LocalCarrierDouble()
        # The messaging port (R8) likewise defaults to the LOCAL
        # DOUBLE (labelled on every delivery): no message reaches a
        # real customer until a deployment binds the retailer's
        # messaging provider. The disclosure pack is the versioned
        # data the messaging verifier enforces alongside it.
        self._messaging = messaging_port or LocalMessagingDouble()
        self._disclosure = disclosure_pack or load_disclosure_pack()
        # The memory port (R9) defaults to the LOCAL DOUBLE
        # (labelled on every record it writes): no real memory
        # service is consulted until a deployment binds AgentCore
        # Memory to this port. The double's persistence is the
        # store's memory tables (migration 0009); the port owns
        # the semantics — customer scoping, retention, access log.
        self._memory_pack = memory_pack or load_memory_pack()
        self._memory = memory_port or LocalMemoryDouble(
            pack=self._memory_pack,
            put_record=store.put_memory_record,
            get_record=store.get_memory_record,
            list_records=store.list_memory_records,
            record_index=store.list_memory_record_index,
            put_access=store.put_memory_access,
            count_access=store.count_memory_access,
        )
        # The gateway policy pack (R11): versioned data, evaluated
        # by the gateway BEFORE any handler runs — actors on every
        # tool, and the money tool's state and amount constraints.
        # A denial is recorded with the policy's id and the handler
        # is never reached.
        self._gateway_policy = gateway_policy_pack or load_gateway_policy_pack()
        # The tool gateway (R6, generalized R11): the ONLY path
        # tool-shaped actions take — carrier tools, refund execution
        # and the grading callback alike. Handlers are the _tool_*
        # methods below; every call — success, refusal or denial —
        # is recorded into the store.
        self._gateway = ToolGateway(
            handlers={
                "create_return_label": self._tool_create_label,
                "void_return_label": self._tool_void_label,
                "get_carrier_events": self._tool_get_carrier_events,
                "book_pickup": self._tool_book_pickup,
                "execute_refund": self._tool_execute_refund,
                "submit_grading_report": self._tool_submit_grading_report,
            },
            record=self._store.put_tool_call,
            count_calls=self._store.count_tool_calls,
            policy_evaluator=self._evaluate_gateway_policy,
        )

    @property
    def payments_port(self) -> PaymentsPort:
        return self._payments

    @property
    def carrier_port(self) -> CarrierPort:
        return self._carrier

    @property
    def messaging_port(self) -> MessagingPort:
        return self._messaging

    @property
    def memory_port(self) -> MemoryPort:
        return self._memory

    # -- seeding ---------------------------------------------------------
    def seed_orders(self) -> int:
        for order in ORDERS.values():
            self._store.put_order(order.model_dump(mode="json"))
        return len(ORDERS)

    def seed_history(self) -> int:
        """Load the R4 fraud-history seed: the planted customers'
        orders and their closed historical cases, as stored
        artefacts. Idempotent — re-seeding restores the same rows."""
        for order in HISTORY_ORDERS.values():
            self._store.put_order(order.model_dump(mode="json"))
        for case in HISTORY_CASES:
            self._store.put_case(case.model_dump(mode="json"))
        return len(HISTORY_CASES)

    # -- workflow ----------------------------------------------------------
    def ingest(self, request: ReturnRequest) -> ReturnCase:
        existing = self._store.get_case_by_request(request.request_id)
        if existing is not None:
            return ReturnCase.model_validate(existing)

        order_payload = self._store.get_order(request.order_id)
        if order_payload is None:
            raise LookupError(f"unknown order: {request.order_id}")
        order = Order.model_validate(order_payload)
        product = CATALOGUE.get(request.sku)

        decision = evaluate(self._pack, order, product, request)
        self._store.put_decision(decision.model_dump(mode="json"))

        computation = compute_refund(self._pack, order, product, request, decision)
        self._store.put_computation(computation.model_dump(mode="json"))

        # Disposition (R3): only an eligible return has an item on its
        # way back to be disposed of — a denied case gets no decision,
        # rather than a decision nobody may act on. An eligible case
        # whose facts the matrix cannot cover gets a stored
        # not_decidable artefact naming the missing dimension.
        disposition = None
        if decision.eligible:
            disposition = self._decide_for(order, product, request, computation.computation_id)
            self._store.put_disposition(disposition.model_dump(mode="json"))

        # Fraud scoring (R4): every case is scored over the stored
        # artefacts at ingest. The artefact is content-addressed and
        # pinned to its inputs; the case carries it plus the derived
        # fraud state. A FRAUD_REVIEW band blocks the refund path as
        # state — the R5 gates must consult it.
        scored_fact = self._fact_for(order, request)
        score = self._score_over(scored_fact)
        self._store.put_fraud_score(score.model_dump(mode="json"))
        fraud_state = derive_fraud_state(score.band, current_score_id=score.score_id, reviews=[])

        product_name = product.name if product else request.sku
        summary_text = self._compose_summary_text(decision, computation, product_name)
        violations = verify_summary(summary_text, decision, computation)
        case = ReturnCase(
            case_id=case_id_for(request.request_id),
            request=request,
            order_id=order.order_id,
            customer_id=order.customer_id,
            status="summary_ready" if not violations else "summary_failed",
            decision=decision,
            computation=computation,
            disposition=disposition,
            summary=summary_text if not violations else None,
            fraud_score=score,
            fraud_state=fraud_state,
        )
        self._store.put_case(case.model_dump(mode="json"))
        self._store.put_summary(
            case.case_id,
            verified=not violations,
            payload={
                "case_id": case.case_id,
                "text": summary_text if not violations else None,
                "violations": violations,
                "profile": self._model.profile_name,
            },
        )
        # The summary is a customer message (R8): it enters the
        # message lifecycle here — verified when the draft passed,
        # refused with its violations listed when it did not. The
        # case-level gate above is unchanged (R1 semantics); the
        # message record is the lifecycle the summary now lives in.
        self._record_summary_message(case, summary_text, violations, as_of=request.requested_on)
        # Memory milestone (R9): the case was decided — the record
        # is written whether or not the summary verified, because
        # the decision happened either way.
        self._memory_case_decided(
            case,
            fact=scored_fact,
            score=score,
            computation=computation,
            disposition=disposition,
        )
        if violations:
            raise FigureVerificationError(violations)
        return case

    def _compose_summary_text(
        self,
        decision,
        computation: RefundComputation,
        product_name: str,
        label: ReturnLabel | None = None,
    ) -> str:
        """The ONE summary composition path (R8): the return_summary
        template's body. With a label, the label composer's text
        (the R6 extended summary); for an eligible case, the refund
        composer's (the R2 text quoting the computation); otherwise
        the R1 composer's. Every surface that (re-)issues a summary
        — ingest, label issuance, grading, message drafting — goes
        through here, so the text can never diverge between them."""
        label_composer = getattr(self._model, "compose_label_summary", None)
        refund_composer = getattr(self._model, "compose_refund_summary", None)
        if label is not None and label_composer is not None:
            return label_composer(decision, product_name, computation, label)
        if refund_composer is not None and decision.eligible:
            return refund_composer(decision, product_name, computation)
        return self._model.compose_summary(decision, product_name)

    def attach_evidence(self, case_id: str, kind: str, filename: str, content: bytes) -> dict:
        if self._evidence is None:
            raise RuntimeError("no evidence store configured")
        record = self._evidence.put(case_id, kind, filename, content)
        self._store.put_evidence(record)
        return record

    # -- computations (R2) ---------------------------------------------------
    def get_computation(self, case_id: str) -> RefundComputation | None:
        """The stored computation; computed+stored on demand for a case
        decided before R2 (its decision and order are still pinned)."""
        stored = self._store.get_computation(case_id)
        if stored is not None:
            return RefundComputation.model_validate(stored)
        case = self.get_case(case_id)
        if case is None or case.decision is None:
            return None
        order = self.get_order(case.order_id)
        if order is None:
            return None
        computation = compute_refund(
            self._pack, order, CATALOGUE.get(case.request.sku), case.request, case.decision
        )
        self._store.put_computation(computation.model_dump(mode="json"))
        return computation

    def verify_stored_computation(self, case_id: str) -> list[str]:
        """Re-perform the stored computation's arithmetic; [] = intact."""
        computation = self.get_computation(case_id)
        case = self.get_case(case_id)
        if computation is None or case is None or case.decision is None:
            return ["no computation or decision for this case"]
        order = self.get_order(case.order_id)
        if order is None:
            return ["order missing for this case"]
        return verify_computation(
            computation, self._pack, order, CATALOGUE.get(case.request.sku), case.request, case.decision
        )

    def list_computations(self) -> list[RefundComputation]:
        return [RefundComputation.model_validate(p) for p in self._store.list_computations()]

    def list_computations_for_case(self, case_id: str) -> list[RefundComputation]:
        """The case's computation chain, oldest first (R7): the
        declared-basis original, then each graded re-run that
        superseded it, in link order."""
        payloads = self._store.list_computations_for_case(case_id)
        return [
            RefundComputation.model_validate(p)
            for p in _chain_order(payloads, "computation_id")
        ]

    # -- dispositions (R3) ---------------------------------------------------
    def _decide_for(
        self, order: Order, product, request: ReturnRequest, computation_id: str | None
    ) -> DispositionDecision:
        age_days = (request.requested_on - order.delivered_on).days
        return decide_disposition(
            self._matrix,
            self._channels,
            case_id=case_id_for(request.request_id),
            computation_id=computation_id,
            category=product.category if product is not None else None,
            condition_declared=request.condition_declared,
            age_days=age_days,
        )

    def get_disposition(self, case_id: str) -> DispositionDecision | None:
        """The stored disposition; decided+stored on demand for an
        eligible case decided before R3 (its facts are still pinned)."""
        stored = self._store.get_disposition(case_id)
        if stored is not None:
            return DispositionDecision.model_validate(stored)
        case = self.get_case(case_id)
        if case is None or case.decision is None or not case.decision.eligible:
            return None
        order = self.get_order(case.order_id)
        if order is None:
            return None
        computation = self.get_computation(case_id)
        disposition = self._decide_for(
            order,
            CATALOGUE.get(case.request.sku),
            case.request,
            computation.computation_id if computation else None,
        )
        self._store.put_disposition(disposition.model_dump(mode="json"))
        return disposition

    def verify_stored_disposition(self, case_id: str) -> list[str]:
        """Re-perform the stored (current) disposition; [] = intact.

        Declared-basis decisions re-run the matrix walk; graded-basis
        decisions (R7) re-run it over the graded facts; an
        inspection-resolution decision (a GR-* cell) is re-performed
        from the grading report it rests on.
        """
        disposition = self.get_disposition(case_id)
        case = self.get_case(case_id)
        if disposition is None or case is None:
            return ["no disposition for this case"]
        order = self.get_order(case.order_id)
        if order is None:
            return ["order missing for this case"]
        if disposition.cell_id is not None and disposition.cell_id.startswith("GR-"):
            report = self.get_grading_report(case_id)
            if report is None:
                return ["no grading report for this inspection-resolution disposition"]
            held = next(
                (
                    d
                    for d in self.list_dispositions_for_case(case_id)
                    if d.disposition_id == disposition.supersedes
                ),
                None,
            )
            if held is None:
                return ["the held decision this resolution supersedes is missing"]
            return verify_inspection_decision(disposition, report=report, held_decision=held)
        product = CATALOGUE.get(case.request.sku)
        return verify_disposition(
            disposition,
            self._matrix,
            self._channels,
            case_id=case_id,
            computation_id=disposition.computation_id,
            category=product.category if product is not None else None,
            condition_declared=disposition.condition_declared,
            age_days=(case.request.requested_on - order.delivered_on).days,
            basis=disposition.basis,
            graded_condition=disposition.graded_condition,
            supersedes=disposition.supersedes,
        )

    def list_dispositions(self) -> list[DispositionDecision]:
        return [DispositionDecision.model_validate(p) for p in self._store.list_dispositions()]

    def list_dispositions_for_case(self, case_id: str) -> list[DispositionDecision]:
        """The case's disposition chain, oldest first (R7): the
        declared-basis decision, then each graded successor that
        superseded it, in link order."""
        payloads = self._store.list_dispositions_for_case(case_id)
        return [
            DispositionDecision.model_validate(p)
            for p in _chain_order(payloads, "disposition_id")
        ]

    def get_matrix(self) -> MatrixPack:
        return self._matrix

    def get_channels(self) -> ChannelTable:
        return self._channels

    # -- fraud scoring (R4) --------------------------------------------------
    def _fact_for(self, order: Order, request: ReturnRequest) -> CaseFact:
        product = CATALOGUE.get(request.sku)
        return CaseFact(
            case_id=case_id_for(request.request_id),
            order_id=order.order_id,
            customer_id=order.customer_id,
            sku=request.sku,
            qty=request.qty,
            category=product.category if product is not None else None,
            condition_declared=request.condition_declared,
            requested_on=request.requested_on,
            delivered_on=order.delivered_on,
            returned_weight_grams=request.returned_weight_grams,
        )

    def _fact_of_case(self, case: ReturnCase) -> CaseFact | None:
        order = self.get_order(case.order_id)
        if order is None:
            return None
        fact = self._fact_for(order, case.request)
        # The warehouse weigh-in (R7) is the weight of record once a
        # grading report states it: it supersedes the intake drop-off
        # scan, so scores taken after grading measure the parcel the
        # warehouse actually weighed. The intake scan stays on the
        # request artefact — the fact derivation is what changes.
        reports = self.list_grading_reports(case.case_id)
        if reports and reports[-1].received_weight_grams is not None:
            fact.returned_weight_grams = reports[-1].received_weight_grams
        return fact

    def _history_from_store(self, scored: CaseFact) -> HistoryFacts:
        """The engine's input bundle from the artefacts of record:
        the scored case's fact plus the derived fact of every other
        stored case of the same customer, and the customer's order
        book. Tagged `source: store` (R9)."""
        facts = [scored]
        for payload in self._store.list_cases():
            other = ReturnCase.model_validate(payload)
            if other.customer_id != scored.customer_id or other.case_id == scored.case_id:
                continue
            fact = self._fact_of_case(other)
            if fact is not None:
                facts.append(fact)
        orders = [
            Order.model_validate(p)
            for p in self._store.list_orders()
            if p["customer_id"] == scored.customer_id
        ]
        return HistoryFacts(source="store", facts=facts, orders=orders)

    def _score_over(self, scored: CaseFact, supersedes: str | None = None) -> FraudScore:
        return score_from_history(
            self._scoring,
            scored=scored,
            history=self._history_from_store(scored),
            weights_grams=PRODUCT_WEIGHTS_GRAMS,
            supersedes=supersedes,
        )

    def _current_score(self, case_id: str) -> FraudScore | None:
        scores = [FraudScore.model_validate(p) for p in self._store.list_fraud_scores(case_id)]
        if not scores:
            return None
        superseded = {s.supersedes for s in scores if s.supersedes}
        current = [s for s in scores if s.score_id not in superseded]
        return current[0] if len(current) == 1 else scores[-1]

    def _reviews_for(self, case_id: str) -> list[FraudReviewDecision]:
        return [FraudReviewDecision.model_validate(p) for p in self._store.list_fraud_reviews(case_id)]

    def _refresh_case_fraud(self, case: ReturnCase) -> ReturnCase:
        """Re-derive the case's fraud fields from the artefacts of
        record (current score + review decisions) and persist."""
        score = self._current_score(case.case_id)
        reviews = self._reviews_for(case.case_id)
        case.fraud_score = score
        case.fraud_review = reviews[-1] if reviews else None
        case.fraud_state = (
            derive_fraud_state(score.band, current_score_id=score.score_id, reviews=reviews)
            if score is not None
            else case.fraud_state
        )
        self._store.put_case(case.model_dump(mode="json"))
        return case

    def get_fraud_score(self, case_id: str) -> FraudScore | None:
        """The case's current score; scored+stored on demand for a
        case ingested before R4 (its inputs are still pinned)."""
        current = self._current_score(case_id)
        if current is not None:
            return current
        case = self.get_case(case_id)
        if case is None:
            return None
        fact = self._fact_of_case(case)
        if fact is None:
            return None
        score = self._score_over(fact)
        self._store.put_fraud_score(score.model_dump(mode="json"))
        self._refresh_case_fraud(case)
        return score

    def rescore_case(self, case_id: str) -> FraudScore | None:
        """Re-score over the store's current artefacts. If nothing
        changed, the same content-addressed artefact comes back and
        nothing new is written; if new evidence landed, the new
        artefact supersedes the old by link — the old one stands."""
        case = self.get_case(case_id)
        if case is None:
            return None
        fact = self._fact_of_case(case)
        if fact is None:
            return None
        current = self._current_score(case_id)
        fresh = self._score_over(fact, supersedes=current.score_id if current else None)
        if current is not None and fresh.score_id == current.score_id:
            return current
        if current is None:
            fresh = self._score_over(fact)
        self._store.put_fraud_score(fresh.model_dump(mode="json"))
        self._refresh_case_fraud(case)
        return fresh

    def verify_stored_fraud_score(self, case_id: str) -> list[str]:
        """Re-perform the current score from its pinned inputs;
        [] = intact. The inputs are re-read by the ids the artefact
        pins — a missing input is itself a named problem."""
        score = self._current_score(case_id)
        case = self.get_case(case_id)
        if score is None or case is None:
            return ["no fraud score for this case"]
        scored = self._fact_of_case(case)
        if scored is None:
            return ["order missing for this case"]
        facts = [scored]
        for pinned_id in score.input_case_ids:
            if pinned_id == case_id:
                continue
            pinned = self.get_case(pinned_id)
            if pinned is None:
                return [f"pinned input case {pinned_id} is missing from the store"]
            fact = self._fact_of_case(pinned)
            if fact is None:
                return [f"order missing for pinned input case {pinned_id}"]
            facts.append(fact)
        orders = []
        for order_id in score.input_order_ids:
            order = self.get_order(order_id)
            if order is None:
                return [f"pinned input order {order_id} is missing from the store"]
            orders.append(order)
        return verify_fraud_score(
            score, self._scoring, scored=scored, facts=facts,
            orders=orders, weights_grams=PRODUCT_WEIGHTS_GRAMS,
        )

    def list_fraud_scores(self) -> list[FraudScore]:
        return [FraudScore.model_validate(p) for p in self._store.list_all_fraud_scores()]

    def get_scoring_pack(self) -> ScoringPack:
        return self._scoring

    # -- customer memory (R9) -------------------------------------------------
    def get_memory_pack(self) -> MemoryPack:
        return self._memory_pack

    def _draft_memory_record(
        self,
        case: ReturnCase,
        event_type: str,
        *,
        occurred_on: date,
        source_ref: str,
        **facts,
    ) -> MemoryRecord:
        """Assemble one milestone record: the pack's retention
        window applied, the writing port's label stamped, the
        content address assigned. Deterministic — the same
        milestone derived from the same artefacts is the same
        record, which is what makes the write idempotent."""
        record = MemoryRecord(
            record_id="",
            customer_id=case.customer_id,
            case_id=case.case_id,
            event_type=event_type,
            occurred_on=occurred_on,
            recorded_on=occurred_on,
            expires_on=occurred_on + timedelta(days=self._memory_pack.retention_days),
            memory_pack_version=self._memory_pack.version,
            provider=self._memory.provider_name,
            source_ref=source_ref,
            **facts,
        )
        return assign_record_id(record)

    def _record_memory(self, record: MemoryRecord) -> bool:
        """Write a milestone record through the memory port. One
        record per (case, event, source artefact): a milestone
        that already stands is left standing — memory is
        append-only, and a re-fired milestone neither duplicates
        nor edits. Returns True when a new record was written."""
        if (
            record.case_id,
            record.event_type,
            record.source_ref,
        ) in self._memory.existing_keys():
            return False
        self._memory.write(record)
        return True

    def _memory_case_decided(
        self,
        case: ReturnCase,
        *,
        fact: CaseFact,
        score: FraudScore,
        computation: RefundComputation,
        disposition: DispositionDecision | None,
    ) -> None:
        """Milestone: the case was decided. The record embeds the
        scoring fact exactly as scored, so the fraud engine's
        history can later be reconstructed from memory alone."""
        self._record_memory(
            self._draft_memory_record(
                case,
                EVENT_CASE_DECIDED,
                occurred_on=case.request.requested_on,
                source_ref=computation.computation_id,
                eligible=case.decision.eligible if case.decision else None,
                refund_total=computation.refund_total,
                disposition=(
                    disposition.disposition
                    if disposition is not None and disposition.status == "decided"
                    else None
                ),
                fraud_band=score.band,
                fact=fact,
            )
        )

    def _memory_refund_executed(self, case: ReturnCase, receipt: RefundReceipt) -> None:
        """Milestone: money moved. One record per receipt — a
        top-up execution is a second record on the same case, its
        source the top-up receipt."""
        self._record_memory(
            self._draft_memory_record(
                case,
                EVENT_REFUND_EXECUTED,
                occurred_on=receipt.executed_on,
                source_ref=receipt.receipt_id,
                refund_amount=receipt.amount,
            )
        )

    def _disposition_by_id(self, case_id: str, disposition_id: str | None):
        if disposition_id is None:
            return None
        for disp in self.list_dispositions_for_case(case_id):
            if disp.disposition_id == disposition_id:
                return disp
        return None

    def sync_memory(self) -> int:
        """Backfill memory from the artefacts of record — the
        milestones of cases that predate the port (the planted
        history above all) derived and written through the same
        builders the live hooks use. Idempotent: a milestone that
        already stands is not rewritten. Returns the number of
        records newly written."""
        written = 0
        for case in self.list_cases():
            decision = case.decision
            computation = self.get_computation(case.case_id)
            disposition = self.get_disposition(case.case_id)
            score = self._current_score(case.case_id)
            fact = self._fact_of_case(case)
            if fact is not None:
                written += self._record_memory(
                    self._draft_memory_record(
                        case,
                        EVENT_CASE_DECIDED,
                        occurred_on=case.request.requested_on,
                        source_ref=(
                            computation.computation_id
                            if computation is not None
                            else f"decision:{case.case_id}"
                        ),
                        eligible=decision.eligible if decision is not None else None,
                        refund_total=(
                            computation.refund_total if computation is not None else None
                        ),
                        disposition=(
                            disposition.disposition
                            if disposition is not None
                            and disposition.status == "decided"
                            else None
                        ),
                        fraud_band=score.band if score is not None else None,
                        fact=fact,
                    )
                )
            for receipt in self._case_receipts(case.case_id):
                written += self._record_memory(
                    self._draft_memory_record(
                        case,
                        EVENT_REFUND_EXECUTED,
                        occurred_on=receipt.executed_on,
                        source_ref=receipt.receipt_id,
                        refund_amount=receipt.amount,
                    )
                )
            for report in self.list_grading_reports(case.case_id):
                new_disp = self._disposition_by_id(case.case_id, report.new_disposition_id)
                current_disp = new_disp or self.get_disposition(case.case_id)
                written += self._record_memory(
                    self._draft_memory_record(
                        case,
                        EVENT_CASE_GRADED,
                        occurred_on=report.graded_on,
                        source_ref=report.report_id,
                        graded_condition=report.graded_condition,
                        received_weight_grams=report.received_weight_grams,
                        refund_delta=report.refund_delta,
                        disposition=(
                            current_disp.disposition
                            if current_disp is not None
                            and current_disp.status == "decided"
                            else None
                        ),
                    )
                )
            for review in self._reviews_for(case.case_id):
                decided_payload = self._store.get_fraud_score(review.score_id)
                decided_score = (
                    FraudScore.model_validate(decided_payload) if decided_payload else None
                )
                written += self._record_memory(
                    self._draft_memory_record(
                        case,
                        EVENT_FRAUD_REVIEW_CLOSED,
                        occurred_on=(
                            decided_score.as_of
                            if decided_score is not None
                            else case.request.requested_on
                        ),
                        source_ref=review.review_id,
                        fraud_band=decided_score.band if decided_score else None,
                        review_outcome=review.outcome,
                    )
                )
        return written

    def retrieve_memory(
        self,
        customer_id: str,
        *,
        as_of: date | None = None,
        purpose: str = "staff_read",
        actor: str = "operations",
    ) -> list[MemoryRecord]:
        """The customer's live memory records — through the port,
        so the read is customer-scoped, retention-filtered and on
        the access log. There is no unlogged read path."""
        return self._memory.retrieve(customer_id, purpose=purpose, actor=actor, as_of=as_of)

    def memory_retention_status(
        self, customer_id: str, *, as_of: date | None = None, actor: str = "operations"
    ) -> dict:
        """The tombstone read: live vs retention-expired counts
        for one customer. Expired records are counted, never
        deleted."""
        return self._memory.retention_status(customer_id, actor=actor, as_of=as_of)

    def list_memory_access_log(
        self, customer_id: str | None = None
    ) -> list[MemoryAccessEntry]:
        """The access log — every retrieval the port has served,
        in append order, optionally for one customer."""
        return [
            MemoryAccessEntry.model_validate(p)
            for p in self._store.list_memory_access(customer_id)
        ]

    def has_customer(self, customer_id: str) -> bool:
        """Whether any order or case names this customer — the
        existence check the memory reads hang their 404 on."""
        return any(c.customer_id == customer_id for c in self.list_cases()) or any(
            o.customer_id == customer_id for o in self.list_orders()
        )

    def score_case_from_memory(self, case_id: str) -> FraudScore | None:
        """Score a case with its history read from MEMORY (R9) —
        the fraud engine's second input path.

        The fact set is reconstructed from the customer's memory
        records alone: each case's decision record embeds the fact
        as scored, and a later grading record's warehouse weigh-in
        supersedes the intake weight, exactly as the store path
        derives it. The order book is read as usual (memory holds
        case history, not orders). The returned artefact is tagged
        `history_source: memory` and is NOT stored — the store path
        remains the scoring path of record; this path exists so
        parity is provable, and so a deployment can flip the input
        source without the engine changing. Returns None when
        memory holds no decision record for the case (sync first).
        """
        case = self.get_case(case_id)
        if case is None:
            return None
        # Read as of the customer's latest recorded event — the
        # current state of record, exactly what the store path
        # derives from. The engine itself time-filters the facts
        # it consumes, so later records cannot leak backwards.
        records = self._memory.retrieve(
            case.customer_id,
            purpose="fraud_scoring",
            actor="fraud engine (memory input)",
            as_of=None,
        )
        decided: dict[str, MemoryRecord] = {}
        graded: dict[str, MemoryRecord] = {}
        for record in records:
            if record.event_type == EVENT_CASE_DECIDED:
                decided[record.case_id] = record
            elif record.event_type == EVENT_CASE_GRADED:
                graded[record.case_id] = record

        def _fact_from_records(cid: str) -> CaseFact | None:
            base = decided.get(cid)
            if base is None or base.fact is None:
                return None
            fact = base.fact.model_copy()
            weigh_in = graded.get(cid)
            if weigh_in is not None and weigh_in.received_weight_grams is not None:
                fact.returned_weight_grams = weigh_in.received_weight_grams
            return fact

        facts_by_case: dict[str, CaseFact] = {}
        for cid in decided:
            fact = _fact_from_records(cid)
            if fact is not None:
                facts_by_case[cid] = fact
        scored = facts_by_case.get(case_id)
        if scored is None:
            return None
        orders = [
            Order.model_validate(p)
            for p in self._store.list_orders()
            if p["customer_id"] == case.customer_id
        ]
        return score_from_history(
            self._scoring,
            scored=scored,
            history=HistoryFacts(
                source="memory", facts=list(facts_by_case.values()), orders=orders
            ),
            weights_grams=PRODUCT_WEIGHTS_GRAMS,
        )

    # -- the fraud block + the review act -------------------------------------
    def refund_path_blocked(self, case_id: str) -> bool:
        case = self.get_case(case_id)
        if case is None:
            return False
        return case.fraud_state in BLOCKED_STATES

    def assert_refund_path_open(self, case_id: str) -> None:
        """The guard every money-moving surface (R5 onward) must call
        before proceeding: a blocked case refuses here, in code."""
        case = self.get_case(case_id)
        if case is not None and case.fraud_state in BLOCKED_STATES:
            raise FraudHoldError(
                f"case {case_id} is under fraud hold (state={case.fraud_state}); "
                "only a named fraud-review decision can change that"
            )

    def record_fraud_review(
        self, case_id: str, *, reviewer: str, outcome: str, reason_code: str
    ) -> FraudReviewDecision:
        """The decision act — the ONLY way a fraud block is lifted or
        confirmed. Refusals: case not in FRAUD_REVIEW, a decision
        already recorded on the current score, an unnamed reviewer,
        or an outcome/reason outside the pack's vocabulary. The
        score artefact is never modified by this act."""
        case = self.get_case(case_id)
        if case is None:
            raise LookupError(f"unknown case: {case_id}")
        reviewer = (reviewer or "").strip()
        if not reviewer:
            raise FraudReviewError("a fraud-review decision requires a named reviewer")
        score = self.get_fraud_score(case_id)
        if score is None or score.band != "FRAUD_REVIEW":
            raise FraudReviewError(f"case {case_id} is not in fraud review")
        if self._store.get_fraud_review(score.score_id) is not None:
            raise FraudReviewError(f"score {score.score_id} already has a review decision")
        reasons = self._scoring.review_outcomes.get(outcome)
        if reasons is None:
            raise FraudReviewError(
                f"outcome {outcome!r} is not in the scoring pack's review vocabulary"
            )
        if reason_code not in reasons:
            raise FraudReviewError(
                f"reason {reason_code!r} is not a coded reason for outcome {outcome!r}"
            )
        blob = json.dumps(
            {
                "case_id": case_id,
                "score_id": score.score_id,
                "reviewer": reviewer,
                "outcome": outcome,
                "reason_code": reason_code,
                "pack_version": self._scoring.version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        decision = FraudReviewDecision(
            review_id="FR-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            case_id=case_id,
            score_id=score.score_id,
            reviewer=reviewer,
            outcome=outcome,
            reason_code=reason_code,
            pack_version=self._scoring.version,
        )
        self._store.put_fraud_review(decision.model_dump(mode="json"))
        # Memory milestone (R9): the fraud review closed. The
        # record carries the outcome and the decided score's BAND
        # — never the score's internals.
        self._record_memory(
            self._draft_memory_record(
                case,
                EVENT_FRAUD_REVIEW_CLOSED,
                occurred_on=score.as_of,
                source_ref=decision.review_id,
                fraud_band=score.band,
                review_outcome=decision.outcome,
            )
        )
        self._refresh_case_fraud(case)
        return decision

    def get_fraud_review(self, case_id: str) -> FraudReviewDecision | None:
        reviews = self._reviews_for(case_id)
        return reviews[-1] if reviews else None

    # -- refund approvals (R5) ----------------------------------------------
    def list_approvals(self, case_id: str) -> list[RefundApproval]:
        """Every approval act of record for the case, oldest first —
        including any pinned to a superseded computation (they stand
        on the record; the gates simply do not count them)."""
        approvals = [RefundApproval.model_validate(p) for p in self._store.list_approvals(case_id)]
        return sorted(approvals, key=lambda a: (a.as_of, a.approval_id))

    def _approvals_for(self, case_id: str, computation_id: str) -> list[RefundApproval]:
        """The approvals that count: pinned to THIS computation."""
        return [a for a in self.list_approvals(case_id) if a.computation_id == computation_id]

    def approval_status(self, case_id: str) -> dict:
        """The case's approval position, computed from the pack caps
        and the approvals pinned to the current computation."""
        case = self.get_case(case_id)
        if case is None:
            raise LookupError(f"unknown case: {case_id}")
        computation = self.get_computation(case_id)
        if computation is None or computation.refund_total is None:
            return {
                "case_id": case_id,
                "computation_id": computation.computation_id if computation else None,
                "refund_total": None,
                "tier": None,
                "approvals_required": None,
                "approvers": [],
                "satisfied": False,
                "missing": "no final refund computation to approve",
            }
        tier = approval_tier(self._pack, computation.refund_total)
        required = APPROVERS_REQUIRED[tier]
        approvers = [a.approver for a in self._approvals_for(case_id, computation.computation_id)]
        satisfied = len(set(approvers)) >= required
        if satisfied:
            missing = None
        elif required == 0:
            missing = None
        elif not approvers:
            missing = (
                f"tier {tier} (refund ${computation.refund_total}) requires "
                f"{required} named approval(s); none recorded"
            )
        else:
            missing = (
                f"tier {tier} (refund ${computation.refund_total}) requires a second, "
                "different approver; only "
                + ", ".join(sorted(set(approvers)))
                + " recorded"
            )
        return {
            "case_id": case_id,
            "computation_id": computation.computation_id,
            "refund_total": str(computation.refund_total),
            "tier": tier,
            "approvals_required": required,
            "approvers": approvers,
            "satisfied": satisfied,
            "missing": missing,
        }

    def record_approval(
        self,
        case_id: str,
        *,
        approver: str,
        role: str,
        as_of: date,
        computation_id: str | None = None,
    ) -> RefundApproval:
        """The approval act. Refusals, each naming its reason: an
        unnamed approver or role; the customer approving their own
        return; a fraud-blocked case (the R4 guard — approvals never
        override it); a stale computation (the act must pin the
        case's CURRENT computation); a case with no refund due; a
        computation that is not final; a second act by the same
        approver on the same computation (the dual tier's second
        approval must be a different person); a refund already
        executed."""
        case = self.get_case(case_id)
        if case is None:
            raise LookupError(f"unknown case: {case_id}")
        approver = (approver or "").strip()
        if not approver:
            raise ApprovalError("a refund approval requires a named approver")
        role = (role or "").strip()
        if not role:
            raise ApprovalError("a refund approval requires the approver's role")
        if approver == case.customer_id:
            raise ApprovalError(
                "the customer who submitted the return cannot approve its refund"
            )
        # Law 4 over Law 5: a fraud hold refuses approvals too —
        # through the R4 guard, not a reimplementation of it.
        self.assert_refund_path_open(case_id)
        computation = self.get_computation(case_id)
        if computation is None:
            raise ApprovalError(f"case {case_id} has no refund computation to approve")
        if computation_id is not None and computation_id != computation.computation_id:
            raise ApprovalError(
                f"stale computation: {computation_id} is not the case's current "
                f"computation ({computation.computation_id}); an approval must pin "
                "the current computation"
            )
        if not computation.eligible:
            raise ApprovalError(
                "no refund is due on this case (eligibility denied); there is nothing to approve"
            )
        if not computation.is_final or computation.refund_total is None:
            raise ApprovalError(
                "the refund computation is not final (a component is not computable); "
                "a partial computation cannot be approved"
            )
        if self._store.get_receipt_by_computation(computation.computation_id) is not None:
            raise ApprovalError("this refund has already been executed; nothing remains to approve")
        if any(
            a.approver == approver
            for a in self._approvals_for(case_id, computation.computation_id)
        ):
            raise ApprovalError(
                f"{approver} has already approved computation {computation.computation_id}; "
                "a further approval must come from a different approver"
            )
        tier = approval_tier(self._pack, computation.refund_total)
        blob = json.dumps(
            {
                "case_id": case_id,
                "computation_id": computation.computation_id,
                "approver": approver,
                "role": role,
                "as_of": as_of.isoformat(),
                "amount": str(computation.refund_total),
                "policy_pack_version": self._pack.version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        approval = RefundApproval(
            approval_id="RA-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            case_id=case_id,
            computation_id=computation.computation_id,
            approver=approver,
            role=role,
            as_of=as_of,
            amount=computation.refund_total,
            tier=tier,
            policy_pack_version=self._pack.version,
        )
        self._store.put_approval(approval.model_dump(mode="json"))
        return approval

    # -- refund execution (R5; the port call rides the gateway since R11) -----
    def _execute_via_gateway(
        self,
        *,
        case_id: str,
        computation_id: str,
        amount: Decimal,
        tender_allocations: list[TenderAllocation],
        idempotency_key: str,
        as_of: date,
    ) -> ProviderReceipt:
        """The payments port call, through the gateway as the
        execute_refund tool (R11). The execution record already
        stands in 'executing' (prepared by the caller, after its
        own gates passed); the gateway evaluates the policy pack,
        records the invocation, and the handler verifies the
        prepared record before the port is called. A policy denial
        propagates as PolicyDeniedError — the callers record the
        execution as failed on it, exactly as on a provider
        failure: no receipt, no money moved.
        """
        arguments = {
            "case_id": case_id,
            "computation_id": computation_id,
            "amount": str(money(amount)),
            "tender_allocations": [a.model_dump(mode="json") for a in tender_allocations],
            "idempotency_key": idempotency_key,
        }
        _record, receipt = self._gateway.invoke(
            "execute_refund",
            arguments,
            case_id=case_id,
            called_on=as_of,
            actor=ACTOR_STAFF,
        )
        return receipt

    def execute_refund(
        self, case_id: str, *, idempotency_key: str, as_of: date
    ) -> RefundReceipt:
        """Execute the case's refund through the payments port.

        Idempotent by key: the same key returns the original receipt
        and moves money once, however often it is retried. A
        different key for an already-executed computation is refused
        as duplicate_refund. Preconditions, each refusal naming the
        gap: a final, eligible computation; the fraud path open (the
        R4 guard); a decided disposition; the approval tier
        satisfied. A provider failure records a failed execution —
        no receipt, no partial state — and the case stays executable.
        """
        key = (idempotency_key or "").strip()
        if not key:
            raise PaymentsError("an idempotency key is mandatory on every refund execution")
        case = self.get_case(case_id)
        if case is None:
            raise LookupError(f"unknown case: {case_id}")
        computation = self.get_computation(case_id)

        # Idempotent replay: this key already moved the money.
        replayed = self._store.get_receipt_by_key(key)
        if replayed is not None:
            receipt = RefundReceipt.model_validate(replayed)
            if receipt.case_id != case_id:
                raise PaymentsError(
                    f"idempotency key {key!r} was already used for case "
                    f"{receipt.case_id} / computation {receipt.computation_id}; "
                    "a key names one execution"
                )
            # The key names THIS case's execution, even if grading
            # (R7) has since superseded the computation it settled:
            # the replay returns the original receipt, and no money
            # moves again.
            return receipt

        if computation is None:
            raise PaymentsError(f"case {case_id} has no refund computation to execute")
        self.assert_refund_path_open(case_id)
        if not computation.eligible:
            raise PaymentsError(
                "no refund is due on this case (eligibility denied); nothing to execute"
            )
        if not computation.is_final or computation.refund_total is None:
            raise PaymentsError(
                "the refund computation is not final (a component is not computable); "
                "a partial computation cannot be executed"
            )
        disposition = self.get_disposition(case_id)
        if disposition is None:
            raise PaymentsError(f"case {case_id} has no disposition decision; execution refused")
        if disposition.status != "decided":
            raise PaymentsError(
                f"case {case_id}'s disposition is not decided "
                f"(missing dimension: {disposition.missing_dimension}); execution refused"
            )
        status = self.approval_status(case_id)
        if not status["satisfied"]:
            raise ApprovalError(f"approval gate unmet: {status['missing']}")
        already = self._store.get_receipt_by_computation(computation.computation_id)
        if already is not None:
            raise DuplicateRefundError(
                f"duplicate_refund: computation {computation.computation_id} already "
                f"executed under idempotency key {already['idempotency_key']!r} "
                f"(receipt {already['receipt_id']}); a second execution is refused"
            )
        settled_elsewhere = [r for r in self.list_receipts() if r.case_id == case_id]
        if settled_elsewhere:
            # The case's money already moved — against a computation
            # grading has since superseded (R7). Executing the new
            # computation in full would pay the settled part twice;
            # the difference belongs to the grading adjustment path.
            raise PaymentsError(
                f"case {case_id}'s refund already executed against a superseded "
                f"computation (receipt {settled_elsewhere[0].receipt_id}); the graded "
                "difference is settled through the grading adjustment (execute_top_up "
                "for a top-up, a recorded recovery position for an over-refund) — "
                "never by re-executing the full computation"
            )

        # The execution record: created on first attempt for this
        # key, continued on retries — its transitions are the
        # requested -> executing -> executed/failed trail.
        stored_execution = self._store.get_execution_by_key(key)
        if stored_execution is not None:
            execution = RefundExecution.model_validate(stored_execution)
            if execution.case_id != case_id or execution.computation_id != computation.computation_id:
                raise PaymentsError(
                    f"idempotency key {key!r} already names an execution for case "
                    f"{execution.case_id} / computation {execution.computation_id}"
                )
        else:
            blob = json.dumps(
                {
                    "case_id": case_id,
                    "computation_id": computation.computation_id,
                    "idempotency_key": key,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            execution = RefundExecution(
                execution_id="RX-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
                case_id=case_id,
                computation_id=computation.computation_id,
                idempotency_key=key,
                amount=computation.refund_total,
                provider=self._payments.provider_name,
                status="requested",
                transitions=[
                    ExecutionTransition(state="requested", at=as_of, detail="execution requested")
                ],
            )
        execution.status = "executing"
        execution.failure_reason = None
        execution.transitions.append(
            ExecutionTransition(state="executing", at=as_of, detail="payments port called")
        )
        self._store.put_execution(execution.model_dump(mode="json"))

        try:
            provider_receipt = self._execute_via_gateway(
                case_id=case_id,
                computation_id=computation.computation_id,
                amount=computation.refund_total,
                tender_allocations=computation.tender_allocations,
                idempotency_key=key,
                as_of=as_of,
            )
        except PolicyDeniedError as exc:
            execution.status = "failed"
            execution.failure_reason = str(exc)
            execution.transitions.append(
                ExecutionTransition(state="failed", at=as_of, detail=str(exc))
            )
            self._store.put_execution(execution.model_dump(mode="json"))
            raise PaymentsError(
                f"gateway policy denied the refund execution for case {case_id}: {exc} "
                "No receipt was written; the case stays executable."
            ) from exc
        except PaymentsProviderError as exc:
            execution.status = "failed"
            execution.failure_reason = str(exc)
            execution.transitions.append(
                ExecutionTransition(state="failed", at=as_of, detail=str(exc))
            )
            self._store.put_execution(execution.model_dump(mode="json"))
            raise PaymentsError(
                f"payments provider failed for case {case_id}: {exc} "
                "No receipt was written; the case stays executable."
            ) from exc

        blob = json.dumps(
            {
                "case_id": case_id,
                "computation_id": computation.computation_id,
                "idempotency_key": key,
                "provider_ref": provider_receipt.provider_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        receipt = RefundReceipt(
            receipt_id="PR-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            case_id=case_id,
            computation_id=computation.computation_id,
            execution_id=execution.execution_id,
            idempotency_key=key,
            provider=provider_receipt.provider,
            provider_ref=provider_receipt.provider_ref,
            amount=computation.refund_total,
            tender_allocations=computation.tender_allocations,
            status="executed",
            executed_on=as_of,
        )
        self._store.put_receipt(receipt.model_dump(mode="json"))
        execution.status = "executed"
        execution.receipt_id = receipt.receipt_id
        execution.transitions.append(
            ExecutionTransition(
                state="executed", at=as_of, detail=f"receipt {receipt.receipt_id}"
            )
        )
        self._store.put_execution(execution.model_dump(mode="json"))
        # Memory milestone (R9): the refund executed — written
        # once, on the fresh execution only (an idempotent replay
        # returns above and never reaches here).
        self._memory_refund_executed(case, receipt)
        return receipt

    def get_receipt(self, case_id: str) -> RefundReceipt | None:
        payload = self._store.get_receipt_for_case(case_id)
        return RefundReceipt.model_validate(payload) if payload else None

    def list_receipts(self) -> list[RefundReceipt]:
        return [RefundReceipt.model_validate(p) for p in self._store.list_receipts()]

    def list_executions(self, case_id: str) -> list[RefundExecution]:
        return [RefundExecution.model_validate(p) for p in self._store.list_executions(case_id)]

    def get_execution_by_key(self, idempotency_key: str) -> RefundExecution | None:
        payload = self._store.get_execution_by_key(idempotency_key)
        return RefundExecution.model_validate(payload) if payload else None

    # -- the gateway: tools, policy, invocation log (R6 + R11) ----------------
    def tool_definitions(self) -> list[ToolDefinition]:
        """The gateway's tool list, as the gateway itself states it."""
        return self._gateway.definitions()

    def get_gateway_policy_pack(self) -> GatewayPolicyPack:
        """The gateway policy pack of record — the data the gateway
        evaluates in front of every tool invocation."""
        return self._gateway_policy

    def _gateway_facts(self, tool: str, case_id: str, arguments: dict) -> ToolFacts:
        """The facts a policy evaluation reads, built from the
        artefacts of record for the case and the invocation's own
        arguments. A fact that cannot be established stays None —
        and a required fact that is None denies (fail-closed)."""
        case = self.get_case(case_id)
        facts = ToolFacts(case_exists=case is not None)
        if case is not None:
            facts.eligible = case.decision.eligible if case.decision is not None else None
            facts.fraud_blocked = self.refund_path_blocked(case_id)
            disposition = self.get_disposition(case_id)
            facts.disposition_decided = (
                disposition.status == "decided" if disposition is not None else None
            )
        if tool == "execute_refund":
            computation_id = arguments.get("computation_id")
            payload = next(
                (
                    p
                    for p in self._store.list_computations()
                    if p["computation_id"] == computation_id
                ),
                None,
            )
            if payload is not None:
                computation = RefundComputation.model_validate(payload)
                facts.computed_amount = computation.refund_total
                if computation.refund_total is not None:
                    tier = approval_tier(self._pack, computation.refund_total)
                    approvers = {
                        a.approver
                        for a in self._approvals_for(case_id, computation.computation_id)
                    }
                    facts.approval_satisfied = len(approvers) >= APPROVERS_REQUIRED[tier]
                existing = self._store.get_receipt_by_computation(computation.computation_id)
                facts.computation_executed = (
                    existing is not None
                    and existing["idempotency_key"] != arguments.get("idempotency_key")
                )
            raw_amount = arguments.get("amount")
            if raw_amount is not None:
                try:
                    facts.amount = money(Decimal(str(raw_amount)))
                except Exception:
                    facts.amount = None
        return facts

    def _evaluate_gateway_policy(
        self, tool: str, actor: str, case_id: str, arguments: dict
    ) -> PolicyDenial | None:
        """The gateway's policy hook: the pack's denial of record
        for this invocation, or None when it is permitted."""
        return evaluate_gateway_policy(
            self._gateway_policy,
            tool=tool,
            actor=actor,
            facts=self._gateway_facts(tool, case_id, arguments),
        )

    def invoke_tool(
        self, tool_name: str, arguments: dict, *, case_id: str, called_on: date,
        actor: str = ACTOR_STAFF,
    ):
        """The one tool-invocation path every surface shares. The
        gateway evaluates the policy pack first (a denial raises
        PolicyDeniedError and nothing executes), records the call —
        success, refusal or denial — and returns the tool's result;
        refusals raise with their named reason."""
        _record, result = self._gateway.invoke(
            tool_name, arguments, case_id=case_id, called_on=called_on, actor=actor
        )
        return result

    def invoke_tool_envelope(
        self, tool_name: str, arguments: dict, *, case_id: str, called_on: date,
        actor: str = ACTOR_STAFF,
    ) -> GatewayResponse:
        """The same invocation through the port's envelope: the
        outcome — result, denial or refusal — arrives in the
        response, AgentCore-shaped, instead of raising."""
        return self._gateway.invoke_envelope(
            GatewayRequest(
                tool=tool_name,
                arguments=arguments,
                case_id=case_id,
                actor=actor,
                called_on=called_on,
            )
        )

    def list_tool_calls(self, case_id: str) -> list[ToolCallRecord]:
        calls = [ToolCallRecord.model_validate(p) for p in self._store.list_tool_calls(case_id)]
        return sorted(calls, key=lambda c: (c.called_on, c.call_id))

    # -- label reads ---------------------------------------------------------
    def list_labels(self, case_id: str) -> list[ReturnLabel]:
        labels = [ReturnLabel.model_validate(p) for p in self._store.list_labels(case_id)]
        return sorted(labels, key=lambda label: (label.issued_on, label.label_id))

    def list_all_labels(self) -> list[ReturnLabel]:
        return [ReturnLabel.model_validate(p) for p in self._store.list_all_labels()]

    def get_label(self, label_id: str) -> ReturnLabel | None:
        payload = self._store.get_label(label_id)
        return ReturnLabel.model_validate(payload) if payload else None

    def get_label_for_case(self, case_id: str) -> ReturnLabel | None:
        """The case's most recent label artefact, whatever its status."""
        labels = self.list_labels(case_id)
        return labels[-1] if labels else None

    def get_active_label(self, case_id: str) -> ReturnLabel | None:
        """The case's active (issued, not voided) label, if any."""
        for label in reversed(self.list_labels(case_id)):
            if label.status == "issued":
                return label
        return None

    def list_voids(self, case_id: str) -> list[LabelVoid]:
        return [LabelVoid.model_validate(p) for p in self._store.list_voids(case_id)]

    def list_pickups(self, case_id: str) -> list[PickupBooking]:
        pickups = [PickupBooking.model_validate(p) for p in self._store.list_pickups(case_id)]
        return sorted(pickups, key=lambda p: (p.scheduled_on, p.pickup_id))

    def get_carrier_events(self, label_id: str, as_of: date) -> list[CarrierEvent]:
        """A label's tracking events as of a date, read directly from
        the carrier port (the recorded tool path is
        `get_carrier_events` via invoke_tool; the timeline reads
        through this same helper without minting tool calls)."""
        label = self.get_label(label_id)
        if label is None:
            raise LookupError(f"unknown label: {label_id}")
        return self._carrier_events_for(label, as_of)

    def _carrier_events_for(self, label: ReturnLabel, as_of: date) -> list[CarrierEvent]:
        voided = self._store.get_void_by_label(label.label_id) is not None
        return self._carrier.fetch_events(
            carrier_ref=label.carrier_ref,
            label_id=label.label_id,
            issued_on=label.issued_on,
            as_of=as_of,
            voided=voided,
        )

    # -- the carrier schedule + cost reconciliation --------------------------
    def _label_schedule(self, category: str, weight_grams: int) -> tuple[str, Decimal]:
        """Service level + cost for a parcel, from the pack's carrier
        schedule: freight at or above the pack's weight threshold,
        otherwise the category's stated level. A category or level
        the schedule does not cover is named, never defaulted."""
        pack = self._pack
        if pack.label_freight_weight_grams and weight_grams >= pack.label_freight_weight_grams:
            level = "freight"
        else:
            level = pack.label_service_by_category.get(category)
            if level is None:
                raise CarrierError(
                    f"the policy pack's carrier schedule has no service level for category "
                    f"{category!r} (missing input: label_service_by_category[{category}]); "
                    "no label can be scheduled"
                )
        raw_cost = pack.label_costs.get(level)
        if raw_cost is None:
            raise CarrierError(
                f"the policy pack's carrier schedule has no cost for service level "
                f"{level!r} (missing input: label_costs[{level}]); no label can be priced"
            )
        return level, money(Decimal(raw_cost))

    def _reconcile_label_cost(
        self, case: ReturnCase, computation: RefundComputation | None, label_cost: Decimal
    ) -> LabelCostReconciliation:
        """The label's cost against the computation's return-shipping
        deduction. A difference is classified and stated on the
        artefact — matched, the pack's stated free-return intent
        (retailer_absorbed), an open variance, or honestly
        not_reconcilable when the computation's shipping line names
        a missing input instead of an amount."""
        if computation is None:
            return LabelCostReconciliation(
                label_cost=label_cost,
                status="not_reconcilable",
                detail="no refund computation of record to reconcile the label cost against",
            )
        line = next((ln for ln in computation.lines if ln.line_id == "return_shipping"), None)
        if line is None or line.status != "computed" or line.amount is None:
            missing = line.missing_input if line is not None and line.missing_input else "return_shipping"
            return LabelCostReconciliation(
                label_cost=label_cost,
                status="not_reconcilable",
                detail=(
                    "the computation's return-shipping line is not computable "
                    f"(missing input: {missing}); there is no deduction to reconcile against"
                ),
            )
        deducted = line.amount
        variance = money(label_cost - deducted)
        product = CATALOGUE.get(case.request.sku)
        category = product.category if product is not None else None
        if variance == 0:
            return LabelCostReconciliation(
                deducted_amount=deducted,
                label_cost=label_cost,
                variance=variance,
                status="matched",
                detail="the label cost equals the computation's return-shipping deduction",
            )
        if deducted == 0 and category in self._pack.free_return_categories:
            return LabelCostReconciliation(
                deducted_amount=deducted,
                label_cost=label_cost,
                variance=variance,
                status="retailer_absorbed",
                detail=(
                    f"{category} is a free-return category in the pack: the customer was "
                    f"deducted $0.00 and the retailer carries the ${label_cost} label cost. "
                    "The variance is the pack's stated intent — stated here, not absorbed silently"
                ),
            )
        return LabelCostReconciliation(
            deducted_amount=deducted,
            label_cost=label_cost,
            variance=variance,
            status="variance",
            detail=(
                f"the carrier charges ${label_cost} for this label but the computation "
                f"deducted ${deducted}; the ${variance} variance stands on the record"
            ),
        )

    # -- tool handlers (invoked ONLY through the gateway) ---------------------
    def _tool_execute_refund(
        self,
        *,
        call_id: str,
        case_id: str,
        computation_id: str,
        amount: str,
        tender_allocations: list,
        idempotency_key: str,
    ) -> ProviderReceipt:
        """execute_refund: the payments port call, and nothing else.

        The gateway policy pack has already evaluated the call.
        What remains is the prepared-execution contract: this tool
        moves money ONLY against an execution record the service's
        execution flow prepared — its gates passed, the record
        stands in 'executing', and its amount is the amount this
        call names. An invocation without a prepared record, or
        naming a different case, computation or amount, is refused
        here: there is no path from a bare tool call to the
        payments port that skips the governed flow.
        """
        stored = self._store.get_execution_by_key(idempotency_key)
        if stored is None:
            raise PaymentsError(
                "execute_refund names no prepared execution: refund money moves only "
                "through the service's execution flow, which prepares an execution "
                "record after its gates pass"
            )
        execution = RefundExecution.model_validate(stored)
        if execution.case_id != case_id or execution.computation_id != computation_id:
            raise PaymentsError(
                f"the prepared execution for key {idempotency_key!r} names case "
                f"{execution.case_id} / computation {execution.computation_id}, not "
                f"case {case_id} / computation {computation_id}"
            )
        if execution.status != "executing":
            raise PaymentsError(
                f"the prepared execution {execution.execution_id} stands at "
                f"{execution.status!r}, not 'executing'; the port is not called"
            )
        if money(execution.amount) != money(Decimal(str(amount))):
            raise PaymentsError(
                f"the amount argument {amount} does not equal the prepared "
                f"execution's amount {execution.amount}; the port is not called"
            )
        return self._payments.execute_refund(
            case_id=case_id,
            computation_id=computation_id,
            amount=money(Decimal(str(amount))),
            tender_allocations=[
                TenderAllocation.model_validate(a) for a in tender_allocations
            ],
            idempotency_key=idempotency_key,
        )

    def _tool_submit_grading_report(
        self,
        *,
        call_id: str,
        case_id: str,
        grader: str,
        graded_condition: str,
        graded_on: date,
        functional_test_result: str | None = None,
        data_wipe_status: str | None = None,
        damage_notes: str = "",
        received_weight_grams: int | None = None,
        wear_evidence: bool = False,
    ) -> GradingReport:
        """submit_grading_report: the warehouse's grading callback.
        The grading act itself — gates and cascade — is
        _record_grading_impl; this handler is its gateway face."""
        return self._record_grading_impl(
            case_id,
            grader=grader,
            graded_condition=graded_condition,
            graded_on=graded_on,
            functional_test_result=functional_test_result,
            data_wipe_status=data_wipe_status,
            damage_notes=damage_notes,
            received_weight_grams=received_weight_grams,
            wear_evidence=wear_evidence,
        )

    def _tool_create_label(self, *, call_id: str, case_id: str, issued_on: date) -> ReturnLabel:
        """create_return_label: issue the case's return label.

        Preconditions, each refusal naming the gap: the case exists;
        its fraud path is open (the R4 guard — a label moves the
        case forward, so a blocked case refuses here); the decision
        is eligible; the product's shipped weight is of record (the
        schedule prices by category and weight). Idempotent per
        case: a case with an active label gets that label back —
        no second artefact, no second carrier issue. The extended
        customer summary (label reference + cost) is verified
        against the artefacts BEFORE anything is stored, so an
        unverified summary can never ride along with a label.
        """
        case = self.get_case(case_id)
        if case is None:
            raise LookupError(f"unknown case: {case_id}")
        self.assert_refund_path_open(case_id)
        decision = case.decision
        if decision is None or not decision.eligible:
            denied = (
                ", ".join(c.clause_id for c in decision.clauses if c.effect == "deny")
                if decision is not None
                else "no decision of record"
            )
            raise CarrierError(
                f"case {case_id} is not eligible for a return label (denied: {denied})"
            )
        active = self.get_active_label(case_id)
        if active is not None:
            return active
        product = CATALOGUE.get(case.request.sku)
        if product is None:
            raise CarrierError(
                f"case {case_id}'s sku {case.request.sku} is not in the catalogue; "
                "the carrier schedule cannot be applied"
            )
        unit_weight = PRODUCT_WEIGHTS_GRAMS.get(case.request.sku)
        if unit_weight is None:
            raise CarrierError(
                f"no shipped weight of record for sku {case.request.sku} "
                "(missing input: product_ship_weight_grams); a label cannot be scheduled"
            )
        weight_grams = unit_weight * case.request.qty
        service_level, cost = self._label_schedule(product.category, weight_grams)
        prior = self.list_labels(case_id)
        request_ref = f"{case_id}:label:{len(prior) + 1}"
        result = self._carrier.create_label(
            case_id=case_id,
            request_ref=request_ref,
            service_level=service_level,
            category=product.category,
            weight_grams=weight_grams,
            cost=cost,
            issued_on=issued_on,
        )
        computation = self.get_computation(case_id)
        blob = json.dumps(
            {
                "case_id": case_id,
                "carrier_ref": result.carrier_ref,
                "request_ref": request_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        label = ReturnLabel(
            label_id="RL-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            case_id=case_id,
            provider=result.provider,
            carrier_ref=result.carrier_ref,
            service_level=service_level,
            category=product.category,
            weight_grams=weight_grams,
            cost=cost,
            reconciliation=self._reconcile_label_cost(case, computation, cost),
            status="issued",
            issued_on=issued_on,
            policy_pack_version=self._pack.version,
            via_call_id=call_id,
            supersedes=prior[-1].label_id if prior else None,
        )
        # The summary gains the label reference once issued — and is
        # re-verified against the artefacts (the label's cost is
        # sayable only as the label states it) before it is stored.
        label_composer = getattr(self._model, "compose_label_summary", None)
        summary_text: str | None = None
        if case.summary is not None and computation is not None and label_composer is not None:
            text = self._compose_summary_text(decision, computation, product.name, label=label)
            violations = verify_summary(text, decision, computation, label)
            if violations:
                raise FigureVerificationError(violations)
            case.summary = text
            summary_text = text
            self._store.put_case(case.model_dump(mode="json"))
            self._store.put_summary(
                case.case_id,
                verified=True,
                payload={
                    "case_id": case.case_id,
                    "text": text,
                    "violations": [],
                    "profile": self._model.profile_name,
                },
            )
        self._store.put_label(label.model_dump(mode="json"))
        if summary_text is not None:
            # The re-issued summary enters the message lifecycle
            # (R8): the prior unsent summary message is withdrawn as
            # superseded and the new text stands as a fresh verified
            # message — a queued copy of the old text can never be
            # sent now that the label exists.
            self._record_summary_message(case, summary_text, [], as_of=issued_on)
        return label

    def _tool_void_label(
        self, *, call_id: str, case_id: str, label_id: str, voided_by: str, reason: str, voided_on: date
    ) -> LabelVoid:
        """void_return_label: the void act. A named actor, a stated
        reason, an active label of this case — each refusal names
        its gap. The act is its own artefact; the label's status
        flips and the void stands as the reason."""
        voided_by = (voided_by or "").strip()
        if not voided_by:
            raise CarrierError("voiding a label requires a named actor")
        reason = (reason or "").strip()
        if not reason:
            raise CarrierError("voiding a label requires a stated reason")
        label = self.get_label(label_id)
        if label is None or label.case_id != case_id:
            raise LookupError(f"unknown label {label_id} for case {case_id}")
        if label.status == "voided":
            raise CarrierError(f"label {label_id} is already voided; a void is a single act")
        try:
            self._carrier.void_label(carrier_ref=label.carrier_ref)
        except CarrierProviderError as exc:
            raise CarrierError(str(exc)) from exc
        blob = json.dumps(
            {
                "label_id": label_id,
                "voided_by": voided_by,
                "reason": reason,
                "voided_on": voided_on.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        void = LabelVoid(
            void_id="LV-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            label_id=label_id,
            case_id=case_id,
            voided_by=voided_by,
            reason=reason,
            voided_on=voided_on,
        )
        self._store.put_void(void.model_dump(mode="json"))
        label.status = "voided"
        self._store.put_label(label.model_dump(mode="json"))
        return void

    def _tool_get_carrier_events(
        self, *, call_id: str, case_id: str, label_id: str, as_of: date
    ) -> list[CarrierEvent]:
        """get_carrier_events: the label's tracking events as of a
        date, from the carrier port. Read-only."""
        label = self.get_label(label_id)
        if label is None or label.case_id != case_id:
            raise LookupError(f"unknown label {label_id} for case {case_id}")
        return self._carrier_events_for(label, as_of)

    def _tool_book_pickup(
        self, *, call_id: str, case_id: str, scheduled_on: date, label_id: str | None = None
    ) -> PickupBooking:
        """book_pickup: a carrier pickup for the case's active label.

        Bulky categories only — the pack flags them, and the refusal
        for any other category names that rule. The pickup collects
        the labelled parcel, so an active label must exist first.
        """
        case = self.get_case(case_id)
        if case is None:
            raise LookupError(f"unknown case: {case_id}")
        product = CATALOGUE.get(case.request.sku)
        category = product.category if product is not None else None
        if category not in self._pack.bulky_categories:
            raise CarrierError(
                f"category {category!r} is not a pack-flagged bulky category "
                f"(bulky_categories: {', '.join(sorted(self._pack.bulky_categories))}); "
                "carrier pickup is refused — the customer ships with the label"
            )
        if label_id is not None:
            label = self.get_label(label_id)
            if label is None or label.case_id != case_id:
                raise LookupError(f"unknown label {label_id} for case {case_id}")
            if label.status != "issued":
                raise CarrierError(f"label {label_id} is {label.status}; a pickup needs an active label")
        else:
            label = self.get_active_label(case_id)
            if label is None:
                raise CarrierError(
                    f"case {case_id} has no active label; a pickup collects the labelled "
                    "parcel, so a label must be issued first"
                )
        result = self._carrier.book_pickup(
            case_id=case_id,
            carrier_ref=label.carrier_ref,
            scheduled_on=scheduled_on,
            window=self._pack.pickup_window,
        )
        blob = json.dumps(
            {
                "case_id": case_id,
                "label_id": label.label_id,
                "carrier_ref": result.carrier_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        booking = PickupBooking(
            pickup_id="PK-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            case_id=case_id,
            label_id=label.label_id,
            provider=result.provider,
            carrier_ref=result.carrier_ref,
            scheduled_on=scheduled_on,
            window=self._pack.pickup_window,
            status="booked",
            via_call_id=call_id,
        )
        self._store.put_pickup(booking.model_dump(mode="json"))
        return booking

    # -- the timeline ---------------------------------------------------------
    def get_timeline(self, case_id: str, as_of: date) -> CaseTimeline:
        """The case's derived timeline at `as_of`: every artefact of
        record in recorded-date order, expected-but-absent artefacts
        named as gaps. Carrier events are read through the port as
        of the same date, so the view is a pure function of the
        record and the date — never stored, never prose."""
        case = self.get_case(case_id)
        if case is None:
            raise LookupError(f"unknown case: {case_id}")
        labels = self.list_labels(case_id)
        return build_timeline(
            case=case,
            computation=self.get_computation(case_id),
            disposition=self.get_disposition(case_id),
            fraud_scores=[
                FraudScore.model_validate(p) for p in self._store.list_fraud_scores(case_id)
            ],
            fraud_reviews=self._reviews_for(case_id),
            approvals=self.list_approvals(case_id),
            executions=self.list_executions(case_id),
            receipt=self.get_receipt(case_id),
            labels=labels,
            voids=self.list_voids(case_id),
            pickups=self.list_pickups(case_id),
            events_by_label={
                label.label_id: self._carrier_events_for(label, as_of) for label in labels
            },
            computation_chain=self.list_computations_for_case(case_id),
            disposition_chain=self.list_dispositions_for_case(case_id),
            warehouse_receipts=self.list_warehouse_receipts(case_id),
            grading_reports=self.list_grading_reports(case_id),
            adjustments=self.list_adjustments(case_id),
            messages=self.list_messages(case_id),
            as_of=as_of,
        )

    # -- warehouse receipt + grading (R7) ------------------------------------
    def record_warehouse_receipt(
        self, case_id: str, *, received_by: str, received_on: date
    ) -> WarehouseReceipt:
        """The warehouse receipt act: a named receiver takes the
        parcel into the warehouse. Refusals, each naming its gap:
        an unnamed receiver; a case with no eligible return (there
        is no parcel); no label of record; and — the carrier's word
        governs — no `received` carrier event for the label as of
        the receipt date. Re-recording the same act (same receiver,
        same date, same label) returns the existing artefact; a
        different act against an already-receipted label refuses."""
        case = self.get_case(case_id)
        if case is None:
            raise LookupError(f"unknown case: {case_id}")
        received_by = (received_by or "").strip()
        if not received_by:
            raise GradingError("a warehouse receipt requires a named receiver")
        if case.decision is None or not case.decision.eligible:
            raise GradingError(
                f"case {case_id} has no eligible return; there is no parcel to receive"
            )
        label = self.get_label_for_case(case_id)
        if label is None:
            raise GradingError(
                f"case {case_id} has no return label of record; a parcel cannot be "
                "received before a label moves it"
            )
        existing = self._store.get_warehouse_receipt_for_label(label.label_id)
        if existing is not None:
            receipt = WarehouseReceipt.model_validate(existing)
            if receipt.received_by == received_by and receipt.received_on == received_on:
                return receipt
            raise GradingError(
                f"label {label.label_id} already has a warehouse receipt of record "
                f"({receipt.warehouse_receipt_id}, by {receipt.received_by} on "
                f"{receipt.received_on}); a parcel is received once"
            )
        events = self._carrier_events_for(label, received_on)
        if not any(e.kind == "received" for e in events):
            seen = ", ".join(e.kind for e in events) or "none"
            raise GradingError(
                f"the carrier has not recorded receipt of label {label.label_id} as of "
                f"{received_on} (events of record: {seen}); the warehouse cannot receipt "
                "a parcel the carrier still shows in transit"
            )
        blob = json.dumps(
            {
                "case_id": case_id,
                "label_id": label.label_id,
                "received_by": received_by,
                "received_on": received_on.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        receipt = WarehouseReceipt(
            warehouse_receipt_id="WHR-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            case_id=case_id,
            label_id=label.label_id,
            received_by=received_by,
            received_on=received_on,
        )
        self._store.put_warehouse_receipt(receipt.model_dump(mode="json"))
        return receipt

    def list_warehouse_receipts(self, case_id: str) -> list[WarehouseReceipt]:
        receipts = [
            WarehouseReceipt.model_validate(p)
            for p in self._store.list_warehouse_receipts(case_id)
        ]
        return sorted(receipts, key=lambda r: (r.received_on, r.warehouse_receipt_id))

    def get_warehouse_receipt(self, case_id: str) -> WarehouseReceipt | None:
        receipts = self.list_warehouse_receipts(case_id)
        return receipts[-1] if receipts else None

    def list_grading_reports(self, case_id: str) -> list[GradingReport]:
        reports = [
            GradingReport.model_validate(p) for p in self._store.list_grading_reports(case_id)
        ]
        return sorted(reports, key=lambda r: (r.graded_on, r.report_id))

    def get_grading_report(self, case_id: str) -> GradingReport | None:
        reports = self.list_grading_reports(case_id)
        return reports[-1] if reports else None

    def list_adjustments(self, case_id: str) -> list[RefundAdjustment]:
        adjustments = [
            RefundAdjustment.model_validate(p) for p in self._store.list_adjustments(case_id)
        ]
        return sorted(adjustments, key=lambda a: (a.created_on, a.adjustment_id))

    def get_adjustment(self, adjustment_id: str) -> RefundAdjustment | None:
        payload = self._store.get_adjustment(adjustment_id)
        return RefundAdjustment.model_validate(payload) if payload else None

    def record_grading(
        self,
        case_id: str,
        *,
        grader: str,
        graded_condition: str,
        graded_on: date,
        functional_test_result: str | None = None,
        data_wipe_status: str | None = None,
        damage_notes: str = "",
        received_weight_grams: int | None = None,
        wear_evidence: bool = False,
    ) -> GradingReport:
        """Record the warehouse's grading report for a case.

        Since R11 the grading act is the warehouse's callback
        through the gateway (the submit_grading_report tool, actor
        "warehouse"): the invocation is policy-evaluated and
        recorded like every tool call, and the act itself — gates
        and supersession cascade — runs as the tool's handler in
        _record_grading_impl.
        """
        return self.invoke_tool(
            "submit_grading_report",
            {
                "case_id": case_id,
                "grader": grader,
                "graded_condition": graded_condition,
                "graded_on": graded_on,
                "functional_test_result": functional_test_result,
                "data_wipe_status": data_wipe_status,
                "damage_notes": damage_notes,
                "received_weight_grams": received_weight_grams,
                "wear_evidence": wear_evidence,
            },
            case_id=case_id,
            called_on=graded_on,
            actor=ACTOR_WAREHOUSE,
        )

    def _record_grading_impl(
        self,
        case_id: str,
        *,
        grader: str,
        graded_condition: str,
        graded_on: date,
        functional_test_result: str | None = None,
        data_wipe_status: str | None = None,
        damage_notes: str = "",
        received_weight_grams: int | None = None,
        wear_evidence: bool = False,
    ) -> GradingReport:
        """The grading act and its supersession cascade, in order.

        Gates first, each refusal naming its gap: the case is an
        eligible return; a warehouse receipt of record exists; the
        grader is named and is NOT the receiver of record
        (separation of duties — the person who took the parcel in
        does not grade it); the graded condition is in the pack's
        vocabulary; no grading report stands yet for the case; and
        when inspection facts are required — the current disposition
        is HOLD_FOR_INSPECTION, or the graded facts would hold the
        item again — the report records BOTH of them, or it does not
        finalise.

        The cascade then runs in its stated order, each step a new
        linked artefact, the superseded ones never edited:

        1. disposition — re-decided over the graded condition
           (basis "graded") whenever the grade differs from the
           declaration or a hold must resolve; a still-held item
           resolves through the grading pack's inspection table.
        2. computation — re-run over the graded condition whenever
           the grade differs; the delta against the superseded total
           is computed and recorded on this report.
        3. adjustment — if money already executed against the
           superseded computation, the delta becomes a stated
           position: recovery_due (over-refunded; a recorded
           receivable, no clawback built) or top_up_due
           (under-refunded; settled only via execute_top_up).
        4. summary — re-issued against the new computation and
           figure-verified as always; a verification failure
           refuses the whole act before anything is stored.
        5. fraud re-score — when the grade is wildly worse than the
           declaration (the pack's rank jump) AND the grader records
           wear evidence, the case is re-scored through the R4
           machinery; the report's weigh-in is the score's weight
           of record by then.
        """
        case = self.get_case(case_id)
        if case is None:
            raise LookupError(f"unknown case: {case_id}")
        grader = (grader or "").strip()
        if not grader:
            raise GradingError("a grading report requires a named grader")
        decision = case.decision
        if decision is None or not decision.eligible:
            raise GradingError(
                f"case {case_id} is not an eligible return; there is no item to grade"
            )
        if self.list_grading_reports(case_id):
            existing = self.list_grading_reports(case_id)[-1]
            raise GradingError(
                f"case {case_id} already has a finalised grading report "
                f"({existing.report_id} by {existing.grader}); grading is a single act "
                "of record — a later disagreement is new evidence, not an edit"
            )
        receipt = self.get_warehouse_receipt(case_id)
        if receipt is None:
            raise GradingError(
                f"case {case_id} has no warehouse receipt of record; the parcel must be "
                "received into the warehouse before it can be graded"
            )
        if grader == receipt.received_by:
            raise GradingError(
                f"separation of duties: {grader} received this parcel into the warehouse "
                f"(receipt {receipt.warehouse_receipt_id}) and cannot grade it; "
                "a different grader must record the grading"
            )
        if graded_condition not in CONDITION_VOCABULARY:
            raise GradingError(
                f"graded condition {graded_condition!r} is not in the grading "
                f"vocabulary ({', '.join(CONDITION_VOCABULARY)}); the warehouse states "
                "one of these, never a nearest match"
            )
        if functional_test_result is not None and functional_test_result not in FUNCTIONAL_RESULTS:
            raise GradingError(
                f"functional_test_result {functional_test_result!r} is not a recorded "
                f"outcome ({', '.join(sorted(FUNCTIONAL_RESULTS))})"
            )
        if data_wipe_status is not None and data_wipe_status not in DATA_WIPE_STATES:
            raise GradingError(
                f"data_wipe_status {data_wipe_status!r} is not a recorded state "
                f"({', '.join(sorted(DATA_WIPE_STATES))})"
            )
        if received_weight_grams is not None and received_weight_grams <= 0:
            raise GradingError("a warehouse weigh-in must be a positive weight in grams")

        order = self.get_order(case.order_id)
        if order is None:
            raise GradingError(f"order missing for case {case_id}")
        product = CATALOGUE.get(case.request.sku)
        category = product.category if product is not None else None
        declared = case.request.condition_declared
        age_days = (case.request.requested_on - order.delivered_on).days
        current_disposition = self.get_disposition(case_id)
        current_computation = self.get_computation(case_id)

        # -- step 2 (computed early, in memory): the refund re-run ----
        new_computation: RefundComputation | None = None
        refund_delta: Decimal | None = None
        if graded_condition != declared and current_computation is not None:
            new_computation = compute_refund(
                self._pack,
                order,
                product,
                case.request,
                decision,
                condition_override=graded_condition,
                supersedes=current_computation.computation_id,
            )
            if (
                new_computation.refund_total is not None
                and current_computation.refund_total is not None
            ):
                refund_delta = money(
                    new_computation.refund_total - current_computation.refund_total
                )

        # -- step 1 (in memory): the disposition over the grade -------
        new_disposition: DispositionDecision | None = None
        held_now = (
            current_disposition is not None
            and current_disposition.disposition == HOLD_FOR_INSPECTION
        )
        if graded_condition != declared or held_now:
            pinned_computation = (
                new_computation.computation_id
                if new_computation is not None
                else (current_computation.computation_id if current_computation else None)
            )
            walk = decide_disposition(
                self._matrix,
                self._channels,
                case_id=case_id,
                computation_id=pinned_computation,
                category=category,
                condition_declared=declared,
                age_days=age_days,
                basis=BASIS_GRADED,
                graded_condition=graded_condition,
                supersedes=(
                    current_disposition.disposition_id if current_disposition else None
                ),
            )
            required_unknowns: set[str] = set()
            if held_now and current_disposition is not None:
                required_unknowns |= set(current_disposition.inspection_unknowns)
            if walk.status == "decided" and walk.disposition == HOLD_FOR_INSPECTION:
                required_unknowns |= set(walk.inspection_unknowns)
            established = {
                "functional_test_result": functional_test_result,
                "data_wipe_status": data_wipe_status,
            }
            missing = sorted(u for u in required_unknowns if established.get(u) is None)
            if missing:
                raise GradingError(
                    f"case {case_id} is held for inspection: the grading report must "
                    f"record {', '.join(missing)} — what inspection had to establish — "
                    "or it does not finalise"
                )
            if walk.status == "decided" and walk.disposition == HOLD_FOR_INSPECTION:
                rule = resolve_inspection(functional_test_result, data_wipe_status)
                if rule is None:
                    raise GradingError(
                        "the recorded inspection facts resolve to no rule in the "
                        "grading pack; the hold cannot be resolved on these facts"
                    )
                band = self._matrix.band_for(age_days) if age_days is not None else None
                new_disposition = inspection_successor_decision(
                    case_id=case_id,
                    computation_id=pinned_computation,
                    matrix_version=self._matrix.version,
                    channel_table_version=self._channels.version,
                    category=category,
                    condition_declared=declared,
                    graded_condition=graded_condition,
                    age_days=age_days,
                    age_band=band.band_id if band else None,
                    rule=rule,
                    supersedes=(
                        current_disposition.disposition_id if current_disposition else ""
                    ),
                )
            else:
                new_disposition = walk

        report_blob = json.dumps(
            {
                "case_id": case_id,
                "warehouse_receipt_id": receipt.warehouse_receipt_id,
                "grader": grader,
                "graded_on": graded_on.isoformat(),
                "graded_condition": graded_condition,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        report_id = "GR-" + hashlib.sha256(report_blob.encode()).hexdigest()[:16]

        # -- step 3 (in memory): the adjustment position ---------------
        adjustment: RefundAdjustment | None = None
        if (
            new_computation is not None
            and current_computation is not None
            and refund_delta is not None
            and refund_delta != 0
        ):
            settled = self._store.get_receipt_by_computation(
                current_computation.computation_id
            )
            if settled is not None:
                amount = money(abs(refund_delta))
                tier = approval_tier(self._pack, amount)
                if refund_delta < 0:
                    kind = "recovery_due"
                    reason = (
                        f"grading re-priced the refund from ${current_computation.refund_total} "
                        f"to ${new_computation.refund_total}: the customer was over-refunded "
                        f"by ${amount} under the declared condition {declared!r}; the graded "
                        f"condition is {graded_condition!r}"
                    )
                    note = (
                        f"collecting this recovery requires its own approval (tier {tier} "
                        f"on ${amount} under policy pack {self._pack.version}); no clawback "
                        "execution exists in this build — the position stands as a recorded "
                        "receivable until a gated collection act is built"
                    )
                else:
                    kind = "top_up_due"
                    reason = (
                        f"grading re-priced the refund from ${current_computation.refund_total} "
                        f"to ${new_computation.refund_total}: the customer was under-refunded "
                        f"by ${amount} under the declared condition {declared!r}; the graded "
                        f"condition is {graded_condition!r}"
                    )
                    note = (
                        "the top-up settles only through execute_top_up: approvals "
                        f"re-checked against the new computation (its total falls under "
                        f"tier {approval_tier(self._pack, new_computation.refund_total)}), "
                        "the fraud guard consulted, and the idempotency key taken in the "
                        "topup: key domain"
                    )
                adj_blob = json.dumps(
                    {"report_id": report_id, "kind": kind, "amount": str(amount)},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                adjustment = RefundAdjustment(
                    adjustment_id="ADJ-" + hashlib.sha256(adj_blob.encode()).hexdigest()[:16],
                    case_id=case_id,
                    kind=kind,
                    amount=amount,
                    reason=reason,
                    from_computation_id=current_computation.computation_id,
                    to_computation_id=new_computation.computation_id,
                    source_receipt_id=settled["receipt_id"],
                    grading_report_id=report_id,
                    approval_tier=tier,
                    approval_note=note,
                    status="open",
                    created_on=graded_on,
                )

        # -- step 4 (in memory): the re-issued, verified summary -------
        summary_text: str | None = None
        if new_computation is not None:
            product_name = product.name if product else case.request.sku
            active_label = self.get_active_label(case_id)
            summary_text = self._compose_summary_text(
                decision, new_computation, product_name, label=active_label
            )
            violations = verify_summary(
                summary_text, decision, new_computation, active_label
            )
            if violations:
                raise FigureVerificationError(violations)

        # -- the fraud trigger (step 5's decision, on the record) -------
        rank_jump = CONDITION_RANKS[graded_condition] - CONDITION_RANKS.get(declared, 0)
        rescore_triggered = bool(rank_jump >= WILDLY_WORSE_JUMP and wear_evidence)

        report = GradingReport(
            report_id=report_id,
            case_id=case_id,
            label_id=receipt.label_id,
            warehouse_receipt_id=receipt.warehouse_receipt_id,
            grader=grader,
            graded_on=graded_on,
            declared_condition=declared,
            graded_condition=graded_condition,
            functional_test_result=functional_test_result,
            data_wipe_status=data_wipe_status,
            damage_notes=damage_notes,
            received_weight_grams=received_weight_grams,
            wear_evidence=wear_evidence,
            grading_pack_version=GRADING_PACK_VERSION,
            superseded_disposition_id=(
                current_disposition.disposition_id
                if new_disposition is not None and current_disposition is not None
                else None
            ),
            new_disposition_id=new_disposition.disposition_id if new_disposition else None,
            superseded_computation_id=(
                current_computation.computation_id if new_computation is not None else None
            ),
            new_computation_id=new_computation.computation_id if new_computation else None,
            refund_delta=refund_delta,
            adjustment_id=adjustment.adjustment_id if adjustment else None,
            fraud_rescore_triggered=rescore_triggered,
        )

        # -- persist: new artefacts only; the superseded ones stand ----
        if new_disposition is not None:
            self._store.put_disposition(new_disposition.model_dump(mode="json"))
        if new_computation is not None:
            self._store.put_computation(new_computation.model_dump(mode="json"))
        if adjustment is not None:
            self._store.put_adjustment(adjustment.model_dump(mode="json"))
        self._store.put_grading_report(report.model_dump(mode="json"))
        # Memory milestone (R9): the case was graded — the record
        # carries the graded condition, the weigh-in (the weight of
        # record for later scores) and the refund delta.
        graded_disp = new_disposition or current_disposition
        self._record_memory(
            self._draft_memory_record(
                case,
                EVENT_CASE_GRADED,
                occurred_on=report.graded_on,
                source_ref=report.report_id,
                graded_condition=report.graded_condition,
                received_weight_grams=report.received_weight_grams,
                refund_delta=report.refund_delta,
                disposition=(
                    graded_disp.disposition
                    if graded_disp is not None and graded_disp.status == "decided"
                    else None
                ),
            )
        )

        # -- step 5: the fraud re-score, through the R4 machinery ------
        if rescore_triggered:
            score = self.rescore_case(case_id)
            if score is not None:
                report.new_fraud_score_id = score.score_id
                self._store.put_grading_report(report.model_dump(mode="json"))

        # -- the case record follows its current artefacts --------------
        fresh = self.get_case(case_id)
        if fresh is not None:
            if new_computation is not None:
                fresh.computation = new_computation
            if new_disposition is not None:
                fresh.disposition = new_disposition
            if summary_text is not None:
                fresh.summary = summary_text
                fresh.status = "summary_ready"
            self._store.put_case(fresh.model_dump(mode="json"))
            if summary_text is not None:
                self._store.put_summary(
                    case_id,
                    verified=True,
                    payload={
                        "case_id": case_id,
                        "text": summary_text,
                        "violations": [],
                        "profile": self._model.profile_name,
                    },
                )
                # The re-issued summary enters the message lifecycle
                # (R8): any unsent summary message drafted against the
                # superseded computation is withdrawn, and the new
                # text stands as a fresh verified message.
                self._record_summary_message(fresh, summary_text, [], as_of=graded_on)
        return report

    # -- the top-up execution (R7) --------------------------------------------
    def execute_top_up(
        self, case_id: str, *, idempotency_key: str, as_of: date
    ) -> RefundReceipt:
        """Settle an open `top_up_due` adjustment — the only money a
        graded case may still move, and only through the R5 gates.

        The key lives in the top-up domain: the caller's key is
        stored as `topup:<key>`, so it can never collide with (or
        replay) the original refund's execution. Preconditions, each
        refusal naming the gap: an open top_up_due adjustment of
        record; the fraud path open (the R4 guard); the adjustment's
        computation still the case's current one and final; and the
        approval gate RE-CHECKED against that new computation —
        approvals pinned to the superseded computation do not count,
        because they approved a different amount. A provider failure
        records a failed execution and leaves the adjustment open.
        """
        key = (idempotency_key or "").strip()
        if not key:
            raise PaymentsError("an idempotency key is mandatory on every refund execution")
        namespaced = f"topup:{key}"
        case = self.get_case(case_id)
        if case is None:
            raise LookupError(f"unknown case: {case_id}")

        replayed = self._store.get_receipt_by_key(namespaced)
        if replayed is not None:
            receipt = RefundReceipt.model_validate(replayed)
            if receipt.case_id != case_id:
                raise PaymentsError(
                    f"idempotency key {namespaced!r} was already used for case "
                    f"{receipt.case_id}; a key names one execution"
                )
            return receipt

        adjustments = self.list_adjustments(case_id)
        top_ups = [a for a in adjustments if a.kind == "top_up_due"]
        adjustment = next((a for a in top_ups if a.status == "open"), None)
        if adjustment is None:
            settled = next((a for a in top_ups if a.status == "executed"), None)
            if settled is not None:
                raise DuplicateRefundError(
                    f"duplicate_refund: the top-up for case {case_id} already executed "
                    f"(receipt {settled.top_up_receipt_id}); a second top-up is refused"
                )
            raise PaymentsError(
                f"case {case_id} has no open top-up adjustment; a top-up settles a "
                "grading adjustment of record, and there is none to settle"
            )
        self.assert_refund_path_open(case_id)
        computation = self.get_computation(case_id)
        if computation is None or computation.computation_id != adjustment.to_computation_id:
            raise PaymentsError(
                f"the adjustment settles computation {adjustment.to_computation_id}, "
                "which is no longer the case's current computation; the position must "
                "be re-stated before it can settle"
            )
        if not computation.is_final or computation.refund_total is None:
            raise PaymentsError(
                "the graded computation is not final (a component is not computable); "
                "a top-up cannot settle against a partial computation"
            )
        status = self.approval_status(case_id)
        if not status["satisfied"]:
            raise ApprovalError(
                f"approval gate unmet for the top-up against computation "
                f"{computation.computation_id}: {status['missing']}"
            )

        stored_execution = self._store.get_execution_by_key(namespaced)
        if stored_execution is not None:
            execution = RefundExecution.model_validate(stored_execution)
            if execution.case_id != case_id or execution.computation_id != computation.computation_id:
                raise PaymentsError(
                    f"idempotency key {namespaced!r} already names an execution for case "
                    f"{execution.case_id} / computation {execution.computation_id}"
                )
        else:
            blob = json.dumps(
                {
                    "case_id": case_id,
                    "computation_id": computation.computation_id,
                    "idempotency_key": namespaced,
                    "adjustment_id": adjustment.adjustment_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            execution = RefundExecution(
                execution_id="RX-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
                case_id=case_id,
                computation_id=computation.computation_id,
                idempotency_key=namespaced,
                amount=adjustment.amount,
                provider=self._payments.provider_name,
                status="requested",
                transitions=[
                    ExecutionTransition(
                        state="requested", at=as_of, detail="top-up execution requested"
                    )
                ],
            )
        execution.status = "executing"
        execution.failure_reason = None
        execution.transitions.append(
            ExecutionTransition(state="executing", at=as_of, detail="payments port called")
        )
        self._store.put_execution(execution.model_dump(mode="json"))

        # The top-up's tender allocation: what each tender is still
        # owed — the new computation's allocation less what the
        # original receipt already paid that tender. Orders without
        # tender data allocate nothing, exactly as their computations
        # state.
        allocations: list[TenderAllocation] = []
        settled_payload = self._store.get_receipt_by_computation(
            adjustment.from_computation_id
        )
        if settled_payload is not None:
            settled_receipt = RefundReceipt.model_validate(settled_payload)
            paid = {a.method: a.amount for a in settled_receipt.tender_allocations}
            for alloc in computation.tender_allocations:
                remaining = money(alloc.amount - paid.get(alloc.method, Decimal("0.00")))
                if remaining > 0:
                    allocations.append(
                        TenderAllocation(method=alloc.method, amount=remaining)
                    )

        try:
            provider_receipt = self._execute_via_gateway(
                case_id=case_id,
                computation_id=computation.computation_id,
                amount=adjustment.amount,
                tender_allocations=allocations,
                idempotency_key=namespaced,
                as_of=as_of,
            )
        except PolicyDeniedError as exc:
            execution.status = "failed"
            execution.failure_reason = str(exc)
            execution.transitions.append(
                ExecutionTransition(state="failed", at=as_of, detail=str(exc))
            )
            self._store.put_execution(execution.model_dump(mode="json"))
            raise PaymentsError(
                f"gateway policy denied the top-up execution for case {case_id}: {exc} "
                "No receipt was written; the top-up adjustment stays open."
            ) from exc
        except PaymentsProviderError as exc:
            execution.status = "failed"
            execution.failure_reason = str(exc)
            execution.transitions.append(
                ExecutionTransition(state="failed", at=as_of, detail=str(exc))
            )
            self._store.put_execution(execution.model_dump(mode="json"))
            raise PaymentsError(
                f"payments provider failed for case {case_id}: {exc} "
                "No receipt was written; the top-up adjustment stays open."
            ) from exc

        blob = json.dumps(
            {
                "case_id": case_id,
                "computation_id": computation.computation_id,
                "idempotency_key": namespaced,
                "provider_ref": provider_receipt.provider_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        receipt = RefundReceipt(
            receipt_id="PR-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            case_id=case_id,
            computation_id=computation.computation_id,
            execution_id=execution.execution_id,
            idempotency_key=namespaced,
            provider=provider_receipt.provider,
            provider_ref=provider_receipt.provider_ref,
            amount=adjustment.amount,
            tender_allocations=allocations,
            status="executed",
            executed_on=as_of,
        )
        self._store.put_receipt(receipt.model_dump(mode="json"))
        execution.status = "executed"
        execution.receipt_id = receipt.receipt_id
        execution.transitions.append(
            ExecutionTransition(
                state="executed", at=as_of, detail=f"receipt {receipt.receipt_id}"
            )
        )
        self._store.put_execution(execution.model_dump(mode="json"))
        adjustment.status = "executed"
        adjustment.top_up_execution_id = execution.execution_id
        adjustment.top_up_receipt_id = receipt.receipt_id
        adjustment.idempotency_key = namespaced
        self._store.put_adjustment(adjustment.model_dump(mode="json"))
        # Memory milestone (R9): the top-up is a refund execution
        # too — a second executed record on the case, sourced by
        # the top-up receipt.
        top_up_case = self.get_case(case_id)
        if top_up_case is not None:
            self._memory_refund_executed(top_up_case, receipt)
        return receipt

    # -- customer messaging (R8) ---------------------------------------------
    def get_disclosure_pack(self) -> DisclosurePack:
        return self._disclosure

    def _case_receipts(self, case_id: str) -> list[RefundReceipt]:
        receipts = [r for r in self.list_receipts() if r.case_id == case_id]
        return sorted(receipts, key=lambda r: (r.executed_on, r.receipt_id))

    def _message_universe(
        self,
        case: ReturnCase,
        message_type: str,
        label_override: ReturnLabel | None = None,
        as_of: date | None = None,
    ) -> tuple[MessageContext, dict[str, str], str]:
        """The verified universe for one message of this case, plus
        the artefact pin: (context, refs, fingerprint).

        The context is case-wide — every figure any artefact of the
        case computed, every disposition and condition the record
        states, the case's own public references, and the disclosure
        rules resolved (pack terms + the scoring pack's signal names
        and review reason codes, derived here so the ban can never
        drift from the pack). From R9 the universe also carries the
        case's OWN CUSTOMER's memory figures (prior refunds, prior
        totals, grading deltas from the memory port) — the only
        channel through which a message may reference a prior case,
        and only ever this customer's: the retrieval is scoped to
        `case.customer_id`, retention-filtered, and logged. The pin
        is per message TYPE: the refs name the artefacts that type
        renders against, and the fingerprint hashes their payloads
        — recomputed at send time, it is how a stale message is
        recognised.
        """
        decision = case.decision
        if decision is None:
            raise MessagingError(
                f"case {case.case_id} has no decision of record; no message can be drafted or checked"
            )
        computations = self.list_computations_for_case(case.case_id)
        computation = computations[-1] if computations else None
        dispositions = self.list_dispositions_for_case(case.case_id)
        label = label_override if label_override is not None else self.get_active_label(case.case_id)
        labels = self.list_labels(case.case_id)
        receipts = self._case_receipts(case.case_id)
        report = self.get_grading_report(case.case_id)
        adjustments = self.list_adjustments(case.case_id)
        adjustment = adjustments[-1] if adjustments else None
        review = self.get_fraud_review(case.case_id)
        score = self._current_score(case.case_id)
        pickups = self.list_pickups(case.case_id)
        pickup = pickups[-1] if pickups else None
        original_receipt = next(
            (r for r in receipts if not r.idempotency_key.startswith("topup:")), None
        )
        top_up_receipt = next(
            (r for r in receipts if r.idempotency_key.startswith("topup:")), None
        )

        amounts = set(decision.allowed_amounts)
        for comp in computations:
            amounts |= comp.allowed_amounts
        if label is not None:
            amounts.add(label.cost)
        for receipt in receipts:
            amounts.add(receipt.amount)
            amounts |= {a.amount for a in receipt.tender_allocations}
        if report is not None and report.refund_delta is not None:
            amounts.add(abs(report.refund_delta))
        for adj in adjustments:
            amounts.add(adj.amount)

        dates = set(decision.allowed_dates)
        if label is not None:
            dates.add(label.issued_on)
        for receipt in receipts:
            dates.add(receipt.executed_on)
        if report is not None:
            dates.add(report.graded_on)
        for adj in adjustments:
            dates.add(adj.created_on)
        for booking in pickups:
            dates.add(booking.scheduled_on)
        if score is not None:
            dates.add(score.as_of)

        percents: set[Decimal] = set()
        for comp in computations:
            condition = (
                comp.graded_condition
                if comp.condition_basis == "graded"
                else case.request.condition_declared
            )
            rate = self._pack.restocking_fee_rates.get(condition or "")
            if rate is not None:
                percents.add(Decimal(rate) * 100)

        # Memory (R9): the customer's own prior-case figures join
        # the sayable set — a returning customer's earlier refund
        # may be referenced in a new message ONLY as memory records
        # it, and only this customer's: the port's retrieval is
        # predicated on the case's customer id, excludes expired
        # records, and is written to the access log.
        memory_as_of = as_of or max(dates | {case.request.requested_on})
        for record in self._memory.retrieve(
            case.customer_id,
            purpose="message_verification",
            actor="messaging verifier",
            as_of=memory_as_of,
        ):
            if record.refund_total is not None:
                amounts.add(record.refund_total)
            if record.refund_amount is not None:
                amounts.add(record.refund_amount)
            if record.refund_delta is not None:
                amounts.add(abs(record.refund_delta))

        forbidden_terms = list(self._disclosure.forbidden_terms)
        for reasons in self._scoring.review_outcomes.values():
            forbidden_terms.extend(reasons)
        if score is not None:
            forbidden_terms.extend(s.signal_id for s in score.signals)
        forbidden_terms = list(dict.fromkeys(forbidden_terms))

        context = MessageContext(
            case_id=case.case_id,
            order_id=case.order_id,
            allowed_amounts=amounts,
            allowed_dates=dates,
            allowed_percents=percents,
            disposition_facts={d.disposition for d in dispositions if d.disposition},
            condition_facts=(
                {case.request.condition_declared}
                | {c.graded_condition for c in computations if c.graded_condition}
                | ({report.graded_condition} if report is not None else set())
            ),
            allowed_refs=(
                {case.case_id, case.order_id}
                | {lbl.carrier_ref for lbl in labels}
                | {r.provider_ref for r in receipts}
            ),
            forbidden_terms=forbidden_terms,
            forbidden_patterns=list(self._disclosure.forbidden_patterns),
        )

        def dump(model) -> dict | None:
            return model.model_dump(mode="json") if model is not None else None

        if message_type == "return_summary":
            # The summary is verified against the disposition chain
            # as well as the money: a grading that supersedes the
            # disposition (the held lamp resolving to REFURBISH)
            # moves the record under the message even when no figure
            # changes, so the chain is part of the pin.
            payloads = {
                "decision": dump(decision),
                "computation": dump(computation),
                "dispositions": [dump(d) for d in dispositions],
                "label": dump(label),
            }
            refs = {"decision": f"decision:{case.case_id}"}
            if computation is not None:
                refs["computation"] = computation.computation_id
            if dispositions:
                refs["dispositions"] = ",".join(d.disposition_id for d in dispositions)
            if label is not None:
                refs["label"] = label.label_id
        elif message_type == "label_instructions":
            payloads = {
                "label": dump(label),
                "decision": dump(decision),
                "pickup": dump(pickup),
            }
            refs = {"decision": f"decision:{case.case_id}"}
            if label is not None:
                refs["label"] = label.label_id
            if pickup is not None:
                refs["pickup"] = pickup.pickup_id
        elif message_type == "grading_outcome_notice":
            payloads = {
                "report": dump(report),
                "computations": [dump(c) for c in computations],
                "dispositions": [dump(d) for d in dispositions],
                "adjustment": dump(adjustment),
            }
            refs = {}
            if report is not None:
                refs["grading_report"] = report.report_id
            if computations:
                refs["computations"] = ",".join(c.computation_id for c in computations)
            if dispositions:
                refs["dispositions"] = ",".join(d.disposition_id for d in dispositions)
            if adjustment is not None:
                refs["adjustment"] = adjustment.adjustment_id
        elif message_type == "fraud_review_notice":
            payloads = {
                "review": dump(review),
                "score": dump(score),
                "computation": dump(computation),
            }
            refs = {}
            if review is not None:
                refs["fraud_review"] = review.review_id
            if score is not None:
                refs["fraud_score"] = score.score_id
            if computation is not None:
                refs["computation"] = computation.computation_id
        elif message_type == "refund_executed_notice":
            payloads = {
                "receipt": dump(original_receipt),
                "computation": dump(computation),
            }
            refs = {}
            if original_receipt is not None:
                refs["receipt"] = original_receipt.receipt_id
            if computation is not None:
                refs["computation"] = computation.computation_id
        elif message_type == "adjustment_notice":
            payloads = {
                "adjustment": dump(adjustment),
                "computations": [dump(c) for c in computations],
                "receipts": [dump(r) for r in receipts],
            }
            refs = {}
            if adjustment is not None:
                refs["adjustment"] = adjustment.adjustment_id
            if computations:
                refs["computations"] = ",".join(c.computation_id for c in computations)
            if receipts:
                refs["receipts"] = ",".join(r.receipt_id for r in receipts)
        else:
            raise MessagingError(
                f"unknown message type {message_type!r}: no template exists for it, "
                "so no text can be drafted"
            )
        fingerprint = hashlib.sha256(
            json.dumps(payloads, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return context, refs, fingerprint

    def _message_facts(self, case: ReturnCase, message_type: str) -> MessageFacts:
        """Assemble the facts a template renders from, checking the
        type's prerequisites — each refusal names the artefact that
        is missing, because a message about an event that has not
        happened cannot be drafted."""
        if case.decision is None:
            raise MessagingError(
                f"case {case.case_id} has no decision of record; no message can be drafted"
            )
        product = CATALOGUE.get(case.request.sku)
        facts = MessageFacts(
            case=case,
            product_name=product.name if product is not None else case.request.sku,
        )
        facts.computation = self.get_computation(case.case_id)
        facts.computations = self.list_computations_for_case(case.case_id)
        facts.disposition = self.get_disposition(case.case_id)
        facts.dispositions = self.list_dispositions_for_case(case.case_id)
        facts.label = self.get_active_label(case.case_id)
        receipts = self._case_receipts(case.case_id)
        facts.receipt = next(
            (r for r in receipts if not r.idempotency_key.startswith("topup:")), None
        )
        facts.top_up_receipt = next(
            (r for r in receipts if r.idempotency_key.startswith("topup:")), None
        )
        facts.report = self.get_grading_report(case.case_id)
        adjustments = self.list_adjustments(case.case_id)
        facts.adjustment = adjustments[-1] if adjustments else None
        facts.review = self.get_fraud_review(case.case_id)
        pickups = self.list_pickups(case.case_id)
        facts.pickup = pickups[-1] if pickups else None

        if message_type == "return_summary":
            facts.summary_text = self._compose_summary_text(
                case.decision, facts.computation, facts.product_name, label=facts.label
            )
        elif message_type == "label_instructions":
            if facts.label is None:
                raise MessagingError(
                    f"case {case.case_id} has no active return label; there are "
                    "no label instructions to send"
                )
            if facts.pickup is not None and facts.pickup.label_id != facts.label.label_id:
                facts.pickup = None
        elif message_type == "grading_outcome_notice":
            if facts.report is None:
                raise MessagingError(
                    f"case {case.case_id} has no grading report of record; there "
                    "is no grading outcome to notify"
                )
            if (
                facts.report.new_computation_id is None
                and facts.report.new_disposition_id is None
            ):
                raise MessagingError(
                    f"grading on case {case.case_id} changed neither the refund "
                    "nor the disposition; there is no grading outcome to notify"
                )
        elif message_type == "fraud_review_notice":
            if facts.review is None:
                raise MessagingError(
                    f"case {case.case_id} has no fraud-review decision of record; "
                    "there is no review outcome to notify"
                )
        elif message_type == "refund_executed_notice":
            if facts.receipt is None:
                raise MessagingError(
                    f"case {case.case_id} has no executed refund receipt; there "
                    "is no execution to notify"
                )
        elif message_type == "adjustment_notice":
            if facts.adjustment is None:
                raise MessagingError(
                    f"case {case.case_id} has no refund adjustment of record; "
                    "there is no adjustment to notify"
                )
        return facts

    def _withdraw_message(self, message: CustomerMessage, *, at: date, detail: str) -> None:
        """Withdraw an unsent message, on the record. Sent, refused
        and already-withdrawn messages stand as they are — history
        is not rewritten."""
        if message.status not in (STATE_DRAFT, STATE_VERIFIED, STATE_QUEUED):
            return
        message.status = STATE_WITHDRAWN
        message.transitions.append(
            MessageTransition(state=STATE_WITHDRAWN, at=at, actor="case service", detail=detail)
        )
        self._store.put_message(message.model_dump(mode="json"))

    def _create_message(
        self,
        case: ReturnCase,
        message_type: str,
        text: str,
        *,
        as_of: date,
        refs: dict[str, str],
        fingerprint: str,
        initial_violations: list[str] | None = None,
    ) -> CustomerMessage:
        """Store a new draft. Any prior UNSENT message of the same
        type for this case is withdrawn as superseded first — one
        live draft per type, and the superseded text can never be
        queued or sent from that moment. When `initial_violations`
        is given (the summary path, whose draft was verified as it
        was composed), the verification transition is recorded with
        the draft: verified when clean, refused with the violations
        listed when not."""
        seq = self._store.count_messages(case.case_id) + 1
        blob = json.dumps(
            {
                "case_id": case.case_id,
                "message_type": message_type,
                "seq": seq,
                "fingerprint": fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        message_id = "MSG-" + hashlib.sha256(blob.encode()).hexdigest()[:16]
        supersedes = None
        for prior in self.list_messages(case.case_id):
            if prior.message_type != message_type:
                continue
            if prior.status in (STATE_DRAFT, STATE_VERIFIED, STATE_QUEUED):
                self._withdraw_message(
                    prior, at=as_of, detail=f"superseded by new draft {message_id}"
                )
                supersedes = prior.message_id
        message = CustomerMessage(
            message_id=message_id,
            case_id=case.case_id,
            message_type=message_type,
            status=STATE_DRAFT,
            seq=seq,
            text=text,
            template_version=MESSAGING_PACK_VERSION,
            disclosure_pack_version=self._disclosure.version,
            artefact_refs=refs,
            artefact_fingerprint=fingerprint,
            supersedes=supersedes,
            created_on=as_of,
            transitions=[
                MessageTransition(
                    state=STATE_DRAFT,
                    at=as_of,
                    actor=f"composer ({self._model.profile_name})",
                    detail="drafted from the type's template over the artefacts of record",
                )
            ],
        )
        if initial_violations is not None:
            message.violations = list(initial_violations)
            if initial_violations:
                message.status = STATE_REFUSED
                message.transitions.append(
                    MessageTransition(
                        state=STATE_REFUSED,
                        at=as_of,
                        actor="messaging verifier",
                        detail=(
                            f"verification failed with {len(initial_violations)} "
                            "violation(s); the message is refused and will not be sent"
                        ),
                    )
                )
            else:
                message.status = STATE_VERIFIED
                message.transitions.append(
                    MessageTransition(
                        state=STATE_VERIFIED,
                        at=as_of,
                        actor="messaging verifier",
                        detail="figures, facts and disclosure checks passed",
                    )
                )
        self._store.put_message(message.model_dump(mode="json"))
        return message

    def _record_summary_message(
        self, case: ReturnCase, text: str, figure_violations: list[str], *, as_of: date
    ) -> CustomerMessage:
        """Enter a (re-)issued case summary into the message
        lifecycle. Idempotent: the same text against the same
        artefacts already standing as an unsent summary message is
        returned unchanged. A figure-clean text is then checked by
        the full messaging verifier; a figure-failing text is
        recorded refused with the figure violations as they stand."""
        _ctx, refs, fingerprint = self._message_universe(case, "return_summary", as_of=as_of)
        for existing in self.list_messages(case.case_id):
            if (
                existing.message_type == "return_summary"
                and existing.status in (STATE_DRAFT, STATE_VERIFIED, STATE_QUEUED)
                and existing.text == text
                and existing.artefact_fingerprint == fingerprint
            ):
                return existing
        if figure_violations:
            return self._create_message(
                case,
                "return_summary",
                text,
                as_of=as_of,
                refs=refs,
                fingerprint=fingerprint,
                initial_violations=list(figure_violations),
            )
        checked = verify_message_text(text, _ctx)
        return self._create_message(
            case,
            "return_summary",
            text,
            as_of=as_of,
            refs=refs,
            fingerprint=fingerprint,
            initial_violations=checked,
        )

    def draft_message(
        self,
        case_id: str,
        message_type: str,
        *,
        as_of: date,
        model_note: str | None = None,
    ) -> CustomerMessage:
        """Draft a customer message from its type's template over
        the case's current artefacts. `model_note` is the model
        port's free-form slot — appended as the closing paragraph
        and verified with the rest of the text at the verify step.
        Refusals name the gap: an unknown type (no template), a
        missing prerequisite artefact."""
        case = self.get_case(case_id)
        if case is None:
            raise LookupError(f"unknown case: {case_id}")
        if message_type not in TEMPLATES:
            raise MessagingError(
                f"unknown message type {message_type!r}: no template exists for it, "
                "so no text can be drafted"
            )
        facts = self._message_facts(case, message_type)
        text = TEMPLATES[message_type](facts, model_note)
        _ctx, refs, fingerprint = self._message_universe(case, message_type, as_of=as_of)
        return self._create_message(
            case, message_type, text, as_of=as_of, refs=refs, fingerprint=fingerprint
        )

    def verify_message(self, message_id: str, *, as_of: date) -> CustomerMessage:
        """The verification act: the messaging verifier runs over
        the draft's final text against the case's CURRENT verified
        universe. Clean → verified (sendable after queueing).
        Violations → stored as refused, with the violations listed
        on the artefact — never sent, never silently fixed."""
        message = self.get_message(message_id)
        if message is None:
            raise LookupError(f"unknown message: {message_id}")
        if message.status != STATE_DRAFT:
            raise MessagingError(
                f"message {message_id} is {message.status}; only a draft can be verified"
            )
        case = self.get_case(message.case_id)
        if case is None:
            raise LookupError(f"unknown case: {message.case_id}")
        context, _refs, _fingerprint = self._message_universe(
            case, message.message_type, as_of=as_of
        )
        violations = verify_message_text(message.text, context)
        message.violations = violations
        if violations:
            message.status = STATE_REFUSED
            message.transitions.append(
                MessageTransition(
                    state=STATE_REFUSED,
                    at=as_of,
                    actor="messaging verifier",
                    detail=(
                        f"verification failed with {len(violations)} violation(s); "
                        "the message is refused and will not be sent"
                    ),
                )
            )
        else:
            message.status = STATE_VERIFIED
            message.transitions.append(
                MessageTransition(
                    state=STATE_VERIFIED,
                    at=as_of,
                    actor="messaging verifier",
                    detail="figures, facts and disclosure checks passed",
                )
            )
        self._store.put_message(message.model_dump(mode="json"))
        return message

    def queue_message(self, message_id: str, *, as_of: date) -> CustomerMessage:
        """Queue a verified message for sending. A draft (never
        verified) or a refused message cannot be queued — the
        refusal names the state it is actually in."""
        message = self.get_message(message_id)
        if message is None:
            raise LookupError(f"unknown message: {message_id}")
        if message.status != STATE_VERIFIED:
            raise MessagingError(
                f"message {message_id} is {message.status}; only a verified message can be queued"
            )
        message.status = STATE_QUEUED
        message.transitions.append(
            MessageTransition(
                state=STATE_QUEUED,
                at=as_of,
                actor="operations",
                detail="queued for sending",
            )
        )
        self._store.put_message(message.model_dump(mode="json"))
        return message

    def send_message(self, message_id: str, *, as_of: date) -> CustomerMessage:
        """Send a queued message through the messaging port.

        Idempotent per message id: an already-sent message returns
        as it stands and the port is not called again. Staleness is
        checked first, in code: the artefact fingerprint is
        recomputed from the current record, and a mismatch means
        the case moved under the message — the message is WITHDRAWN
        unsent and re-drafted from the current artefacts (the fresh
        draft is verified and left standing), and this act refuses,
        naming the replacement. A provider failure leaves the
        message queued and sendable later.
        """
        message = self.get_message(message_id)
        if message is None:
            raise LookupError(f"unknown message: {message_id}")
        if message.status == STATE_SENT:
            return message
        if message.status != STATE_QUEUED:
            raise MessagingError(
                f"message {message_id} is {message.status}; only a queued message can be sent"
            )
        case = self.get_case(message.case_id)
        if case is None:
            raise LookupError(f"unknown case: {message.case_id}")
        try:
            _ctx, _refs, fingerprint = self._message_universe(
                case, message.message_type, as_of=as_of
            )
        except MessagingError:
            fingerprint = None
        if fingerprint != message.artefact_fingerprint:
            self._withdraw_message(
                message,
                at=as_of,
                detail=(
                    "withdrawn at send time: the artefacts this message was "
                    "rendered against changed after it was queued (stale); "
                    "re-drafting from the current artefacts"
                ),
            )
            replacement = None
            try:
                candidate = self.draft_message(case.case_id, message.message_type, as_of=as_of)
                candidate.supersedes = message.message_id
                self._store.put_message(candidate.model_dump(mode="json"))
                replacement = self.verify_message(candidate.message_id, as_of=as_of)
            except MessagingError:
                replacement = None
            raise MessagingError(
                f"message {message_id} went stale before sending and was withdrawn unsent"
                + (
                    f"; a fresh draft {replacement.message_id} (status "
                    f"{replacement.status}) replaces it"
                    if replacement is not None
                    else "; no replacement could be drafted from the current artefacts"
                )
            )
        try:
            delivery = self._messaging.send(
                message_id=message.message_id, case_id=message.case_id, text=message.text
            )
        except MessagingProviderError as exc:
            raise MessagingError(
                f"messaging provider failed for message {message_id}: {exc} "
                "Nothing was delivered; the message stays queued."
            ) from exc
        message.status = STATE_SENT
        message.provider = delivery.provider
        message.provider_ref = delivery.provider_ref
        message.transitions.append(
            MessageTransition(
                state=STATE_SENT,
                at=as_of,
                actor="messaging port",
                detail=f"delivered via {delivery.provider} (ref {delivery.provider_ref})",
            )
        )
        self._store.put_message(message.model_dump(mode="json"))
        return message

    def get_message(self, message_id: str) -> CustomerMessage | None:
        payload = self._store.get_message(message_id)
        return CustomerMessage.model_validate(payload) if payload else None

    def list_messages(self, case_id: str) -> list[CustomerMessage]:
        """Every message of record for the case — drafts, verified,
        refused, queued, sent, withdrawn — in creation order."""
        messages = [
            CustomerMessage.model_validate(p) for p in self._store.list_messages(case_id)
        ]
        return sorted(messages, key=lambda m: (m.seq, m.message_id))

    def list_refused_messages(self, case_id: str) -> list[CustomerMessage]:
        """The case's refused messages, with their violations — the
        refusal read: what was drafted, why it may not be sent."""
        return [m for m in self.list_messages(case_id) if m.status == STATE_REFUSED]

    def list_all_messages(self) -> list[CustomerMessage]:
        messages = [
            CustomerMessage.model_validate(p) for p in self._store.list_all_messages()
        ]
        return sorted(messages, key=lambda m: (m.case_id, m.seq, m.message_id))

    # -- reads ---------------------------------------------------------------
    def get_case(self, case_id: str) -> ReturnCase | None:
        payload = self._store.get_case(case_id)
        return ReturnCase.model_validate(payload) if payload else None

    def list_cases(self) -> list[ReturnCase]:
        return [ReturnCase.model_validate(p) for p in self._store.list_cases()]

    def list_orders(self) -> list[Order]:
        return [Order.model_validate(p) for p in self._store.list_orders()]

    def get_order(self, order_id: str) -> Order | None:
        payload = self._store.get_order(order_id)
        return Order.model_validate(payload) if payload else None
