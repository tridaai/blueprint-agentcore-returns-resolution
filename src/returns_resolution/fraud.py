"""The fraud scoring pack — versioned, deterministic, in code (R4).

Law 4 made mechanical: fraud signals are scored by a pack that is
*data* (signal parameters, tiers, bands, the review vocabulary) plus
pure functions over stored artefacts. The composite is a stated
weighted sum in Decimal — the same stored orders and cases always
produce the same score artefact, byte for byte.

Signals (each recorded with its inputs and its contribution):

- `return_velocity`: prior cases inside a stated window, beyond a
  free allowance, at a stated rate, capped.
- `serial_returner_share`: the share of the customer's orders that
  came back; tiered. Below a minimum order count the signal is
  `insufficient_data` — a customer with one order and one return is
  not a serial returner, and the pack says so instead of dividing
  1/1 and calling it a pattern.
- `wardrobing_indicator`: wear-category items declared new-with-tags
  after a stated minimum ownership age — the keep-it-a-weekend,
  send-it-back pattern — counted per case, capped.
- `weight_mismatch`: the carrier-scanned return weight against the
  product's shipped weight. A parcel at under half its expected
  weight is the empty-box band. When nobody weighed the parcel, or
  the product master carries no weight, the signal is
  `not_computable` and names the missing input.
- `card_churn`: distinct payment tenders across the customer's
  orders; tiered. Orders without a tender breakdown make the signal
  `insufficient_data` — the absence is named (the same
  `payment_tender_breakdown` input the refund engine names in R2).

Outcomes by band (pack data): CLEAR / MONITOR / FRAUD_REVIEW. A
FRAUD_REVIEW band blocks the case's refund path as a case *state*
(`fraud_review`, or the sticky `fraud_confirmed`); the block is
lifted only by a named fraud-review decision act over the pinned
score — coded outcomes with coded reasons — never by editing the
score, which is a content-addressed artefact and cannot be edited
into a different band without the re-performance verifier naming it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

SCORING_PACK_VERSION = "2026.10.1"

BAND_CLEAR = "CLEAR"
BAND_MONITOR = "MONITOR"
BAND_FRAUD_REVIEW = "FRAUD_REVIEW"

OUTCOME_CONFIRMED = "confirmed_fraud"
OUTCOME_CLEARED = "cleared_with_reason"

STATE_CLEAR = "clear"
STATE_MONITOR = "monitor"
STATE_FRAUD_REVIEW = "fraud_review"
STATE_FRAUD_CONFIRMED = "fraud_confirmed"
STATE_FRAUD_CLEARED = "fraud_cleared"
BLOCKED_STATES = {STATE_FRAUD_REVIEW, STATE_FRAUD_CONFIRMED}

MISSING_TENDERS = "payment_tender_breakdown"
MISSING_RETURNED_WEIGHT = "returned_weight_grams"
MISSING_PRODUCT_WEIGHT = "product_ship_weight_grams"


class VelocityParams(BaseModel):
    window_days: int
    free_allowance: int
    points_per_case: Decimal
    cap: Decimal


class ShareTier(BaseModel):
    min_share: Decimal
    points: Decimal


class ShareParams(BaseModel):
    min_orders: int
    tiers: list[ShareTier]


class WardrobingParams(BaseModel):
    wear_categories: list[str]
    declared_condition: str
    min_age_days: int
    points_per_case: Decimal
    cap: Decimal


class WeightParams(BaseModel):
    empty_box_below_ratio: Decimal
    empty_box_points: Decimal
    light_below_ratio: Decimal
    light_points: Decimal


class ChurnTier(BaseModel):
    min_distinct: int
    points: Decimal


class ChurnParams(BaseModel):
    tiers: list[ChurnTier]


class BandThreshold(BaseModel):
    band: str
    min_score: Decimal


class ScoringPack(BaseModel):
    version: str
    velocity: VelocityParams
    share: ShareParams
    wardrobing: WardrobingParams
    weight: WeightParams
    churn: ChurnParams
    bands: list[BandThreshold]
    # The review vocabulary: outcome -> the reason codes that outcome
    # may be recorded with. Anything outside this map is refused.
    review_outcomes: dict[str, list[str]]

    def band_for(self, total: Decimal) -> str:
        for threshold in sorted(self.bands, key=lambda b: b.min_score, reverse=True):
            if total >= threshold.min_score:
                return threshold.band
        return BAND_CLEAR

    def fingerprint(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


SCORING_PACK_V1 = ScoringPack(
    version=SCORING_PACK_VERSION,
    velocity=VelocityParams(
        window_days=90, free_allowance=2, points_per_case=Decimal("8"), cap=Decimal("40")
    ),
    share=ShareParams(
        min_orders=4,
        tiers=[
            ShareTier(min_share=Decimal("0.75"), points=Decimal("35")),
            ShareTier(min_share=Decimal("0.50"), points=Decimal("20")),
            ShareTier(min_share=Decimal("0.34"), points=Decimal("8")),
        ],
    ),
    wardrobing=WardrobingParams(
        wear_categories=["apparel", "footwear"],
        declared_condition="new_with_tags",
        min_age_days=21,
        points_per_case=Decimal("12"),
        cap=Decimal("36"),
    ),
    weight=WeightParams(
        empty_box_below_ratio=Decimal("0.50"),
        empty_box_points=Decimal("30"),
        light_below_ratio=Decimal("0.80"),
        light_points=Decimal("10"),
    ),
    churn=ChurnParams(
        tiers=[
            ChurnTier(min_distinct=4, points=Decimal("20")),
            ChurnTier(min_distinct=3, points=Decimal("10")),
        ]
    ),
    bands=[
        BandThreshold(band=BAND_FRAUD_REVIEW, min_score=Decimal("60")),
        BandThreshold(band=BAND_MONITOR, min_score=Decimal("25")),
        BandThreshold(band=BAND_CLEAR, min_score=Decimal("0")),
    ],
    review_outcomes={
        OUTCOME_CONFIRMED: ["pattern_verified", "evidence_conclusive", "customer_admitted"],
        OUTCOME_CLEARED: [
            "legitimate_pattern_explained",
            "data_error_corrected",
            "first_party_mistake",
        ],
    },
)


def load_scoring_pack(version: str | None = None) -> ScoringPack:
    """Fail-closed loader: an unknown version is an error, never a
    silent fallback — a score must name the pack it was taken under."""
    if version is None or version == SCORING_PACK_V1.version:
        return SCORING_PACK_V1
    raise ValueError(f"unknown fraud scoring pack version: {version!r}")


class CaseFact(BaseModel):
    """The scoring-relevant facts of one stored case, as read from
    the store. The engine consumes facts, never store rows, so the
    same facts always produce the same artefact."""

    case_id: str
    order_id: str
    customer_id: str
    sku: str
    qty: int
    category: str | None
    condition_declared: str
    requested_on: date
    delivered_on: date
    returned_weight_grams: int | None = None


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _points(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _before(fact: CaseFact, scored: CaseFact) -> bool:
    """Strict 'counts as history for the scored case' ordering: dated
    on or before it, and — for same-day cases — ordered before it by
    case id, so the count never depends on ingestion order."""
    if fact.case_id == scored.case_id:
        return False
    if fact.requested_on < scored.requested_on:
        return True
    return fact.requested_on == scored.requested_on and fact.case_id < scored.case_id


def _up_to(fact: CaseFact, scored: CaseFact) -> bool:
    return fact.case_id == scored.case_id or _before(fact, scored)


def score_case(
    pack: ScoringPack,
    *,
    scored: CaseFact,
    facts: list[CaseFact],
    orders: list,
    weights_grams: dict[str, int],
    supersedes: str | None = None,
):
    """The pure evaluation. Same pack + same stored artefacts -> the
    same FraudScore, byte for byte. `orders` are the customer's Order
    models as stored; `weights_grams` is the product master's shipped
    weights (seed data of record)."""
    from .models import FraudScore, SignalResult

    as_of = scored.requested_on
    history = sorted(
        (f for f in facts if f.customer_id == scored.customer_id and _up_to(f, scored)),
        key=lambda f: f.case_id,
    )
    priors = [f for f in history if f.case_id != scored.case_id]
    customer_orders = sorted(
        (o for o in orders if o.customer_id == scored.customer_id and o.delivered_on <= as_of),
        key=lambda o: o.order_id,
    )

    signals: list[SignalResult] = []

    # -- return velocity ------------------------------------------------
    vp = pack.velocity
    in_window = [
        f for f in priors if (as_of - f.requested_on).days <= vp.window_days
    ]
    excess = max(0, len(in_window) - vp.free_allowance)
    contribution = min(vp.cap, vp.points_per_case * excess)
    signals.append(
        SignalResult(
            signal_id="return_velocity",
            status="computed",
            raw_value=str(len(in_window)),
            contribution=_points(contribution),
            inputs={
                "window_days": str(vp.window_days),
                "prior_cases_in_window": str(len(in_window)),
                "prior_case_ids": ",".join(f.case_id for f in in_window),
                "free_allowance": str(vp.free_allowance),
                "points_per_case": str(vp.points_per_case),
                "cap": str(vp.cap),
            },
            rule="each prior case in the window beyond the free allowance adds points_per_case, capped",
        )
    )

    # -- serial returner share -------------------------------------------
    sp = pack.share
    if len(customer_orders) < sp.min_orders:
        signals.append(
            SignalResult(
                signal_id="serial_returner_share",
                status="insufficient_data",
                raw_value=None,
                contribution=Decimal("0"),
                inputs={
                    "orders_on_record": str(len(customer_orders)),
                    "min_orders": str(sp.min_orders),
                },
                missing_input=f"customer_order_history>={sp.min_orders}",
                rule="a return share is only a pattern over at least min_orders orders",
            )
        )
    else:
        returned_ids = {f.order_id for f in history}
        share = (Decimal(len(returned_ids)) / Decimal(len(customer_orders))).quantize(
            Decimal("0.0001")
        )
        points = Decimal("0")
        for tier in sorted(sp.tiers, key=lambda t: t.min_share, reverse=True):
            if share >= tier.min_share:
                points = tier.points
                break
        signals.append(
            SignalResult(
                signal_id="serial_returner_share",
                status="computed",
                raw_value=str(share),
                contribution=_points(points),
                inputs={
                    "orders_on_record": str(len(customer_orders)),
                    "orders_returned": str(len(returned_ids)),
                    "returned_order_ids": ",".join(sorted(returned_ids)),
                },
                rule="share of the customer's orders returned, tiered by the pack",
            )
        )

    # -- wardrobing indicator ----------------------------------------------
    wp = pack.wardrobing
    matches = [
        f
        for f in history
        if f.category in wp.wear_categories
        and f.condition_declared == wp.declared_condition
        and (f.requested_on - f.delivered_on).days >= wp.min_age_days
    ]
    contribution = min(wp.cap, wp.points_per_case * len(matches))
    signals.append(
        SignalResult(
            signal_id="wardrobing_indicator",
            status="computed",
            raw_value=str(len(matches)),
            contribution=_points(contribution),
            inputs={
                "wear_categories": ",".join(wp.wear_categories),
                "declared_condition": wp.declared_condition,
                "min_age_days": str(wp.min_age_days),
                "matching_case_ids": ",".join(f.case_id for f in matches),
                "points_per_case": str(wp.points_per_case),
                "cap": str(wp.cap),
            },
            rule="wear-category items declared as the pack's condition after min_age_days of ownership, per case, capped",
        )
    )

    # -- weight mismatch (the scored case's own parcel) ---------------------
    xp = pack.weight
    expected = weights_grams.get(scored.sku)
    if scored.returned_weight_grams is None:
        signals.append(
            SignalResult(
                signal_id="weight_mismatch",
                status="not_computable",
                raw_value=None,
                contribution=Decimal("0"),
                inputs={"sku": scored.sku},
                missing_input=MISSING_RETURNED_WEIGHT,
                rule="the return parcel was never weighed at intake, so no weight comparison exists",
            )
        )
    elif expected is None:
        signals.append(
            SignalResult(
                signal_id="weight_mismatch",
                status="not_computable",
                raw_value=None,
                contribution=Decimal("0"),
                inputs={"sku": scored.sku, "returned_weight_grams": str(scored.returned_weight_grams)},
                missing_input=MISSING_PRODUCT_WEIGHT,
                rule="the product master carries no shipped weight for this sku",
            )
        )
    else:
        expected_total = expected * scored.qty
        ratio = (Decimal(scored.returned_weight_grams) / Decimal(expected_total)).quantize(
            Decimal("0.0001")
        )
        if ratio < xp.empty_box_below_ratio:
            points = xp.empty_box_points
        elif ratio < xp.light_below_ratio:
            points = xp.light_points
        else:
            points = Decimal("0")
        signals.append(
            SignalResult(
                signal_id="weight_mismatch",
                status="computed",
                raw_value=str(ratio),
                contribution=_points(points),
                inputs={
                    "sku": scored.sku,
                    "qty": str(scored.qty),
                    "expected_weight_grams": str(expected_total),
                    "returned_weight_grams": str(scored.returned_weight_grams),
                },
                rule="returned/expected weight ratio: under the empty-box ratio scores empty_box_points, under the light ratio scores light_points",
            )
        )

    # -- card churn ------------------------------------------------------------
    tendered = [o for o in customer_orders if o.tenders]
    if not tendered:
        signals.append(
            SignalResult(
                signal_id="card_churn",
                status="insufficient_data",
                raw_value=None,
                contribution=Decimal("0"),
                inputs={"orders_on_record": str(len(customer_orders))},
                missing_input=MISSING_TENDERS,
                rule="no order on record carries a tender breakdown, so card churn cannot be measured",
            )
        )
    else:
        distinct = sorted({t.method for o in tendered for t in o.tenders})
        points = Decimal("0")
        for tier in sorted(pack.churn.tiers, key=lambda t: t.min_distinct, reverse=True):
            if len(distinct) >= tier.min_distinct:
                points = tier.points
                break
        signals.append(
            SignalResult(
                signal_id="card_churn",
                status="computed",
                raw_value=str(len(distinct)),
                contribution=_points(points),
                inputs={
                    "distinct_tenders": ",".join(distinct),
                    "orders_with_tenders": str(len(tendered)),
                },
                rule="distinct payment tenders across the customer's orders, tiered by the pack",
            )
        )

    total = _points(sum((s.contribution for s in signals), Decimal("0")))
    band = pack.band_for(total)

    fingerprint = hashlib.sha256(
        _canonical(
            {
                "pack_version": pack.version,
                "pack_fingerprint": pack.fingerprint(),
                "scored_case_id": scored.case_id,
                "facts": [f.model_dump(mode="json") for f in history],
                "orders": [
                    {
                        "order_id": o.order_id,
                        "delivered_on": str(o.delivered_on),
                        "tenders": o.model_dump(mode="json")["tenders"],
                    }
                    for o in customer_orders
                ],
                "weights_grams": {scored.sku: weights_grams.get(scored.sku)},
            }
        ).encode()
    ).hexdigest()

    score_id = "FS-" + hashlib.sha256(
        _canonical(
            {
                "case_id": scored.case_id,
                "pack_version": pack.version,
                "inputs_fingerprint": fingerprint,
            }
        ).encode()
    ).hexdigest()[:16]

    return FraudScore(
        score_id=score_id,
        case_id=scored.case_id,
        customer_id=scored.customer_id,
        pack_version=pack.version,
        inputs_fingerprint=fingerprint,
        input_case_ids=[f.case_id for f in history],
        input_order_ids=[o.order_id for o in customer_orders],
        signals=signals,
        total_score=total,
        band=band,
        as_of=as_of,
        supersedes=supersedes,
    )


def verify_fraud_score(score, pack: ScoringPack, *, scored: CaseFact, facts: list[CaseFact], orders: list, weights_grams: dict[str, int]) -> list[str]:
    """Re-perform the score from the pinned inputs and compare.

    Returns the list of problems; empty means the stored artefact is
    exactly what the pack produces from these inputs — a tampered
    band, total or signal contribution is named."""
    expected = score_case(
        pack, scored=scored, facts=facts, orders=orders,
        weights_grams=weights_grams, supersedes=score.supersedes,
    )
    problems: list[str] = []
    if score.score_id != expected.score_id:
        problems.append(
            f"score_id {score.score_id} != recomputed {expected.score_id} "
            "(inputs or pack version changed since this artefact was written)"
        )
    if score.model_dump(mode="json") != expected.model_dump(mode="json"):
        problems.append("stored score differs from a fresh scoring over the same inputs")
    return problems


def derive_fraud_state(band: str, *, current_score_id: str, reviews: list) -> str:
    """The case-level fraud state, derived — never stored opinion.

    A confirmed_fraud decision on ANY score of the case is sticky: it
    survives re-scoring, because new arithmetic does not un-happen a
    fraud finding. A cleared_with_reason decision lifts the block
    only for the score it decided on; a later score that again lands
    in FRAUD_REVIEW blocks again until a new decision is recorded.
    """
    if any(r.outcome == OUTCOME_CONFIRMED for r in reviews):
        return STATE_FRAUD_CONFIRMED
    if band == BAND_FRAUD_REVIEW:
        cleared = any(
            r.score_id == current_score_id and r.outcome == OUTCOME_CLEARED for r in reviews
        )
        return STATE_FRAUD_CLEARED if cleared else STATE_FRAUD_REVIEW
    if band == BAND_MONITOR:
        return STATE_MONITOR
    return STATE_CLEAR
