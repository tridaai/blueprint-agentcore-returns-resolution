"""The eligibility engine — pure, deterministic, Decimal-only.

Evaluation order is deliberate and recorded: structural problems
(unknown sku, quantity beyond the order) first, then the hard
exclusions (category, final sale, barred condition), then the window.
Every rule that fires leaves a ClauseHit on the decision — including
the WINDOW_WITHIN hit on a pass — so the decision record shows not
just the outcome but the complete reasoning an auditor would replay.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from .models import (
    ClauseHit,
    EligibilityDecision,
    Order,
    Product,
    ReturnRequest,
    money,
)
from .policy import (
    CLAUSE_CATEGORY_EXCLUDED,
    CLAUSE_CONDITION_BARRED,
    CLAUSE_FINAL_SALE,
    CLAUSE_QTY_EXCEEDS_ORDER,
    CLAUSE_UNKNOWN_SKU,
    CLAUSE_WINDOW_EXPIRED,
    CLAUSE_WINDOW_WITHIN,
    PolicyPack,
)


def case_id_for(request_id: str) -> str:
    return f"CASE-{request_id.removeprefix('REQ-')}"


def evaluate(
    pack: PolicyPack,
    order: Order,
    product: Product | None,
    request: ReturnRequest,
) -> EligibilityDecision:
    clauses: list[ClauseHit] = []
    deny = False

    line = order.line_for(request.sku)
    item_amount = Decimal("0.00")
    if line is not None:
        item_amount = money(line.unit_price * min(request.qty, line.qty))

    if product is None or line is None:
        clauses.append(
            ClauseHit(
                clause_id=CLAUSE_UNKNOWN_SKU,
                effect="deny",
                detail=f"sku {request.sku} is not a line on order {order.order_id}",
            )
        )
        deny = True
    elif request.qty > line.qty:
        clauses.append(
            ClauseHit(
                clause_id=CLAUSE_QTY_EXCEEDS_ORDER,
                effect="deny",
                detail=f"requested {request.qty} exceeds ordered qty {line.qty}",
            )
        )
        deny = True

    window_end = None
    if product is not None:
        if product.category in pack.excluded_categories:
            clauses.append(
                ClauseHit(
                    clause_id=CLAUSE_CATEGORY_EXCLUDED,
                    effect="deny",
                    detail=f"category {product.category} is not returnable",
                )
            )
            deny = True
        if pack.final_sale_excluded and product.final_sale:
            clauses.append(
                ClauseHit(
                    clause_id=CLAUSE_FINAL_SALE,
                    effect="deny",
                    detail="final-sale items are not returnable",
                )
            )
            deny = True

    if request.condition_declared in pack.barred_conditions:
        clauses.append(
            ClauseHit(
                clause_id=CLAUSE_CONDITION_BARRED,
                effect="deny",
                detail=f"declared condition {request.condition_declared} bars a return",
            )
        )
        deny = True

    if product is not None and product.category in pack.return_window_days:
        days = pack.return_window_days[product.category]
        window_end = order.delivered_on + timedelta(days=days)
        if request.requested_on <= window_end:
            clauses.append(
                ClauseHit(
                    clause_id=CLAUSE_WINDOW_WITHIN,
                    effect="allow",
                    detail=(
                        f"requested {request.requested_on} within {days}-day "
                        f"{product.category} window ending {window_end}"
                    ),
                )
            )
        else:
            clauses.append(
                ClauseHit(
                    clause_id=CLAUSE_WINDOW_EXPIRED,
                    effect="deny",
                    detail=(
                        f"requested {request.requested_on} after {days}-day "
                        f"{product.category} window ending {window_end}"
                    ),
                )
            )
            deny = True

    eligible = not deny
    ship_by = request.requested_on + timedelta(days=pack.ship_within_days) if eligible else None
    return EligibilityDecision(
        case_id=case_id_for(request.request_id),
        eligible=eligible,
        policy_pack_version=pack.version,
        clauses=clauses,
        order_id=order.order_id,
        sku=request.sku,
        qty=request.qty,
        item_amount=item_amount,
        refund_due=item_amount if eligible else Decimal("0.00"),
        delivered_on=order.delivered_on,
        requested_on=request.requested_on,
        window_end=window_end,
        ship_by=ship_by,
    )
