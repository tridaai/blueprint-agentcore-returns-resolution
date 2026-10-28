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
- A label reference is a hash of the exact issue inputs, including
  the caller's per-act `request_ref`: replaying one issuance act
  returns its original reference, while a fresh act (a re-issue
  after a void, under a new request_ref) gets a fresh reference —
  the way a real carrier issues a new tracking number per label.
- Tracking events are a fixed script per label — tendered on the
  issue date, in transit the next day, received three days after
  issue — and a read at `as_of` sees only the events whose day has
  arrived. Nothing is random and nothing reads a wall clock.
- R18: a label's script can be set to one of the exception
  scripts (`set_script`) — the journeys the fixed script cannot
  express: a parcel never tendered, one whose scans stop in
  transit, one the carrier records damaged in transit before it
  is received. Scripting is the double's stand-in for the real
  carrier's behaviour on those journeys; the service reads only
  the events, never the script.
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

# The named scripts a label's journey can follow (R18). "standard"
# is the script above — every label's default. The exception
# scripts are how the double stands in for the journeys that break:
# the parcel is never handed over at all, its scans stop in
# transit and it is never received, or the carrier records it
# damaged in transit on the way in (and it is received, damaged).
SCRIPT_STANDARD = "standard"
SCRIPT_NEVER_TENDERED = "never_tendered"
SCRIPT_STALLED_IN_TRANSIT = "stalled_in_transit"
SCRIPT_DAMAGED_IN_TRANSIT = "damaged_in_transit"
_EVENT_SCRIPTS: dict[str, list] = {
    SCRIPT_STANDARD: _EVENT_SCRIPT,
    SCRIPT_NEVER_TENDERED: [],
    SCRIPT_STALLED_IN_TRANSIT: [
        (0, time(9, 0), "tendered", "parcel tendered to the carrier"),
        (1, time(14, 0), "in_transit", "parcel in transit to the returns centre"),
    ],
    SCRIPT_DAMAGED_IN_TRANSIT: [
        (0, time(9, 0), "tendered", "parcel tendered to the carrier"),
        (1, time(14, 0), "in_transit", "parcel in transit to the returns centre"),
        (2, time(8, 15), "damaged", "parcel recorded damaged in transit by the carrier"),
        (3, time(10, 30), "received", "parcel received at the returns centre"),
    ],
}


class CarrierPort(Protocol):
    """The port contract. Production binds a real carrier integration
    here; the local double below is the offline stand-in."""

    provider_name: str

    def create_label(
        self,
        *,
        case_id: str,
        request_ref: str,
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
      issue inputs (including the caller's per-act request_ref)
      produce the same reference, and `label_count` counts distinct
      references actually issued.
    - Voiding is by reference; voiding an unknown or already-voided
      reference raises, and a voided label's event script stops —
      a voided parcel is never reported received.
    - Events are the label's script (the fixed script above by
      default); `fetch_events` at `as_of` returns only the events
      whose day has arrived, in order. `set_script` re-scripts an
      issued label's journey — the stand-in for the carrier
      behaving as it does on the journeys that break (R18).
    """

    provider_name = DOUBLE_CARRIER

    def __init__(self) -> None:
        self._labels: dict[str, dict] = {}
        self._voided: set[str] = set()
        self._scripts: dict[str, str] = {}
        self._pickups: dict[str, CarrierPickupResult] = {}
        self.label_count = 0

    def set_script(self, *, carrier_ref: str, script: str) -> None:
        """Set the journey script for one issued label (R18): one
        of the named scripts in this module. Scripting an unknown
        reference or an unknown script raises — a test that means
        to break a journey must say so exactly."""
        if carrier_ref not in self._labels:
            raise CarrierProviderError(f"unknown carrier reference: {carrier_ref}")
        if script not in _EVENT_SCRIPTS:
            raise CarrierProviderError(
                f"unknown event script {script!r} — the double knows: "
                f"{', '.join(sorted(_EVENT_SCRIPTS))}"
            )
        self._scripts[carrier_ref] = script

    def script_for(self, carrier_ref: str) -> str:
        return self._scripts.get(carrier_ref, SCRIPT_STANDARD)

    def create_label(
        self,
        *,
        case_id: str,
        request_ref: str,
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
                "request_ref": request_ref,
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
        script = _EVENT_SCRIPTS[self._scripts.get(carrier_ref, SCRIPT_STANDARD)]
        events: list[CarrierEvent] = []
        for days, at_time, kind, detail in script:
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
