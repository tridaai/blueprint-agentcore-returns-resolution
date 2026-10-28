"""Return policy as a versioned pack — data plus the loader.

Law 1 of the blueprint: eligibility is policy-as-data. The pack below
is the whole of Cascade Outfitters' return policy v1; the eligibility
engine evaluates *this data*, and every decision records the pack
version and the clause ids that fired. A policy change is a new pack
version, never a code edit — and decisions taken under the old version
stay reproducible because the pack that produced them is named on the
decision record.
"""

from __future__ import annotations

from pydantic import BaseModel


class PolicyPack(BaseModel):
    version: str
    # Category -> days from delivery in which a return may be requested.
    # A category absent from this map is not returnable at all.
    return_window_days: dict[str, int]
    # Categories that are never returnable, with the clause that fires.
    excluded_categories: set[str]
    # Final-sale items are never returnable regardless of category.
    final_sale_excluded: bool = True
    # Customer-declared conditions that bar a return.
    barred_conditions: set[str]
    # Days after an approved request by which the customer must ship.
    ship_within_days: int = 14


POLICY_PACK_V1 = PolicyPack(
    version="2026.10.1",
    return_window_days={
        "apparel": 60,
        "footwear": 60,
        "gear": 90,
        "electronics": 30,
    },
    excluded_categories={"gift_card", "consumable"},
    barred_conditions={"damaged_by_customer"},
    ship_within_days=14,
)

# Clause ids — stable names that decisions, evals and (later) customer
# messaging all refer to. Denials cite these, never free-text reasons.
CLAUSE_WINDOW_WITHIN = "WINDOW_WITHIN"
CLAUSE_WINDOW_EXPIRED = "WINDOW_EXPIRED"
CLAUSE_CATEGORY_EXCLUDED = "CATEGORY_EXCLUDED"
CLAUSE_FINAL_SALE = "FINAL_SALE"
CLAUSE_CONDITION_BARRED = "CONDITION_BARRED"
CLAUSE_UNKNOWN_SKU = "UNKNOWN_SKU"
CLAUSE_QTY_EXCEEDS_ORDER = "QTY_EXCEEDS_ORDER"


def load_policy_pack(version: str | None = None) -> PolicyPack:
    """Fail-closed loader: only the shipped pack exists in R1.

    An unknown version is an error, never a silent fall back to v1 —
    a decision must name the pack it was actually taken under.
    """
    if version is None or version == POLICY_PACK_V1.version:
        return POLICY_PACK_V1
    raise ValueError(f"unknown policy pack version: {version!r}")
