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
from datetime import date

from .disposition import (
    ChannelTable,
    MatrixPack,
    decide_disposition,
    load_channel_table,
    load_matrix_pack,
    verify_disposition,
)
from .eligibility import case_id_for, evaluate
from .evidence import EvidenceStore
from .fraud import (
    BLOCKED_STATES,
    CaseFact,
    ScoringPack,
    derive_fraud_state,
    load_scoring_pack,
    score_case,
    verify_fraud_score,
)
from .modelport import ModelPort
from .models import (
    DispositionDecision,
    ExecutionTransition,
    FraudReviewDecision,
    FraudScore,
    Order,
    RefundApproval,
    RefundComputation,
    RefundExecution,
    RefundReceipt,
    ReturnCase,
    ReturnRequest,
)
from .payments import (
    APPROVERS_REQUIRED,
    LocalPaymentsDouble,
    PaymentsPort,
    PaymentsProviderError,
    approval_tier,
)
from .policy import PolicyPack, load_policy_pack
from .refunds import compute_refund, verify_computation
from .seed import CATALOGUE, HISTORY_CASES, HISTORY_ORDERS, ORDERS, PRODUCT_WEIGHTS_GRAMS
from .store import Store
from .verifier import verify_summary


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

    @property
    def payments_port(self) -> PaymentsPort:
        return self._payments

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
        refund_composer = getattr(self._model, "compose_refund_summary", None)
        if refund_composer is not None and decision.eligible:
            summary_text = refund_composer(decision, product_name, computation)
        else:
            summary_text = self._model.compose_summary(decision, product_name)
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
        if violations:
            raise FigureVerificationError(violations)
        return case

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
        """Re-perform the stored disposition's walk; [] = intact."""
        disposition = self.get_disposition(case_id)
        case = self.get_case(case_id)
        if disposition is None or case is None:
            return ["no disposition for this case"]
        order = self.get_order(case.order_id)
        if order is None:
            return ["order missing for this case"]
        product = CATALOGUE.get(case.request.sku)
        return verify_disposition(
            disposition,
            self._matrix,
            self._channels,
            case_id=case_id,
            computation_id=disposition.computation_id,
            category=product.category if product is not None else None,
            condition_declared=case.request.condition_declared,
            age_days=(case.request.requested_on - order.delivered_on).days,
        )

    def list_dispositions(self) -> list[DispositionDecision]:
        return [DispositionDecision.model_validate(p) for p in self._store.list_dispositions()]

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
        return self._fact_for(order, case.request)

    def _score_over(self, scored: CaseFact, supersedes: str | None = None) -> FraudScore:
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
        return score_case(
            self._scoring,
            scored=scored,
            facts=facts,
            orders=orders,
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

    # -- refund execution (R5) ------------------------------------------------
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
            if receipt.case_id != case_id or (
                computation is not None and receipt.computation_id != computation.computation_id
            ):
                raise PaymentsError(
                    f"idempotency key {key!r} was already used for case "
                    f"{receipt.case_id} / computation {receipt.computation_id}; "
                    "a key names one execution"
                )
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
            provider_receipt = self._payments.execute_refund(
                case_id=case_id,
                computation_id=computation.computation_id,
                amount=computation.refund_total,
                tender_allocations=computation.tender_allocations,
                idempotency_key=key,
            )
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
