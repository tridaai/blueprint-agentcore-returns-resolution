"""The refund computation engine — deterministic, Decimal-only (R2).

Law 2 made mechanical: eligibility (R1) decides *whether* money is
due; this engine decides *how much*, line by line, from the decision,
the order as stored and the policy pack's refund schedule. Every line
is a named record carrying its derivation (pinned inputs, the rule,
the policy reference), and the whole computation is a content-
addressed artefact: its id is a hash of the exact inputs, so it
cannot drift from the decision and order it was computed from.

Three honesty rules, all graded:
- A component whose input is missing is `not_computable` and names
  the input — never a silent zero. (A *rule-produced* zero, e.g. a 0%
  restocking rate for new-with-tags, is a computed line and says so.)
- While any money line is not computable, `refund_total` is None and
  `is_final` is False — a partial computation states what is not final.
- `verify_computation` re-performs the whole computation from the
  pinned inputs and compares every field; any tampered amount, total
  or allocation is detected.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .models import (
    ComputationLine,
    EligibilityDecision,
    NotComputableComponent,
    Order,
    Product,
    RefundComputation,
    ReturnRequest,
    TenderAllocation,
    money,
)
from .policy import PolicyPack

REFUND_ENGINE_VERSION = "refund-v1"

# The input a tender allocation needs and the seed orders do not carry.
MISSING_TENDER_BREAKDOWN = "payment_tender_breakdown"


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def inputs_fingerprint(order: Order, decision: EligibilityDecision) -> str:
    """Hash of the exact inputs a computation is pinned to."""
    blob = _canonical(
        {
            "order": order.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
        }
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _computation_id(case_id: str, pack_version: str, fingerprint: str) -> str:
    blob = _canonical(
        {
            "case_id": case_id,
            "policy_pack_version": pack_version,
            "inputs_fingerprint": fingerprint,
            "engine": REFUND_ENGINE_VERSION,
        }
    )
    return "RC-" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def compute_refund(
    pack: PolicyPack,
    order: Order,
    product: Product | None,
    request: ReturnRequest,
    decision: EligibilityDecision,
) -> RefundComputation:
    fingerprint = inputs_fingerprint(order, decision)
    computation_id = _computation_id(decision.case_id, pack.version, fingerprint)

    if not decision.eligible:
        # A denied case has a computation of record too: no lines, a
        # final zero total — the denial's money statement, not an
        # absence where a total should be.
        return RefundComputation(
            computation_id=computation_id,
            case_id=decision.case_id,
            engine_version=REFUND_ENGINE_VERSION,
            policy_pack_version=pack.version,
            inputs_fingerprint=fingerprint,
            eligible=False,
            lines=[],
            total_deductions=Decimal("0.00"),
            refund_total=Decimal("0.00"),
            is_final=True,
        )

    lines: list[ComputationLine] = []
    not_computable: list[NotComputableComponent] = []

    item_amount = decision.item_amount
    lines.append(
        ComputationLine(
            line_id="item_amount",
            kind="credit",
            amount=item_amount,
            status="computed",
            inputs={
                "unit_price": str(order.line_for(decision.sku).unit_price) if order.line_for(decision.sku) else "",
                "qty": str(decision.qty),
                "decision_item_amount": str(decision.item_amount),
            },
            rule="item_amount = unit_price * qty, as decided by the eligibility engine",
            policy_ref="decision.item_amount",
        )
    )

    # Restocking fee: rate by declared condition, from the pack.
    rate_raw = pack.restocking_fee_rates.get(request.condition_declared)
    if rate_raw is None:
        missing = f"restocking_fee_rate[condition={request.condition_declared}]"
        lines.append(
            ComputationLine(
                line_id="restocking_fee",
                kind="deduction",
                amount=None,
                status="not_computable",
                missing_input=missing,
                inputs={"condition_declared": request.condition_declared},
                rule="restocking_fee = item_amount * restocking_fee_rates[condition_declared]",
                policy_ref=f"policy.restocking_fee_rates[{request.condition_declared}]",
            )
        )
        not_computable.append(NotComputableComponent(component="restocking_fee", missing_input=missing))
    else:
        rate = Decimal(rate_raw)
        lines.append(
            ComputationLine(
                line_id="restocking_fee",
                kind="deduction",
                amount=money(item_amount * rate),
                status="computed",
                inputs={
                    "item_amount": str(item_amount),
                    "rate": str(rate),
                    "condition_declared": request.condition_declared,
                },
                rule="restocking_fee = item_amount * rate, quantised to cents HALF_UP",
                policy_ref=f"policy.restocking_fee_rates[{request.condition_declared}]={rate_raw}",
            )
        )

    # Return shipping: free for the pack's free-return categories,
    # otherwise the flat fee is deducted. The category is a product
    # fact; without it the deduction cannot be computed.
    category = product.category if product is not None else None
    if category is None:
        missing = "item_category"
        lines.append(
            ComputationLine(
                line_id="return_shipping",
                kind="deduction",
                amount=None,
                status="not_computable",
                missing_input=missing,
                inputs={},
                rule="return_shipping = 0 if category in free_return_categories else return_shipping_fee",
                policy_ref="policy.free_return_categories / policy.return_shipping_fee",
            )
        )
        not_computable.append(NotComputableComponent(component="return_shipping", missing_input=missing))
    elif category in pack.free_return_categories:
        lines.append(
            ComputationLine(
                line_id="return_shipping",
                kind="deduction",
                amount=Decimal("0.00"),
                status="computed",
                inputs={"category": category},
                rule="category is a free-return category: prepaid label, no deduction",
                policy_ref=f"policy.free_return_categories contains {category}",
            )
        )
    else:
        lines.append(
            ComputationLine(
                line_id="return_shipping",
                kind="deduction",
                amount=pack.return_shipping_fee,
                status="computed",
                inputs={"category": category, "fee": str(pack.return_shipping_fee)},
                rule="return_shipping = policy return_shipping_fee (deducted from the refund)",
                policy_ref="policy.return_shipping_fee",
            )
        )

    money_lines_final = all(line.status == "computed" for line in lines)
    if money_lines_final:
        total_deductions = money(
            sum((line.amount for line in lines if line.kind == "deduction"), Decimal("0.00"))
        )
        refund_total = money(item_amount - total_deductions)
    else:
        total_deductions = None
        refund_total = None

    # Tender allocation: where the money goes, in recorded tender
    # order, each tender capped at what it actually paid. Needs the
    # order's tender breakdown; without one the component is named
    # not computable — the total above stands on its own.
    allocations: list[TenderAllocation] = []
    if refund_total is None:
        not_computable.append(
            NotComputableComponent(component="tender_allocation", missing_input="refund_total")
        )
    elif order.tenders is None:
        not_computable.append(
            NotComputableComponent(
                component="tender_allocation", missing_input=MISSING_TENDER_BREAKDOWN
            )
        )
    elif money(sum((t.amount for t in order.tenders), Decimal("0.00"))) != order.order_total:
        not_computable.append(
            NotComputableComponent(
                component="tender_allocation",
                missing_input=f"{MISSING_TENDER_BREAKDOWN} (tender amounts do not sum to the order total)",
            )
        )
    else:
        remaining = refund_total
        for tender in order.tenders:
            if remaining <= 0:
                break
            take = min(remaining, tender.amount)
            allocations.append(TenderAllocation(method=tender.method, amount=money(take)))
            remaining = money(remaining - take)

    return RefundComputation(
        computation_id=computation_id,
        case_id=decision.case_id,
        engine_version=REFUND_ENGINE_VERSION,
        policy_pack_version=pack.version,
        inputs_fingerprint=fingerprint,
        eligible=True,
        lines=lines,
        total_deductions=total_deductions,
        refund_total=refund_total,
        is_final=money_lines_final,
        tender_allocations=allocations,
        not_computable=not_computable,
    )


def verify_computation(
    computation: RefundComputation,
    pack: PolicyPack,
    order: Order,
    product: Product | None,
    request: ReturnRequest,
    decision: EligibilityDecision,
) -> list[str]:
    """Re-perform the computation from the pinned inputs and compare.

    Returns the list of problems; empty means the stored computation is
    exactly what the engine produces from these inputs — any tampered
    line amount, total, allocation or fingerprint is named.
    """
    expected = compute_refund(pack, order, product, request, decision)
    problems: list[str] = []
    if computation.computation_id != expected.computation_id:
        problems.append(
            f"computation_id {computation.computation_id} != recomputed {expected.computation_id} "
            "(inputs or engine changed since this artefact was written)"
        )
    if computation.inputs_fingerprint != expected.inputs_fingerprint:
        problems.append("inputs_fingerprint does not match the pinned decision + order")
    if computation.model_dump(mode="json") != expected.model_dump(mode="json"):
        problems.append("stored computation differs from a fresh computation over the same inputs")
    return problems
