"""FastAPI management surface: intake, cases, decisions, orders.

This is the local/operations API. The AgentCore Runtime contract lives
in runtime_app.py; both drive the same CaseService, so a case decided
through one surface reads identically through the other.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from .intake import ReferenceFeed
from .models import ReturnRequest
from .service import CaseService, FigureVerificationError


def _order_view(order) -> dict:
    view = order.model_dump(mode="json")
    # Totals are computed properties (never stored), so the API adds
    # them explicitly — a reader should never have to re-derive money.
    view["items_total"] = str(order.items_total)
    view["order_total"] = str(order.order_total)
    return view


def create_app(service: CaseService) -> FastAPI:
    app = FastAPI(title="Returns Resolution & Disposition Agent")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/readiness")
    def readiness() -> dict:
        return {"status": "ready", "orders": len(service.list_orders())}

    @app.get("/orders")
    def orders() -> list[dict]:
        return [_order_view(o) for o in service.list_orders()]

    @app.get("/orders/{order_id}")
    def order(order_id: str) -> dict:
        found = service.get_order(order_id)
        if found is None:
            raise HTTPException(status_code=404, detail="order not found")
        return _order_view(found)

    @app.post("/intake/feed")
    def intake_feed() -> dict:
        results = []
        for request in ReferenceFeed().fetch_requests():
            try:
                case = service.ingest(request)
                results.append({"request_id": request.request_id, "case_id": case.case_id, "status": case.status})
            except FigureVerificationError as exc:
                results.append({"request_id": request.request_id, "status": "summary_failed", "violations": exc.violations})
        return {"ingested": results}

    @app.post("/returns", status_code=201)
    def submit_return(request: ReturnRequest) -> dict:
        try:
            case = service.ingest(request)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FigureVerificationError as exc:
            raise HTTPException(status_code=422, detail={"violations": exc.violations}) from exc
        return case.model_dump(mode="json")

    @app.get("/cases")
    def cases() -> list[dict]:
        return [c.model_dump(mode="json") for c in service.list_cases()]

    @app.get("/cases/{case_id}")
    def case(case_id: str) -> dict:
        found = service.get_case(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="case not found")
        return found.model_dump(mode="json")

    @app.get("/cases/{case_id}/decision")
    def decision(case_id: str) -> dict:
        found = service.get_case(case_id)
        if found is None or found.decision is None:
            raise HTTPException(status_code=404, detail="decision not found")
        return found.decision.model_dump(mode="json")

    @app.get("/cases/{case_id}/computation")
    def computation(case_id: str) -> dict:
        found = service.get_computation(case_id)
        if found is None:
            raise HTTPException(status_code=404, detail="computation not found")
        return found.model_dump(mode="json")

    @app.get("/cases/{case_id}/computation/verify")
    def computation_verify(case_id: str) -> dict:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        problems = service.verify_stored_computation(case_id)
        return {"case_id": case_id, "ok": not problems, "problems": problems}

    @app.get("/computations")
    def computations() -> list[dict]:
        return [c.model_dump(mode="json") for c in service.list_computations()]

    @app.post("/cases/{case_id}/evidence")
    async def evidence(case_id: str, request: Request, kind: str = "photo", filename: str = "evidence.bin") -> dict:
        if service.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        content = await request.body()
        return service.attach_evidence(case_id, kind, filename, content)

    return app
