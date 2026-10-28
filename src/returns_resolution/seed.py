"""Cascade Outfitters — fictional catalogue and orders.

Gold by construction: every order is built from catalogue prices, so
its totals are provable in tests from first principles (sum of line
totals plus shipping). The planted orders are arranged so the policy
edges are exercised exactly:

- ORD-1003 / ORD-1004 are a one-day pair around the footwear window:
  same boots, delivered one day apart, both requested on 2026-10-26.
  ORD-1003 is exactly on its last eligible day; ORD-1004 is one day
  past its own.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .models import Order, OrderLine, Product, ReturnCase, ReturnRequest, TenderPortion

CATALOGUE: dict[str, Product] = {
    p.sku: p
    for p in [
        Product(sku="JKT-ALPINE", name="Alpine Shell Jacket", category="apparel", unit_price=Decimal("189.00")),
        Product(sku="BOOT-TRL", name="Ridgeline Trail Boots", category="footwear", unit_price=Decimal("145.00")),
        Product(sku="TENT-2P", name="Ridgeline 2P Tent", category="gear", unit_price=Decimal("329.00")),
        Product(sku="STOVE-PKT", name="Pocket Stove", category="gear", unit_price=Decimal("59.95")),
        Product(sku="LAMP-LMN", name="Lumen Headlamp", category="electronics", unit_price=Decimal("45.00")),
        Product(sku="GC-50", name="Gift Card 50", category="gift_card", unit_price=Decimal("50.00")),
        Product(sku="BAR-12", name="Trail Bars 12-Pack", category="consumable", unit_price=Decimal("24.00")),
        Product(
            sku="PANT-CLR",
            name="Clearance Rain Pants",
            category="apparel",
            unit_price=Decimal("39.99"),
            final_sale=True,
        ),
    ]
}


def _line(sku: str, qty: int) -> OrderLine:
    product = CATALOGUE[sku]
    return OrderLine(sku=sku, qty=qty, unit_price=product.unit_price)


ORDERS: dict[str, Order] = {
    o.order_id: o
    for o in [
        Order(
            order_id="ORD-1001",
            customer_id="CUST-ANA",
            placed_on=date(2026, 10, 16),
            delivered_on=date(2026, 10, 20),
            lines=[_line("JKT-ALPINE", 1), _line("BAR-12", 1)],
            shipping_paid=Decimal("12.00"),
        ),
        Order(
            order_id="ORD-1002",
            customer_id="CUST-BEN",
            placed_on=date(2026, 6, 27),
            delivered_on=date(2026, 7, 1),
            lines=[_line("TENT-2P", 1)],
            shipping_paid=Decimal("0.00"),
        ),
        # Edge pair: footwear window is 60 days.
        # 2026-08-27 + 60 days = 2026-10-26 -> requested on the last day.
        Order(
            order_id="ORD-1003",
            customer_id="CUST-CID",
            placed_on=date(2026, 8, 24),
            delivered_on=date(2026, 8, 27),
            lines=[_line("BOOT-TRL", 1)],
            shipping_paid=Decimal("8.00"),
        ),
        # 2026-08-26 + 60 days = 2026-10-25 -> requested one day late.
        Order(
            order_id="ORD-1004",
            customer_id="CUST-DEE",
            placed_on=date(2026, 8, 23),
            delivered_on=date(2026, 8, 26),
            lines=[_line("BOOT-TRL", 1)],
            shipping_paid=Decimal("8.00"),
        ),
        Order(
            order_id="ORD-1005",
            customer_id="CUST-ELI",
            placed_on=date(2026, 10, 17),
            delivered_on=date(2026, 10, 20),
            lines=[_line("PANT-CLR", 1)],
            shipping_paid=Decimal("6.00"),
        ),
        Order(
            order_id="ORD-1006",
            customer_id="CUST-FAY",
            placed_on=date(2026, 10, 7),
            delivered_on=date(2026, 10, 10),
            lines=[_line("LAMP-LMN", 2)],
            shipping_paid=Decimal("0.00"),
        ),
        Order(
            order_id="ORD-1007",
            customer_id="CUST-GUS",
            placed_on=date(2026, 10, 12),
            delivered_on=date(2026, 10, 15),
            lines=[_line("STOVE-PKT", 1)],
            shipping_paid=Decimal("6.00"),
        ),
        Order(
            order_id="ORD-1008",
            customer_id="CUST-HAL",
            placed_on=date(2026, 10, 18),
            delivered_on=date(2026, 10, 19),
            lines=[_line("GC-50", 1)],
            shipping_paid=Decimal("0.00"),
        ),
    ]
}

# Shipped weights (grams) from the product master — the expected
# weight the fraud engine compares a carrier-scanned return parcel
# against (R4). A sku absent here has no weight of record: the
# engine's weight signal is then not computable, and names it.
PRODUCT_WEIGHTS_GRAMS: dict[str, int] = {
    "JKT-ALPINE": 820,
    "BOOT-TRL": 1450,
    "TENT-2P": 2400,
    "STOVE-PKT": 320,
    "LAMP-LMN": 210,
    "GC-50": 20,
    "BAR-12": 700,
    "PANT-CLR": 380,
}


def _tender(method: str, amount: str) -> TenderPortion:
    return TenderPortion(method=method, amount=Decimal(amount))


# ---------------------------------------------------------------------------
# Fraud history (R4) — gold by construction, hand-derived in tests/evals.
#
# CUST-SER is the serial returner: 8 orders, every one returned, paid
# across 4 distinct cards. Seven returns are closed history; the
# eighth (ORD-3008) is the live request REQ-3008, scored against this
# history when it is ingested:
#   velocity: 7 prior cases inside the 90-day window, allowance 2
#             -> (7-2) x 8 = 40 (at the cap)
#   share:    8 of 8 orders returned -> 1.0000 -> top tier, 35
#   churn:    4 distinct tenders -> top tier, 20
#   wardrobing 0, weight not computable (no scan) -> total 95.00
#   -> FRAUD_REVIEW.
#
# CUST-WARD is the wardrobing pattern: 3 apparel/footwear orders, all
# declared new_with_tags after 35/42/48 days of ownership. Two are
# closed history; the third (ORD-3103) is the live request REQ-3103:
#   wardrobing: 3 matching cases x 12 = 36 (at the cap)
#   velocity:   2 priors, inside the allowance -> 0
#   share:      3 orders < the pack's 4-order minimum
#               -> insufficient_data, named, 0
#   churn:      no tender breakdowns -> insufficient_data, named, 0
#   weight:     no scan -> not computable, named, 0
#   -> total 36.00 -> MONITOR.
# ---------------------------------------------------------------------------

_HISTORY_ORDER_ROWS = [
    # (order_id, customer, placed, delivered, sku, tender method or None)
    ("ORD-3001", "CUST-SER", date(2026, 7, 25), date(2026, 7, 28), "TENT-2P", "card_visa_4411"),
    ("ORD-3002", "CUST-SER", date(2026, 8, 3), date(2026, 8, 6), "JKT-ALPINE", "card_mc_5522"),
    ("ORD-3003", "CUST-SER", date(2026, 8, 12), date(2026, 8, 15), "BOOT-TRL", "card_visa_4411"),
    ("ORD-3004", "CUST-SER", date(2026, 8, 26), date(2026, 8, 29), "STOVE-PKT", "card_amex_1005"),
    ("ORD-3005", "CUST-SER", date(2026, 9, 7), date(2026, 9, 10), "LAMP-LMN", "card_visa_7788"),
    ("ORD-3006", "CUST-SER", date(2026, 9, 21), date(2026, 9, 24), "JKT-ALPINE", "card_mc_5522"),
    ("ORD-3007", "CUST-SER", date(2026, 10, 2), date(2026, 10, 5), "TENT-2P", "card_visa_4411"),
    ("ORD-3008", "CUST-SER", date(2026, 10, 12), date(2026, 10, 15), "BOOT-TRL", "card_amex_1005"),
    ("ORD-3101", "CUST-WARD", date(2026, 8, 17), date(2026, 8, 20), "JKT-ALPINE", None),
    ("ORD-3102", "CUST-WARD", date(2026, 8, 25), date(2026, 8, 28), "BOOT-TRL", None),
    ("ORD-3103", "CUST-WARD", date(2026, 9, 2), date(2026, 9, 5), "JKT-ALPINE", None),
]

HISTORY_ORDERS: dict[str, Order] = {}
for _oid, _cust, _placed, _delivered, _sku, _method in _HISTORY_ORDER_ROWS:
    _line_item = _line(_sku, 1)
    HISTORY_ORDERS[_oid] = Order(
        order_id=_oid,
        customer_id=_cust,
        placed_on=_placed,
        delivered_on=_delivered,
        lines=[_line_item],
        shipping_paid=Decimal("0.00"),
        tenders=[_tender(_method, str(_line_item.line_total))] if _method else None,
    )

_HISTORY_CASE_ROWS = [
    # (request_id, order_id, sku, condition_declared, requested_on)
    ("REQ-3001", "ORD-3001", "TENT-2P", "used", date(2026, 8, 5)),
    ("REQ-3002", "ORD-3002", "JKT-ALPINE", "like_new", date(2026, 8, 20)),
    ("REQ-3003", "ORD-3003", "BOOT-TRL", "used", date(2026, 9, 1)),
    ("REQ-3004", "ORD-3004", "STOVE-PKT", "used", date(2026, 9, 12)),
    ("REQ-3005", "ORD-3005", "LAMP-LMN", "like_new", date(2026, 9, 27)),
    ("REQ-3006", "ORD-3006", "JKT-ALPINE", "used", date(2026, 10, 8)),
    ("REQ-3007", "ORD-3007", "TENT-2P", "like_new", date(2026, 10, 19)),
    ("REQ-3101", "ORD-3101", "JKT-ALPINE", "new_with_tags", date(2026, 9, 24)),
    ("REQ-3102", "ORD-3102", "BOOT-TRL", "new_with_tags", date(2026, 10, 9)),
]


def _history_case(request_id: str, order_id: str, sku: str, condition: str, requested_on: date) -> ReturnCase:
    order = HISTORY_ORDERS[order_id]
    return ReturnCase(
        case_id=f"CASE-{request_id.removeprefix('REQ-')}",
        request=ReturnRequest(
            request_id=request_id,
            order_id=order_id,
            sku=sku,
            qty=1,
            reason_code="changed_mind",
            condition_declared=condition,
            requested_on=requested_on,
        ),
        order_id=order_id,
        customer_id=order.customer_id,
        status="closed",
    )


HISTORY_CASES: list[ReturnCase] = [
    _history_case(rid, oid, sku, cond, on) for rid, oid, sku, cond, on in _HISTORY_CASE_ROWS
]
