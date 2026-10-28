"""The carrier port — Law 6's other half (R6).

Labels and pickups happen through tools (gateway.py); the tools act
through THIS port. Its contract is carrier-shaped: a case, a service
level, a weight in, a carrier reference and a label cost out;
tracking events read back by reference. The only implementation in
this build is `LocalCarrierDouble` — A LOCAL DOUBLE, a stand-in for
tests and demos. It ships nothing and books nothing. In production
the port is bound to the retailer's real carrier integration (behind
an AgentCore Gateway target), and that integration — never this
double — is what prints labels and moves parcels.

Determinism is the double's contract, so behaviour is gradable:
- A label reference is a hash of the exact issue inputs; issuing the
  same parcel twice at the port returns the same reference.
- Tracking events are a fixed script per label — tendered on the
  issue date, in transit the next day, received three days after
  issue — and a read at `as_of` sees only the events whose day has
  arrived. Nothing is random and nothing reads a wall clock.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from .models import CarrierEvent, money

# The provider name every artefact written through the double
# carries. It says what it is: a local double, not a carrier.
DOUBLE_CARRIER = "local-double (carrier stand-in)"


class CarrierProviderError(RuntimeError):
    """The carrier provider refused or failed. Nothing shipped."""


class CarrierLabelResult(BaseModel):
    """What the port returns when a label is issued."""

    provider: str
    carrier_ref: str


class CarrierPickupResult(BaseModel):
    """What the port returns when a pickup is booked."""

    provider: str
    carrier_ref: str


# The event script, as (days after issue, time of day, kind, detail).
_EVENT_SCRIPT = [
    (0, time(9, 0), "tendered", "parcel tendered to the carrier"),
    (1, time(14, 0), "in_transit", "parcel in transit to the returns centre"),
    (3, time(10, 30), "received", "parcel received at the returns centre"),
]


class CarrierPort(Protocol):
    """The port contract. Production binds a real carrier integration
    here; the local double below is the offline stand-in."""

    provider_name: str

    def create_label(
        self,
        *,
        case_id: str,
        service_level: str,
        category: str,
        weight_grams: int,
        cost: Decimal,
        issued_on: date,
    ) -> CarrierLabelResult: ...

    def void_label(self, *, carrier_ref: str) -> None: ...

    def fetch_events(
        self, *, carrier_ref: str, label_id: str, issued_on: date, as_of: date, voided: bool = False
    ) -> list[CarrierEvent]: ...

    def book_pickup(
        self, *, case_id: str, carrier_ref: str, scheduled_on: date, window: str
    ) -> CarrierPickupResult: ...


def _ref(prefix: str, payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return prefix + "-" + hashlib.sha256(blob.encode()).hexdigest()[:16]


class LocalCarrierDouble:
    """LOCAL DOUBLE for the carrier port — ships NO real parcels.

    Stands in for the retailer's carrier integration so the label
    gates, the idempotency contract and the tracking reads can be
    built and graded offline. Production label printing and pickups
    ride the configured carrier integration, never this class.

    Behaviour contract:
    - Label references are content-derived and stable: the same
      issue inputs produce the same reference, and `label_count`
      counts distinct references actually issued.
    - Voiding is by reference; voiding an unknown or already-voided
      reference raises, and a voided label's event script stops —
      a voided parcel is never reported received.
    - Events are the fixed script above; `fetch_events` at `as_of`
      returns only the events whose day has arrived, in order.
    """

    provider_name = DOUBLE_CARRIER

    def __init__(self) -> None:
        self._labels: dict[str, dict] = {}
        self._voided: set[str] = set()
        self._pickups: dict[str, CarrierPickupResult] = {}
        self.label_count = 0

    def create_label(
        self,
        *,
        case_id: str,
        service_level: str,
        category: str,
        weight_grams: int,
        cost: Decimal,
        issued_on: date,
    ) -> CarrierLabelResult:
        ref = _ref(
            "double-label",
            {
                "case_id": case_id,
                "service_level": service_level,
                "category": category,
                "weight_grams": weight_grams,
                "cost": str(money(cost)),
                "issued_on": issued_on.isoformat(),
            },
        )
        if ref not in self._labels:
            self._labels[ref] = {
                "case_id": case_id,
                "issued_on": issued_on,
            }
            self.label_count += 1
        return CarrierLabelResult(provider=self.provider_name, carrier_ref=ref)

    def void_label(self, *, carrier_ref: str) -> None:
        if carrier_ref not in self._labels:
            raise CarrierProviderError(f"unknown carrier reference: {carrier_ref}")
        if carrier_ref in self._voided:
            raise CarrierProviderError(f"carrier reference {carrier_ref} is already voided")
        self._voided.add(carrier_ref)

    def fetch_events(
        self, *, carrier_ref: str, label_id: str, issued_on: date, as_of: date, voided: bool = False
    ) -> list[CarrierEvent]:
        # The script is a pure function of the label's issue date —
        # held on the label artefact of record — so a label issued by
        # another port instance (e.g. before a restart) reads back
        # identically. A voided parcel is never reported received.
        is_voided = voided or carrier_ref in self._voided
        events: list[CarrierEvent] = []
        for days, at_time, kind, detail in _EVENT_SCRIPT:
            if is_voided and kind == "received":
                continue
            day = issued_on + timedelta(days=days)
            if day > as_of:
                continue
            at = datetime.combine(day, at_time, tzinfo=timezone.utc)
            events.append(
                CarrierEvent(
                    event_id=_ref("evt", {"carrier_ref": carrier_ref, "kind": kind}),
                    label_id=label_id,
                    carrier_ref=carrier_ref,
                    kind=kind,
                    at=at.isoformat(),
                    detail=detail,
                )
            )
        return events

    def book_pickup(
        self, *, case_id: str, carrier_ref: str, scheduled_on: date, window: str
    ) -> CarrierPickupResult:
        ref = _ref(
            "double-pickup",
            {
                "case_id": case_id,
                "carrier_ref": carrier_ref,
                "scheduled_on": scheduled_on.isoformat(),
                "window": window,
            },
        )
        if ref not in self._pickups:
            self._pickups[ref] = CarrierPickupResult(provider=self.provider_name, carrier_ref=ref)
        return self._pickups[ref]
