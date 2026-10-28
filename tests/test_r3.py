"""R3 gates: the disposition matrix — every row graded against
hand-derived expectations, reroutes on the record (never silent),
not_decidable naming its missing dimension instead of guessing, the
HOLD_FOR_INSPECTION unknowns, and content-addressed artefacts on
every surface (service, SQL store, API, Runtime dispatch)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from returns_resolution.disposition import (
    decide_disposition,
    load_channel_table,
    load_matrix_pack,
    verify_disposition,
)
from returns_resolution.intake import PLANTED_REQUESTS
from returns_resolution.modelport import OfflineComposer
from returns_resolution.models import ReturnRequest
from returns_resolution.service import CaseService
from returns_resolution.store import InMemoryStore, SqlStore

MATRIX = load_matrix_pack()
CHANNELS = load_channel_table()

# Hand-derived from the matrix's stated retail logic (NOT read off the
# engine): new-with-tags resells at full price while current; fashion
# categories stale into the outlet with age, gear barely stales; used
# is refurbished young and liquidated old; used electronics are held
# for inspection young and recycled old; aged sealed electronics go
# back to the vendor. Age representatives: fresh=10d, season=60d,
# aged=120d. (category, condition, age_days) -> (cell, disposition, channel)
EXPECTED = {
    ("apparel", "new_with_tags", 10): ("M-APP-NWT-FRESH", "RESTOCK", "store"),
    ("apparel", "new_with_tags", 60): ("M-APP-NWT-SEASON", "RESTOCK", "store"),
    ("apparel", "new_with_tags", 120): ("M-APP-NWT-AGED", "OUTLET", "outlet"),
    ("apparel", "like_new", 10): ("M-APP-LN-FRESH", "RESTOCK", "store"),
    ("apparel", "like_new", 60): ("M-APP-LN-SEASON", "OUTLET", "outlet"),
    ("apparel", "like_new", 120): ("M-APP-LN-AGED", "OUTLET", "outlet"),
    ("apparel", "used", 10): ("M-APP-USED-FRESH", "REFURBISH", "store"),
    ("apparel", "used", 60): ("M-APP-USED-SEASON", "OUTLET", "outlet"),
    # Primary cell says LIQUIDATE via the liquidator, which the
    # shipped channel table closes for apparel -> the matrix's stated
    # next cell decides: RECYCLE via the recycler, reroute recorded.
    ("apparel", "used", 120): ("M-APP-USED-AGED-FB", "RECYCLE", "recycler"),
    ("footwear", "new_with_tags", 10): ("M-FTW-NWT-FRESH", "RESTOCK", "store"),
    ("footwear", "new_with_tags", 60): ("M-FTW-NWT-SEASON", "RESTOCK", "store"),
    ("footwear", "new_with_tags", 120): ("M-FTW-NWT-AGED", "OUTLET", "outlet"),
    ("footwear", "like_new", 10): ("M-FTW-LN-FRESH", "RESTOCK", "store"),
    ("footwear", "like_new", 60): ("M-FTW-LN-SEASON", "RESTOCK", "store"),
    ("footwear", "like_new", 120): ("M-FTW-LN-AGED", "OUTLET", "outlet"),
    ("footwear", "used", 10): ("M-FTW-USED-FRESH", "REFURBISH", "store"),
    ("footwear", "used", 60): ("M-FTW-USED-SEASON", "OUTLET", "outlet"),
    ("footwear", "used", 120): ("M-FTW-USED-AGED", "LIQUIDATE", "liquidator"),
    ("gear", "new_with_tags", 10): ("M-GEAR-NWT-FRESH", "RESTOCK", "store"),
    ("gear", "new_with_tags", 60): ("M-GEAR-NWT-SEASON", "RESTOCK", "store"),
    ("gear", "new_with_tags", 120): ("M-GEAR-NWT-AGED", "RESTOCK", "store"),
    ("gear", "like_new", 10): ("M-GEAR-LN-FRESH", "RESTOCK", "store"),
    ("gear", "like_new", 60): ("M-GEAR-LN-SEASON", "OUTLET", "outlet"),
    ("gear", "like_new", 120): ("M-GEAR-LN-AGED", "OUTLET", "outlet"),
    ("gear", "used", 10): ("M-GEAR-USED-FRESH", "REFURBISH", "store"),
    ("gear", "used", 60): ("M-GEAR-USED-SEASON", "REFURBISH", "store"),
    ("gear", "used", 120): ("M-GEAR-USED-AGED", "LIQUIDATE", "liquidator"),
    ("electronics", "new_with_tags", 10): ("M-ELEC-NWT-FRESH", "RESTOCK", "store"),
    ("electronics", "new_with_tags", 60): ("M-ELEC-NWT-SEASON", "RESTOCK", "store"),
    ("electronics", "new_with_tags", 120): ("M-ELEC-NWT-AGED", "RETURN_TO_VENDOR", "vendor"),
    ("electronics", "like_new", 10): ("M-ELEC-LN-FRESH", "REFURBISH", "store"),
    ("electronics", "like_new", 60): ("M-ELEC-LN-SEASON", "REFURBISH", "store"),
    ("electronics", "like_new", 120): ("M-ELEC-LN-AGED", "OUTLET", "outlet"),
    ("electronics", "used", 10): ("M-ELEC-USED-FRESH", "HOLD_FOR_INSPECTION", "store"),
    ("electronics", "used", 60): ("M-ELEC-USED-SEASON", "HOLD_FOR_INSPECTION", "store"),
    ("electronics", "used", 120): ("M-ELEC-USED-AGED", "RECYCLE", "recycler"),
}


def decide(category, condition, age_days, channels=None, case_id="CASE-T", computation_id="RC-T"):
    return decide_disposition(
        MATRIX,
        channels or CHANNELS,
        case_id=case_id,
        computation_id=computation_id,
        category=category,
        condition_declared=condition,
        age_days=age_days,
    )


def make_service(store=None) -> CaseService:
    service = CaseService(store or InMemoryStore(), OfflineComposer())
    service.seed_orders()
    return service


# -- the matrix as data ------------------------------------------------------


def test_matrix_grid_is_complete_and_closed():
    from returns_resolution.disposition import CHANNELS as CHANNEL_VOCAB
    from returns_resolution.disposition import DISPOSITIONS

    primaries = [c for c in MATRIX.cells if not c.fallback_only]
    assert len(primaries) == 36
    for category in ("apparel", "footwear", "gear", "electronics"):
        for condition in MATRIX.conditions:
            for band in ("fresh", "season", "aged"):
                matches = [
                    c for c in primaries
                    if (c.category, c.condition, c.age_band) == (category, condition, band)
                ]
                assert len(matches) == 1, (category, condition, band)
    for cell in MATRIX.cells:
        assert cell.disposition in DISPOSITIONS, cell.cell_id
        assert cell.channel in CHANNEL_VOCAB, cell.cell_id
        if cell.next_cell_id is not None:
            assert MATRIX.cell_by_id(cell.next_cell_id) is not None, cell.cell_id
        if cell.disposition == "HOLD_FOR_INSPECTION":
            assert cell.inspection_unknowns, f"{cell.cell_id} must name what inspection establishes"
        else:
            assert cell.inspection_unknowns == [], cell.cell_id


def test_loaders_are_fail_closed():
    with pytest.raises(ValueError):
        load_matrix_pack("1999.01.1")
    with pytest.raises(ValueError):
        load_channel_table("1999.01.1")


# -- every row, graded --------------------------------------------------------


@pytest.mark.parametrize("facts,expected", EXPECTED.items())
def test_every_matrix_row_decides_as_hand_derived(facts, expected):
    category, condition, age_days = facts
    cell_id, disposition, channel = expected
    decision = decide(category, condition, age_days)
    assert decision.status == "decided"
    assert (decision.cell_id, decision.disposition, decision.channel) == (cell_id, disposition, channel)
    assert decision.basis == "declared"
    assert decision.matrix_version == MATRIX.version
    assert decision.channel_table_version == CHANNELS.version
    assert decision.supersedes is None


def test_age_band_edges_are_exact():
    assert decide("gear", "used", 30).age_band == "fresh"
    assert decide("gear", "used", 31).age_band == "season"
    assert decide("gear", "used", 90).age_band == "season"
    assert decide("gear", "used", 91).age_band == "aged"
    assert decide("gear", "used", 0).age_band == "fresh"


# -- reroutes: stated, recorded, never silent ---------------------------------


def test_closed_channel_falls_to_the_stated_next_cell_on_the_record():
    decision = decide("apparel", "used", 120)
    assert decision.disposition == "RECYCLE"
    assert len(decision.reroutes) == 1
    step = decision.reroutes[0]
    assert (step.from_cell_id, step.closed_channel, step.to_cell_id) == (
        "M-APP-USED-AGED",
        "liquidator",
        "M-APP-USED-AGED-FB",
    )


def test_a_reroute_that_happens_is_never_silent_and_vice_versa():
    for facts in EXPECTED:
        decision = decide(*facts)
        primary = MATRIX.primary_cell(facts[0], facts[1], decision.age_band)
        if decision.cell_id != primary.cell_id:
            assert decision.reroutes, f"{facts}: landed on another cell with no reroute record"
            assert decision.reroutes[-1].to_cell_id == decision.cell_id
        else:
            assert decision.reroutes == [], f"{facts}: phantom reroute on a direct decision"


def test_closed_channel_with_no_stated_next_is_not_decidable():
    table = CHANNELS.model_copy(deep=True)
    table.states["footwear"]["outlet"] = "closed"
    decision = decide("footwear", "new_with_tags", 120, channels=table)
    assert decision.status == "not_decidable"
    assert decision.missing_dimension == "resale_channel"
    assert decision.disposition is None


def test_reroute_chain_that_dead_ends_is_not_decidable_with_the_hop_recorded():
    table = CHANNELS.model_copy(deep=True)
    table.states["apparel"]["recycler"] = "closed"  # liquidator already closed
    decision = decide("apparel", "used", 120, channels=table)
    assert decision.status == "not_decidable"
    assert decision.missing_dimension == "resale_channel"
    assert len(decision.reroutes) == 1  # the hop to the fallback cell is on the record


# -- not decidable: the missing dimension is named -----------------------------


def test_unknown_condition_is_not_decidable_naming_the_dimension():
    decision = decide("gear", "waterlogged", 10)
    assert decision.status == "not_decidable"
    assert decision.missing_dimension == "condition_declared"
    assert decision.disposition is None and decision.cell_id is None


def test_unknown_category_is_not_decidable_naming_the_dimension():
    assert decide("furniture", "used", 10).missing_dimension == "category"
    assert decide(None, "used", 10).missing_dimension == "category"


def test_unbandable_age_is_not_decidable_naming_the_dimension():
    assert decide("gear", "used", None).missing_dimension == "item_age_band"
    assert decide("gear", "used", -1).missing_dimension == "item_age_band"


def test_hold_for_inspection_names_what_inspection_must_establish():
    decision = decide("electronics", "used", 45)
    assert decision.disposition == "HOLD_FOR_INSPECTION"
    assert decision.inspection_unknowns == ["functional_test_result", "data_wipe_status"]


# -- content addressing + tamper ------------------------------------------------


def test_decision_is_deterministic_and_content_addressed():
    first = decide("gear", "used", 60)
    second = decide("gear", "used", 60)
    assert first.disposition_id == second.disposition_id
    assert first.model_dump() == second.model_dump()
    other_table = CHANNELS.model_copy(deep=True)
    other_table.states["gear"]["outlet"] = "closed"
    assert decide("gear", "used", 60, channels=other_table).disposition_id != first.disposition_id


def test_tampering_with_a_stored_decision_is_detected():
    decision = decide("footwear", "used", 120)
    facts = dict(
        case_id="CASE-T", computation_id="RC-T", category="footwear",
        condition_declared="used", age_days=120,
    )
    assert verify_disposition(decision, MATRIX, CHANNELS, **facts) == []
    tampered = decision.model_copy(deep=True)
    tampered.disposition = "RECYCLE"
    assert verify_disposition(tampered, MATRIX, CHANNELS, **facts)
    tampered = decision.model_copy(deep=True)
    tampered.cell_id = "M-FTW-USED-FRESH"
    assert verify_disposition(tampered, MATRIX, CHANNELS, **facts)


# -- service: dispositions land at ingest, for eligible cases only --------------


def test_feed_cases_carry_their_dispositions():
    service = make_service()
    for request in PLANTED_REQUESTS:
        service.ingest(request)
    d2001 = service.get_disposition("CASE-2001")
    assert (d2001.disposition, d2001.cell_id) == ("RESTOCK", "M-APP-NWT-FRESH")
    d2003 = service.get_disposition("CASE-2003")
    assert (d2003.disposition, d2003.cell_id) == ("RESTOCK", "M-FTW-LN-SEASON")
    d2006 = service.get_disposition("CASE-2006")
    assert d2006.disposition == "HOLD_FOR_INSPECTION"
    assert d2006.inspection_unknowns == ["functional_test_result", "data_wipe_status"]
    assert d2006.computation_id == service.get_computation("CASE-2006").computation_id
    # The case payload itself carries the artefact.
    assert service.get_case("CASE-2006").disposition.disposition_id == d2006.disposition_id
    assert service.verify_stored_disposition("CASE-2006") == []


def test_ineligible_cases_get_no_disposition():
    service = make_service()
    service.ingest(PLANTED_REQUESTS[3])  # REQ-2004, window expired
    assert service.get_disposition("CASE-2004") is None
    assert service.get_case("CASE-2004").disposition is None


def test_undecidable_facts_are_stored_as_a_named_not_decidable():
    service = make_service()
    request = ReturnRequest(
        request_id="REQ-2098", order_id="ORD-1007", sku="STOVE-PKT", qty=1,
        reason_code="changed_mind", condition_declared="waterlogged",
        requested_on=date(2026, 10, 25),
    )
    service.ingest(request)
    decision = service.get_disposition("CASE-2098")
    assert decision.status == "not_decidable"
    assert decision.missing_dimension == "condition_declared"
    assert decision.disposition is None


def test_ingest_stays_idempotent_with_dispositions():
    service = make_service()
    service.ingest(PLANTED_REQUESTS[0])
    service.ingest(PLANTED_REQUESTS[0])
    assert len(service.list_dispositions()) == 1


def test_disposition_persists_through_the_sql_store(tmp_path):
    path = tmp_path / "returns.db"
    store = SqlStore(sqlite_path=path)
    assert store.migrate() == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011"]
    assert store.migrate() == []
    service = CaseService(store, OfflineComposer())
    service.seed_orders()
    service.ingest(PLANTED_REQUESTS[5])  # REQ-2006

    reopened = SqlStore(sqlite_path=path)
    stored = reopened.get_disposition("CASE-2006")
    assert stored["disposition"] == "HOLD_FOR_INSPECTION"
    assert stored["matrix_version"] == MATRIX.version


# -- API + Runtime parity ---------------------------------------------------------


def test_api_and_runtime_disposition_surfaces():
    from returns_resolution.api import create_app
    from returns_resolution.runtime_app import handle_payload

    service = make_service()
    client = TestClient(create_app(service))
    client.post("/intake/feed")

    view = client.get("/cases/CASE-2006/disposition").json()
    assert view["disposition"] == "HOLD_FOR_INSPECTION"
    assert view["inspection_unknowns"] == ["functional_test_result", "data_wipe_status"]
    verify = client.get("/cases/CASE-2006/disposition/verify").json()
    assert verify == {"case_id": "CASE-2006", "ok": True, "problems": []}
    assert client.get("/cases/CASE-2004/disposition").status_code == 404
    assert client.get("/cases/CASE-9999/disposition").status_code == 404

    matrix = client.get("/disposition/matrix").json()
    assert matrix["version"] == MATRIX.version
    assert len(matrix["cells"]) == 37
    cell = client.get("/disposition/matrix/cells/M-APP-USED-AGED").json()
    assert cell["next_cell_id"] == "M-APP-USED-AGED-FB"
    assert client.get("/disposition/matrix/cells/M-NOPE").status_code == 404
    channels = client.get("/disposition/channels").json()
    assert channels["states"]["apparel"]["liquidator"] == "closed"
    assert len(client.get("/dispositions").json()) == 3  # the eligible feed cases

    out = handle_payload(service, {"action": "get_disposition", "case_id": "CASE-2006"})
    assert out["disposition"]["disposition"] == "HOLD_FOR_INSPECTION"
    out = handle_payload(service, {"action": "verify_disposition", "case_id": "CASE-2006"})
    assert out["ok"] is True
    out = handle_payload(service, {"action": "get_matrix_cell", "cell_id": "M-ELEC-NWT-AGED"})
    assert out["cell"]["disposition"] == "RETURN_TO_VENDOR"
    out = handle_payload(service, {"action": "get_channels"})
    assert out["channels"]["version"] == CHANNELS.version
    out = handle_payload(service, {"action": "list_dispositions"})
    assert len(out["dispositions"]) == 3
