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

from .models import Order, OrderLine, Product

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
