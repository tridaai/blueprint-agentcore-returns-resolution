"""The disposition matrix — a versioned pack, evaluated purely (R3).

Law 3 made mechanical: where a returned item goes is decided by a
matrix, not by the model and not by an operator's mood. The matrix is
*data*: one cell per (category x declared condition x item age band),
each cell naming a disposition and the resale channel it flows
through. The engine is a pure function over the facts of the case —
category, the customer's DECLARED condition, the item's age in days,
and the channel table's open/closed states — and every decision is a
content-addressed artefact pinned to those facts, the matrix version
and the channel-table version, recording the exact cell that fired.

Three honesty rules, all graded:

- An input combination with no cell is `not_decidable` and names the
  missing dimension (unknown category, unknown declared condition,
  unbandable age, closed channel with no stated next cell) — it is
  never a default guess wearing a disposition's clothes.
- Channel availability is data too. When a cell's channel is closed,
  the walk falls to the matrix's *stated next cell* for that cell and
  records every hop as a reroute step on the decision. There is no
  silent rerouting: the final cell is the one whose channel is open,
  and the path to it is on the record.
- The R3 decision carries `basis: declared` — it rests on the
  customer's declared condition. Warehouse grading (R7) will produce
  a new decision, linked by `supersedes`, on the graded basis; this
  artefact is never edited in place.

The matrix deliberately covers the full grid, including cells the
current policy windows make unreachable at intake (an aged electronics
return is outside its 30-day window, so no eligible case lands there
today): the matrix is the disposition policy of record, and an item
that arrives anyway — a goodwill exception, a late shipment — must
meet a stated cell, not an improvisation.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

MATRIX_VERSION = "2026.10.1"
CHANNEL_TABLE_VERSION = "2026.10.1"
BASIS_DECLARED = "declared"
BASIS_GRADED = "graded"

# Dispositions — the closed vocabulary a decision may name.
RESTOCK = "RESTOCK"
REFURBISH = "REFURBISH"
OUTLET = "OUTLET"
LIQUIDATE = "LIQUIDATE"
RETURN_TO_VENDOR = "RETURN_TO_VENDOR"
RECYCLE = "RECYCLE"
HOLD_FOR_INSPECTION = "HOLD_FOR_INSPECTION"
DISPOSITIONS = {
    RESTOCK,
    REFURBISH,
    OUTLET,
    LIQUIDATE,
    RETURN_TO_VENDOR,
    RECYCLE,
    HOLD_FOR_INSPECTION,
}

# Resale channels — where a disposition physically flows.
CHANNEL_STORE = "store"
CHANNEL_OUTLET = "outlet"
CHANNEL_VENDOR = "vendor"
CHANNEL_LIQUIDATOR = "liquidator"
CHANNEL_RECYCLER = "recycler"
CHANNELS = {CHANNEL_STORE, CHANNEL_OUTLET, CHANNEL_VENDOR, CHANNEL_LIQUIDATOR, CHANNEL_RECYCLER}

# What inspection must establish for a held electronics item before
# any disposition may be re-decided (R7 consumes these by name).
ELECTRONICS_INSPECTION_UNKNOWNS = ["functional_test_result", "data_wipe_status"]


class AgeBand(BaseModel):
    band_id: str
    min_days: int
    max_days: int | None  # None = unbounded


class MatrixCell(BaseModel):
    """One cell of the matrix: an input combination and its outcome.

    `next_cell_id` is the matrix's stated fallback: the cell the walk
    falls to when this cell's channel is closed. A cell with no next
    cell whose channel closes makes the decision `not_decidable`
    naming the resale channel — the matrix does not improvise.
    `fallback_only` cells are reachable only as a stated next cell;
    the primary lookup never selects them directly.
    """

    cell_id: str
    category: str
    condition: str
    age_band: str
    disposition: str
    channel: str
    next_cell_id: str | None = None
    inspection_unknowns: list[str] = []
    fallback_only: bool = False


class MatrixPack(BaseModel):
    version: str
    # The declared-condition vocabulary the matrix covers. A declared
    # condition outside this list is a missing dimension, never a
    # nearest-match guess.
    conditions: list[str]
    age_bands: list[AgeBand]
    cells: list[MatrixCell]

    def band_for(self, age_days: int) -> AgeBand | None:
        for band in self.age_bands:
            if age_days >= band.min_days and (band.max_days is None or age_days <= band.max_days):
                return band
        return None

    def categories(self) -> set[str]:
        return {cell.category for cell in self.cells}

    def primary_cell(self, category: str, condition: str, age_band: str) -> MatrixCell | None:
        for cell in self.cells:
            if (
                not cell.fallback_only
                and cell.category == category
                and cell.condition == condition
                and cell.age_band == age_band
            ):
                return cell
        return None

    def cell_by_id(self, cell_id: str) -> MatrixCell | None:
        for cell in self.cells:
            if cell.cell_id == cell_id:
                return cell
        return None


class ChannelTable(BaseModel):
    """Resale-channel availability, per category — data, versioned.

    A channel absent from a category's row is *unknown*, and the
    engine treats an unknown channel state as a missing dimension
    rather than assuming the channel is open.
    """

    version: str
    states: dict[str, dict[str, str]]  # category -> channel -> open|closed

    def state(self, category: str, channel: str) -> str | None:
        return self.states.get(category, {}).get(channel)

    def fingerprint(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def _cell(
    cell_id: str,
    category: str,
    condition: str,
    age_band: str,
    disposition: str,
    channel: str,
    next_cell_id: str | None = None,
    inspection_unknowns: list[str] | None = None,
    fallback_only: bool = False,
) -> MatrixCell:
    return MatrixCell(
        cell_id=cell_id,
        category=category,
        condition=condition,
        age_band=age_band,
        disposition=disposition,
        channel=channel,
        next_cell_id=next_cell_id,
        inspection_unknowns=inspection_unknowns or [],
        fallback_only=fallback_only,
    )


# The matrix of record. Reading guide: new-with-tags resells at full
# price while the item is current; fashion categories (apparel,
# footwear) stale into the outlet with age, gear barely stales at all;
# used items are refurbished while they are young and liquidated when
# they are old; used electronics are held for inspection at any young
# age because nobody may resell a device whose functional state and
# data-wipe state are unknown, and recycled when old.
MATRIX_PACK_V1 = MatrixPack(
    version=MATRIX_VERSION,
    conditions=["new_with_tags", "like_new", "used"],
    age_bands=[
        AgeBand(band_id="fresh", min_days=0, max_days=30),
        AgeBand(band_id="season", min_days=31, max_days=90),
        AgeBand(band_id="aged", min_days=91, max_days=None),
    ],
    cells=[
        # -- apparel -------------------------------------------------
        _cell("M-APP-NWT-FRESH", "apparel", "new_with_tags", "fresh", RESTOCK, CHANNEL_STORE),
        _cell("M-APP-NWT-SEASON", "apparel", "new_with_tags", "season", RESTOCK, CHANNEL_STORE),
        _cell("M-APP-NWT-AGED", "apparel", "new_with_tags", "aged", OUTLET, CHANNEL_OUTLET),
        _cell("M-APP-LN-FRESH", "apparel", "like_new", "fresh", RESTOCK, CHANNEL_STORE),
        _cell("M-APP-LN-SEASON", "apparel", "like_new", "season", OUTLET, CHANNEL_OUTLET),
        _cell("M-APP-LN-AGED", "apparel", "like_new", "aged", OUTLET, CHANNEL_OUTLET),
        _cell("M-APP-USED-FRESH", "apparel", "used", "fresh", REFURBISH, CHANNEL_STORE),
        _cell("M-APP-USED-SEASON", "apparel", "used", "season", OUTLET, CHANNEL_OUTLET),
        # The apparel liquidator is closed in the shipped channel
        # table; this cell states its next cell openly.
        _cell(
            "M-APP-USED-AGED", "apparel", "used", "aged", LIQUIDATE, CHANNEL_LIQUIDATOR,
            next_cell_id="M-APP-USED-AGED-FB",
        ),
        _cell(
            "M-APP-USED-AGED-FB", "apparel", "used", "aged", RECYCLE, CHANNEL_RECYCLER,
            fallback_only=True,
        ),
        # -- footwear ------------------------------------------------
        _cell("M-FTW-NWT-FRESH", "footwear", "new_with_tags", "fresh", RESTOCK, CHANNEL_STORE),
        _cell("M-FTW-NWT-SEASON", "footwear", "new_with_tags", "season", RESTOCK, CHANNEL_STORE),
        _cell("M-FTW-NWT-AGED", "footwear", "new_with_tags", "aged", OUTLET, CHANNEL_OUTLET),
        _cell("M-FTW-LN-FRESH", "footwear", "like_new", "fresh", RESTOCK, CHANNEL_STORE),
        _cell("M-FTW-LN-SEASON", "footwear", "like_new", "season", RESTOCK, CHANNEL_STORE),
        _cell("M-FTW-LN-AGED", "footwear", "like_new", "aged", OUTLET, CHANNEL_OUTLET),
        _cell("M-FTW-USED-FRESH", "footwear", "used", "fresh", REFURBISH, CHANNEL_STORE),
        _cell("M-FTW-USED-SEASON", "footwear", "used", "season", OUTLET, CHANNEL_OUTLET),
        _cell("M-FTW-USED-AGED", "footwear", "used", "aged", LIQUIDATE, CHANNEL_LIQUIDATOR),
        # -- gear ------------------------------------------------------
        _cell("M-GEAR-NWT-FRESH", "gear", "new_with_tags", "fresh", RESTOCK, CHANNEL_STORE),
        _cell("M-GEAR-NWT-SEASON", "gear", "new_with_tags", "season", RESTOCK, CHANNEL_STORE),
        _cell("M-GEAR-NWT-AGED", "gear", "new_with_tags", "aged", RESTOCK, CHANNEL_STORE),
        _cell("M-GEAR-LN-FRESH", "gear", "like_new", "fresh", RESTOCK, CHANNEL_STORE),
        _cell("M-GEAR-LN-SEASON", "gear", "like_new", "season", OUTLET, CHANNEL_OUTLET),
        _cell("M-GEAR-LN-AGED", "gear", "like_new", "aged", OUTLET, CHANNEL_OUTLET),
        _cell("M-GEAR-USED-FRESH", "gear", "used", "fresh", REFURBISH, CHANNEL_STORE),
        _cell("M-GEAR-USED-SEASON", "gear", "used", "season", REFURBISH, CHANNEL_STORE),
        _cell("M-GEAR-USED-AGED", "gear", "used", "aged", LIQUIDATE, CHANNEL_LIQUIDATOR),
        # -- electronics ----------------------------------------------
        _cell("M-ELEC-NWT-FRESH", "electronics", "new_with_tags", "fresh", RESTOCK, CHANNEL_STORE),
        _cell("M-ELEC-NWT-SEASON", "electronics", "new_with_tags", "season", RESTOCK, CHANNEL_STORE),
        _cell("M-ELEC-NWT-AGED", "electronics", "new_with_tags", "aged", RETURN_TO_VENDOR, CHANNEL_VENDOR),
        _cell("M-ELEC-LN-FRESH", "electronics", "like_new", "fresh", REFURBISH, CHANNEL_STORE),
        _cell("M-ELEC-LN-SEASON", "electronics", "like_new", "season", REFURBISH, CHANNEL_STORE),
        _cell("M-ELEC-LN-AGED", "electronics", "like_new", "aged", OUTLET, CHANNEL_OUTLET),
        _cell(
            "M-ELEC-USED-FRESH", "electronics", "used", "fresh", HOLD_FOR_INSPECTION,
            CHANNEL_STORE, inspection_unknowns=ELECTRONICS_INSPECTION_UNKNOWNS,
        ),
        _cell(
            "M-ELEC-USED-SEASON", "electronics", "used", "season", HOLD_FOR_INSPECTION,
            CHANNEL_STORE, inspection_unknowns=ELECTRONICS_INSPECTION_UNKNOWNS,
        ),
        _cell("M-ELEC-USED-AGED", "electronics", "used", "aged", RECYCLE, CHANNEL_RECYCLER),
    ],
)

# The channel table of record. The apparel liquidator is closed
# (apparel liquidation is suspended in this table's period), which is
# what sends aged used apparel to its matrix-stated next cell — the
# textile recycler — on the record, reroute step and all.
CHANNEL_TABLE_V1 = ChannelTable(
    version=CHANNEL_TABLE_VERSION,
    states={
        "apparel": {
            CHANNEL_STORE: "open",
            CHANNEL_OUTLET: "open",
            CHANNEL_VENDOR: "open",
            CHANNEL_LIQUIDATOR: "closed",
            CHANNEL_RECYCLER: "open",
        },
        "footwear": {
            CHANNEL_STORE: "open",
            CHANNEL_OUTLET: "open",
            CHANNEL_VENDOR: "open",
            CHANNEL_LIQUIDATOR: "open",
            CHANNEL_RECYCLER: "open",
        },
        "gear": {
            CHANNEL_STORE: "open",
            CHANNEL_OUTLET: "open",
            CHANNEL_VENDOR: "open",
            CHANNEL_LIQUIDATOR: "open",
            CHANNEL_RECYCLER: "open",
        },
        "electronics": {
            CHANNEL_STORE: "open",
            CHANNEL_OUTLET: "open",
            CHANNEL_VENDOR: "open",
            CHANNEL_LIQUIDATOR: "open",
            CHANNEL_RECYCLER: "open",
        },
    },
)


def load_matrix_pack(version: str | None = None) -> MatrixPack:
    """Fail-closed loader: an unknown version is an error, never a
    silent fallback — a decision must name the matrix it was taken
    under, exactly as it names the policy pack."""
    if version is None or version == MATRIX_PACK_V1.version:
        return MATRIX_PACK_V1
    raise ValueError(f"unknown disposition matrix version: {version!r}")


def load_channel_table(version: str | None = None) -> ChannelTable:
    if version is None or version == CHANNEL_TABLE_V1.version:
        return CHANNEL_TABLE_V1
    raise ValueError(f"unknown channel table version: {version!r}")


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _decision_id(facts: dict) -> str:
    blob = _canonical(facts)
    return "DD-" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def decide_disposition(
    matrix: MatrixPack,
    channels: ChannelTable,
    *,
    case_id: str,
    computation_id: str | None,
    category: str | None,
    condition_declared: str,
    age_days: int | None,
    basis: str = BASIS_DECLARED,
    graded_condition: str | None = None,
    supersedes: str | None = None,
):
    """The pure evaluation. Same facts -> same artefact, byte for byte.

    Returns a DispositionDecision (imported lazily to keep the model
    module free of engine imports). The walk: locate the primary cell
    for (category, condition, age band); while the current cell's
    channel is closed for the category, record the reroute and fall
    to the cell's stated next cell; the first cell whose channel is
    open decides. Any missing dimension ends the walk as
    `not_decidable` naming that dimension.

    The condition the walk evaluates over is the DECLARED condition
    by default (basis "declared", R3). Warehouse grading (R7) passes
    `graded_condition` with basis "graded": the walk then evaluates
    over the graded condition, the artefact records BOTH conditions
    (what the customer declared, what the warehouse found), and
    `supersedes` links it to the decision it replaces — which is
    never edited. Declared-basis calls produce byte-identical
    artefacts to R3: the graded facts enter the content address only
    on the graded path.
    """
    from .models import DispositionDecision, RerouteStep

    condition = graded_condition if graded_condition is not None else condition_declared

    base_facts: dict = {
        "case_id": case_id,
        "computation_id": computation_id,
        "matrix_version": matrix.version,
        "channel_table_version": channels.version,
        "channel_table_fingerprint": channels.fingerprint(),
        "category": category,
        "condition_declared": condition_declared,
        "age_days": age_days,
        "basis": basis,
    }
    if graded_condition is not None:
        base_facts["graded_condition"] = graded_condition
        base_facts["supersedes"] = supersedes

    def _not_decidable(missing_dimension: str, age_band: str | None, reroutes: list) -> DispositionDecision:
        facts = {**base_facts, "age_band": age_band}
        return DispositionDecision(
            disposition_id=_decision_id(facts),
            case_id=case_id,
            computation_id=computation_id,
            matrix_version=matrix.version,
            channel_table_version=channels.version,
            status="not_decidable",
            disposition=None,
            basis=basis,
            cell_id=None,
            channel=None,
            category=category,
            condition_declared=condition_declared,
            graded_condition=graded_condition,
            age_days=age_days,
            age_band=age_band,
            missing_dimension=missing_dimension,
            reroutes=reroutes,
            supersedes=supersedes,
        )

    if category is None:
        return _not_decidable("category", None, [])
    if category not in matrix.categories():
        return _not_decidable("category", None, [])
    if condition not in matrix.conditions:
        return _not_decidable("condition_declared", None, [])
    if age_days is None:
        return _not_decidable("item_age_band", None, [])
    band = matrix.band_for(age_days)
    if band is None:
        return _not_decidable("item_age_band", None, [])

    cell = matrix.primary_cell(category, condition, band.band_id)
    if cell is None:
        return _not_decidable(
            f"matrix_cell[{category}/{condition}/{band.band_id}]", band.band_id, []
        )

    reroutes: list[RerouteStep] = []
    visited: set[str] = set()
    while True:
        state = channels.state(category, cell.channel)
        if state is None:
            return _not_decidable("resale_channel", band.band_id, reroutes)
        if state == "open":
            break
        # Channel closed: fall to the stated next cell, on the record.
        if cell.cell_id in visited or cell.next_cell_id is None:
            return _not_decidable("resale_channel", band.band_id, reroutes)
        next_cell = matrix.cell_by_id(cell.next_cell_id)
        if next_cell is None:
            return _not_decidable("resale_channel", band.band_id, reroutes)
        visited.add(cell.cell_id)
        reroutes.append(
            RerouteStep(
                from_cell_id=cell.cell_id,
                closed_channel=cell.channel,
                to_cell_id=next_cell.cell_id,
            )
        )
        cell = next_cell

    facts = {**base_facts, "age_band": band.band_id}
    return DispositionDecision(
        disposition_id=_decision_id(facts),
        case_id=case_id,
        computation_id=computation_id,
        matrix_version=matrix.version,
        channel_table_version=channels.version,
        status="decided",
        disposition=cell.disposition,
        basis=basis,
        cell_id=cell.cell_id,
        channel=cell.channel,
        category=category,
        condition_declared=condition_declared,
        graded_condition=graded_condition,
        age_days=age_days,
        age_band=band.band_id,
        missing_dimension=None,
        reroutes=reroutes,
        inspection_unknowns=list(cell.inspection_unknowns),
        supersedes=supersedes,
    )


def verify_disposition(
    decision,
    matrix: MatrixPack,
    channels: ChannelTable,
    *,
    case_id: str,
    computation_id: str | None,
    category: str | None,
    condition_declared: str,
    age_days: int | None,
    basis: str = BASIS_DECLARED,
    graded_condition: str | None = None,
    supersedes: str | None = None,
) -> list[str]:
    """Re-perform the decision from the pinned facts and compare.

    Returns the list of problems; empty means the stored artefact is
    exactly what the matrix produces from these facts — a tampered
    disposition, cell, channel or reroute path is named. Graded-basis
    decisions (R7) are re-performed over the graded facts the
    artefact itself records.
    """
    expected = decide_disposition(
        matrix,
        channels,
        case_id=case_id,
        computation_id=computation_id,
        category=category,
        condition_declared=condition_declared,
        age_days=age_days,
        basis=basis,
        graded_condition=graded_condition,
        supersedes=supersedes,
    )
    problems: list[str] = []
    if decision.disposition_id != expected.disposition_id:
        problems.append(
            f"disposition_id {decision.disposition_id} != recomputed {expected.disposition_id} "
            "(facts or pack versions changed since this artefact was written)"
        )
    if decision.model_dump(mode="json") != expected.model_dump(mode="json"):
        problems.append("stored disposition differs from a fresh decision over the same facts")
    return problems
