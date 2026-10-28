"""Domain models for the returns blueprint.

Money is Decimal everywhere and quantised to cents at construction, so
a figure that enters the system can be compared exactly against the
figure the engine later computes — that exactness is what the figure
verifier (verifier.py) relies on.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field, field_validator

CENT = Decimal("0.01")


def money(value: Decimal | str | int) -> Decimal:
    """Quantise a value to cents, HALF_UP — the one rounding rule."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class Product(BaseModel):
    sku: str
    name: str
    category: str
    unit_price: Decimal
    final_sale: bool = False

    @field_validator("unit_price", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))


class OrderLine(BaseModel):
    sku: str
    qty: int = Field(gt=0)
    unit_price: Decimal

    @field_validator("unit_price", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))

    @property
    def line_total(self) -> Decimal:
        return money(self.unit_price * self.qty)


class Order(BaseModel):
    order_id: str
    customer_id: str
    placed_on: date
    delivered_on: date
    lines: list[OrderLine]
    shipping_paid: Decimal = Decimal("0.00")

    @field_validator("shipping_paid", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))

    @property
    def items_total(self) -> Decimal:
        return money(sum((line.line_total for line in self.lines), Decimal("0.00")))

    @property
    def order_total(self) -> Decimal:
        return money(self.items_total + self.shipping_paid)

    def line_for(self, sku: str) -> OrderLine | None:
        for line in self.lines:
            if line.sku == sku:
                return line
        return None


class ReturnRequest(BaseModel):
    """One planted or submitted request to return items from an order."""

    request_id: str
    order_id: str
    sku: str
    qty: int = Field(gt=0)
    reason_code: str
    # Customer-declared condition: new_with_tags | like_new | used |
    # damaged_by_customer. The declaration is an input to policy, never
    # a decision — warehouse grading arrives in a later round.
    condition_declared: str
    requested_on: date
    channel: str = "web"


class ClauseHit(BaseModel):
    """One policy clause that fired, with its effect and the facts."""

    clause_id: str
    effect: str  # "allow" | "deny"
    detail: str


class EligibilityDecision(BaseModel):
    """The computed decision. Every later figure traces back to this."""

    case_id: str
    eligible: bool
    policy_pack_version: str
    clauses: list[ClauseHit]
    order_id: str
    sku: str
    qty: int
    item_amount: Decimal
    refund_due: Decimal
    delivered_on: date
    requested_on: date
    window_end: date | None
    ship_by: date | None

    @field_validator("item_amount", "refund_due", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))

    @property
    def allowed_amounts(self) -> set[Decimal]:
        return {self.item_amount, self.refund_due}

    @property
    def allowed_dates(self) -> set[date]:
        dates = {self.delivered_on, self.requested_on}
        if self.window_end is not None:
            dates.add(self.window_end)
        if self.ship_by is not None:
            dates.add(self.ship_by)
        return dates


class ReturnCase(BaseModel):
    case_id: str
    request: ReturnRequest
    order_id: str
    customer_id: str
    status: str  # decided | summary_ready | summary_failed
    decision: EligibilityDecision | None = None
    summary: str | None = None
