"""Case service — the workflow the Runtime entrypoint and API share.

One path, used by every surface: seed orders -> ingest a request ->
eligibility in code -> agent drafts the summary -> figure verifier ->
store. If the verifier rejects the draft, the case is stored as
`summary_failed` with no summary — an unverified customer message
never exists anywhere in the system.
"""

from __future__ import annotations

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
from .modelport import ModelPort
from .models import DispositionDecision, Order, RefundComputation, ReturnCase, ReturnRequest
from .policy import PolicyPack, load_policy_pack
from .refunds import compute_refund, verify_computation
from .seed import CATALOGUE, ORDERS
from .store import Store
from .verifier import verify_summary


class FigureVerificationError(RuntimeError):
    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations))
        self.violations = violations


class CaseService:
    def __init__(
        self,
        store: Store,
        model_port: ModelPort,
        pack: PolicyPack | None = None,
        evidence_store: EvidenceStore | None = None,
        matrix: MatrixPack | None = None,
        channels: ChannelTable | None = None,
    ):
        self._store = store
        self._model = model_port
        self._pack = pack or load_policy_pack()
        self._evidence = evidence_store
        self._matrix = matrix or load_matrix_pack()
        self._channels = channels or load_channel_table()

    # -- seeding ---------------------------------------------------------
    def seed_orders(self) -> int:
        for order in ORDERS.values():
            self._store.put_order(order.model_dump(mode="json"))
        return len(ORDERS)

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
