"""Case service — the workflow the Runtime entrypoint and API share.

One path, used by every surface: seed orders -> ingest a request ->
eligibility in code -> agent drafts the summary -> figure verifier ->
store. If the verifier rejects the draft, the case is stored as
`summary_failed` with no summary — an unverified customer message
never exists anywhere in the system.
"""

from __future__ import annotations

from .eligibility import case_id_for, evaluate
from .evidence import EvidenceStore
from .modelport import ModelPort
from .models import Order, ReturnCase, ReturnRequest
from .policy import PolicyPack, load_policy_pack
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
    ):
        self._store = store
        self._model = model_port
        self._pack = pack or load_policy_pack()
        self._evidence = evidence_store

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

        product_name = product.name if product else request.sku
        summary_text = self._model.compose_summary(decision, product_name)
        violations = verify_summary(summary_text, decision)
        case = ReturnCase(
            case_id=case_id_for(request.request_id),
            request=request,
            order_id=order.order_id,
            customer_id=order.customer_id,
            status="summary_ready" if not violations else "summary_failed",
            decision=decision,
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
