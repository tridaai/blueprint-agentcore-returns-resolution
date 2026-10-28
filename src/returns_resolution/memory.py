"""Customer memory — Law 7 made mechanical (R9).

Return history lives behind a memory port shaped on AgentCore
Memory's long-term records. What memory holds is EVENT-shaped
summaries, derived in code from the case's artefacts of record at
four closure milestones — `case_decided`, `refund_executed`,
`case_graded`, `fraud_review_closed` — never prose, never a copy
of the case file. A record states the facts as recorded: the
eligibility outcome and refund total at decision time, the amount
that actually moved, the graded condition and the disposition, the
fraud BAND (never the score's internals beyond the band — memory
is not a second scoring pack) — and, on the decision record, the
scoring fact exactly as the fraud engine scored it, so history can
be reconstructed from memory alone (see fraud.HistoryFacts).

Memory has LIMITS, and the limits are the port's shape, not a
policy hope:

- **One customer at a time.** Retrieval takes exactly one customer
  id; the store read underneath is predicated on that id. There is
  no cross-customer query surface anywhere on the port — a probe
  for another customer's records returns that customer's records
  (none, for a stranger) and the attempt itself is logged.
- **Every retrieval is logged.** Each read appends an access-log
  entry — customer, purpose, actor, the records returned — before
  the records are handed over. A retrieval that leaves no entry
  does not exist, because there is no other read path.
- **Retention is a window, not a deletion.** Records carry an
  expiry from the memory pack (versioned config data). Expired
  records are excluded from retrieval and counted on a tombstone
  read — they are never silently deleted, and the record store
  stays append-only.

The only implementation in this build is `LocalMemoryDouble` —
A LOCAL DOUBLE, labelled as such on every record it writes. It
stores nothing in any real memory service. In production the port
is bound to AgentCore Memory; the record shapes, the scoping and
the access log do not change.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, field_validator

from .fraud import CaseFact

MEMORY_PACK_VERSION = "2026.10.1"

EVENT_CASE_DECIDED = "case_decided"
EVENT_REFUND_EXECUTED = "refund_executed"
EVENT_CASE_GRADED = "case_graded"
EVENT_FRAUD_REVIEW_CLOSED = "fraud_review_closed"

MEMORY_EVENTS = [
    EVENT_CASE_DECIDED,
    EVENT_REFUND_EXECUTED,
    EVENT_CASE_GRADED,
    EVENT_FRAUD_REVIEW_CLOSED,
]

DOUBLE_MEMORY = "local-double (AgentCore Memory stand-in)"


class MemoryPack(BaseModel):
    """Memory configuration as versioned data: the retention
    window every record's expiry is computed from. A retention
    change is a new pack version — records written under an older
    pack keep the expiry they were written with, stated on the
    record itself."""

    version: str
    retention_days: int


MEMORY_PACK_V1 = MemoryPack(version=MEMORY_PACK_VERSION, retention_days=730)


def load_memory_pack(version: str | None = None) -> MemoryPack:
    """Fail-closed loader: an unknown version is an error, never a
    silent fall back — a record must name the retention rules it
    was written under."""
    if version is None or version == MEMORY_PACK_V1.version:
        return MEMORY_PACK_V1
    raise ValueError(f"unknown memory pack version: {version!r}")


class MemoryRecord(BaseModel):
    """One long-term memory record (R9) — an event-shaped summary
    of a case milestone, derived from the artefacts of record.

    Content-addressed: the id hashes the record's own contents, so
    re-deriving the same milestone from the same artefacts yields
    the same record and the write is a no-op — memory is
    append-only and a record, once written, is never edited.
    `expires_on` is `occurred_on` plus the pack's retention
    window, computed at write time and carried on the record.
    """

    record_id: str
    customer_id: str
    case_id: str
    event_type: str  # one of MEMORY_EVENTS
    occurred_on: date
    recorded_on: date
    expires_on: date
    memory_pack_version: str
    provider: str  # the writing port implementation's label
    source_ref: str  # the artefact this record derives from
    # The facts, as recorded — populated per event type:
    eligible: bool | None = None  # case_decided
    refund_total: Decimal | None = None  # case_decided (the computation's total)
    refund_amount: Decimal | None = None  # refund_executed (the receipt's amount)
    disposition: str | None = None  # case_decided / case_graded (when decided)
    graded_condition: str | None = None  # case_graded
    received_weight_grams: int | None = None  # case_graded (the warehouse weigh-in)
    refund_delta: Decimal | None = None  # case_graded
    fraud_band: str | None = None  # case_decided / fraud_review_closed
    review_outcome: str | None = None  # fraud_review_closed
    # case_decided only: the scoring fact exactly as the fraud
    # engine consumed it, so the engine's history can be rebuilt
    # from memory records (with the warehouse weigh-in of a later
    # case_graded record superseding the intake weight, exactly as
    # the store path derives it).
    fact: CaseFact | None = None

    @field_validator("refund_total", "refund_amount", "refund_delta", mode="before")
    @classmethod
    def _decimal(cls, v: object) -> Decimal | None:
        if v is None:
            return None
        return Decimal(str(v)).quantize(Decimal("0.01"))


class MemoryAccessEntry(BaseModel):
    """One logged retrieval (R9). Appended by the port BEFORE the
    records are handed over: customer, purpose, actor, the read's
    as-of date, exactly which records were returned, and how many
    stored records retention excluded from this read."""

    entry_id: str
    seq: int  # the log's append order (the count at write time)
    customer_id: str
    purpose: str
    actor: str
    as_of: date
    returned_record_ids: list[str] = []
    expired_excluded: int = 0


def assign_record_id(record: MemoryRecord) -> MemoryRecord:
    """Content-address a record: the id hashes everything the
    record states except the id itself."""
    payload = record.model_dump(mode="json")
    payload.pop("record_id", None)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    record.record_id = "MR-" + hashlib.sha256(blob.encode()).hexdigest()[:16]
    return record


def expiry_for(pack: MemoryPack, occurred_on: date) -> date:
    return occurred_on + timedelta(days=pack.retention_days)


class MemoryPort(Protocol):
    """The port contract. Production binds AgentCore Memory here;
    the local double below is the offline stand-in.

    Note what the contract does NOT have: no list-everything read,
    no search across customers, no delete. Retrieval is by exact
    customer id, every retrieval is logged, and expired records
    leave through a tombstone count, never through erasure. The
    write-side helpers (`find`, `existing_keys`) expose ids and
    keys only — no facts cross them.
    """

    provider_name: str

    def write(self, record: MemoryRecord) -> MemoryRecord: ...
    def find(self, case_id: str, event_type: str, source_ref: str) -> MemoryRecord | None: ...
    def existing_keys(self) -> set[tuple[str, str, str]]: ...
    def retrieve(
        self, customer_id: str, *, purpose: str, actor: str, as_of: date | None = None
    ) -> list[MemoryRecord]: ...
    def retention_status(
        self, customer_id: str, *, actor: str, as_of: date | None = None
    ) -> dict: ...


class LocalMemoryDouble:
    """LOCAL DOUBLE for the memory port — stores records in the
    blueprint's own store and talks to NO real memory service.

    Stands in for AgentCore Memory so the record shapes, the
    customer scoping, the retention window and the access log can
    be built and graded offline. Production retrieval rides the
    configured AgentCore Memory bound to this port; every record
    the double writes carries its label in `provider`.

    Persistence is delegated to the store callables it is built
    with (the same pattern as the R6 tool gateway's record sink):
    the double owns the port semantics — scoping, expiry, logging —
    and the store owns the bytes.
    """

    provider_name = DOUBLE_MEMORY

    def __init__(
        self,
        *,
        pack: MemoryPack,
        put_record,
        get_record,
        list_records,
        record_index,
        put_access,
        count_access,
    ):
        self._pack = pack
        self._put_record = put_record
        self._get_record = get_record
        self._list_records = list_records
        self._record_index = record_index
        self._put_access = put_access
        self._count_access = count_access

    # -- writes (append-only; never logged as retrievals) -------------

    def write(self, record: MemoryRecord) -> MemoryRecord:
        """Append a record. Idempotent by content address: writing
        the same milestone twice returns the standing record —
        records are never edited or duplicated."""
        existing = self._get_record(record.record_id)
        if existing is not None:
            return MemoryRecord.model_validate(existing)
        self._put_record(record.model_dump(mode="json"))
        return record

    def find(self, case_id: str, event_type: str, source_ref: str) -> MemoryRecord | None:
        """Write-side existence check for one milestone, by key.
        Returns the standing record or None; reads no other
        customer's facts (the index lookup is by case id)."""
        for row in self._record_index():
            if (
                row["case_id"] == case_id
                and row["event_type"] == event_type
                and row["source_ref"] == source_ref
            ):
                payload = self._get_record(row["record_id"])
                return MemoryRecord.model_validate(payload) if payload else None
        return None

    def existing_keys(self) -> set[tuple[str, str, str]]:
        """The (case, event, source) keys already on record — the
        write-side index sync reconciles against. Keys only."""
        return {
            (row["case_id"], row["event_type"], row["source_ref"])
            for row in self._record_index()
        }

    # -- retrievals (every one logged before it returns) ---------------

    def _rows_for(self, customer_id: str) -> list[MemoryRecord]:
        """The store read underneath retrieval — predicated on the
        one customer id the caller named. There is no code path
        that reads another customer's rows into a retrieval."""
        return [
            MemoryRecord.model_validate(p) for p in self._list_records(customer_id)
        ]

    def _resolve_as_of(self, rows: list[MemoryRecord], as_of: date | None) -> date:
        """An omitted as-of reads 'as of the customer's latest
        recorded event' — deterministic, and stated on the log
        entry it produces."""
        if as_of is not None:
            return as_of
        if rows:
            return max(r.occurred_on for r in rows)
        return date.min

    def _log(
        self,
        customer_id: str,
        purpose: str,
        actor: str,
        as_of: date,
        returned: list[MemoryRecord],
        expired_excluded: int,
    ) -> None:
        seq = self._count_access()
        blob = json.dumps(
            {
                "customer_id": customer_id,
                "purpose": purpose,
                "actor": actor,
                "as_of": str(as_of),
                "seq": seq,
                "returned": [r.record_id for r in returned],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        entry = MemoryAccessEntry(
            entry_id="MA-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            seq=seq,
            customer_id=customer_id,
            purpose=purpose,
            actor=actor,
            as_of=as_of,
            returned_record_ids=[r.record_id for r in returned],
            expired_excluded=expired_excluded,
        )
        self._put_access(entry.model_dump(mode="json"))

    def retrieve(
        self, customer_id: str, *, purpose: str, actor: str, as_of: date | None = None
    ) -> list[MemoryRecord]:
        """The customer's live records as of a date: occurred, not
        yet expired. Expired records are excluded (and counted on
        the log entry); records of any other customer cannot appear
        here, because the read underneath is predicated on this id.
        The retrieval — including an empty one, including a probe
        for a stranger — is logged before it returns."""
        rows = self._rows_for(customer_id)
        read_as_of = self._resolve_as_of(rows, as_of)
        live = [r for r in rows if r.occurred_on <= read_as_of <= r.expires_on]
        expired = [
            r for r in rows if r.occurred_on <= read_as_of and read_as_of > r.expires_on
        ]
        self._log(customer_id, purpose, actor, read_as_of, live, len(expired))
        return sorted(live, key=lambda r: (r.occurred_on, r.record_id))

    def retention_status(
        self, customer_id: str, *, actor: str, as_of: date | None = None
    ) -> dict:
        """The tombstone read: how many of the customer's records
        are live and how many retention has expired, as of a date.
        Expired records are COUNTED here — listed as expired,
        never silently deleted (the record store has no delete).
        Logged like every other read."""
        rows = self._rows_for(customer_id)
        read_as_of = self._resolve_as_of(rows, as_of)
        live = [r for r in rows if r.occurred_on <= read_as_of <= r.expires_on]
        expired = [
            r for r in rows if r.occurred_on <= read_as_of and read_as_of > r.expires_on
        ]
        self._log(customer_id, "retention_status", actor, read_as_of, [], len(expired))
        return {
            "customer_id": customer_id,
            "provider": self.provider_name,
            "memory_pack_version": self._pack.version,
            "retention_days": self._pack.retention_days,
            "as_of": str(read_as_of),
            "active_count": len(live),
            "expired_count": len(expired),
            "active_record_ids": [r.record_id for r in live],
            "expired_record_ids": [r.record_id for r in expired],
        }
