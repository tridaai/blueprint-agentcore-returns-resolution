"""Intake port + reference feed.

In production the intake port is fed by the retailer's order/returns
systems (behind AgentCore Gateway targets in a later round). The
reference feed below delivers the planted requests against the seed
orders. Ingestion is idempotent by request_id: re-delivering a request
returns the existing case instead of deciding twice.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from .models import ReturnRequest


class IntakePort(Protocol):
    def fetch_requests(self) -> list[ReturnRequest]: ...


PLANTED_REQUESTS: list[ReturnRequest] = [
    ReturnRequest(
        request_id="REQ-2001", order_id="ORD-1001", sku="JKT-ALPINE", qty=1,
        reason_code="changed_mind", condition_declared="new_with_tags",
        requested_on=date(2026, 10, 25),
    ),
    ReturnRequest(
        request_id="REQ-2002", order_id="ORD-1002", sku="TENT-2P", qty=1,
        reason_code="changed_mind", condition_declared="used",
        requested_on=date(2026, 10, 25),
    ),
    # Edge pair — see seed.py. REQ-2003 is on its last eligible day,
    # REQ-2004 is one day past its own window.
    ReturnRequest(
        request_id="REQ-2003", order_id="ORD-1003", sku="BOOT-TRL", qty=1,
        reason_code="wrong_size", condition_declared="like_new",
        requested_on=date(2026, 10, 26),
    ),
    ReturnRequest(
        request_id="REQ-2004", order_id="ORD-1004", sku="BOOT-TRL", qty=1,
        reason_code="wrong_size", condition_declared="like_new",
        requested_on=date(2026, 10, 26),
    ),
    ReturnRequest(
        request_id="REQ-2005", order_id="ORD-1005", sku="PANT-CLR", qty=1,
        reason_code="changed_mind", condition_declared="new_with_tags",
        requested_on=date(2026, 10, 25),
    ),
    ReturnRequest(
        request_id="REQ-2006", order_id="ORD-1006", sku="LAMP-LMN", qty=1,
        reason_code="defective", condition_declared="used",
        requested_on=date(2026, 10, 25),
    ),
    ReturnRequest(
        request_id="REQ-2007", order_id="ORD-1007", sku="STOVE-PKT", qty=1,
        reason_code="changed_mind", condition_declared="damaged_by_customer",
        requested_on=date(2026, 10, 25),
    ),
    ReturnRequest(
        request_id="REQ-2008", order_id="ORD-1008", sku="GC-50", qty=1,
        reason_code="changed_mind", condition_declared="new_with_tags",
        requested_on=date(2026, 10, 25),
    ),
]


class ReferenceFeed:
    """The planted feed. `requests` is injectable for tests/evals."""

    def __init__(self, requests: list[ReturnRequest] | None = None):
        self._requests = requests if requests is not None else list(PLANTED_REQUESTS)

    def fetch_requests(self) -> list[ReturnRequest]:
        return list(self._requests)


# The live fraud-history requests (R4): the current returns of the
# two planted history customers (see seed.py's fraud history). They
# are deliberately NOT part of PLANTED_REQUESTS — they are only
# meaningful once the history seed is loaded, and surfaces ingest
# them explicitly.
FRAUD_FEED_REQUESTS: list[ReturnRequest] = [
    ReturnRequest(
        request_id="REQ-3008", order_id="ORD-3008", sku="BOOT-TRL", qty=1,
        reason_code="changed_mind", condition_declared="used",
        requested_on=date(2026, 10, 24),
    ),
    ReturnRequest(
        request_id="REQ-3103", order_id="ORD-3103", sku="JKT-ALPINE", qty=1,
        reason_code="changed_mind", condition_declared="new_with_tags",
        requested_on=date(2026, 10, 23),
    ),
]
