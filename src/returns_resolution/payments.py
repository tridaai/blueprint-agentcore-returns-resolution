"""The payments port + approval tiers — Law 5 made mechanical (R5).

Two halves:

1. **Approval tiers** (pure functions over the policy pack): a final
   refund total is auto-approvable at or below the pack's auto cap,
   needs one named approver above it, and a second, different
   approver above the dual cap. The caps are pack data, and the
   tier of a total is computed, never asserted by a caller.

2. **The payments port**, shaped on AgentCore Payments: the service
   hands the port a case, a pinned computation, an amount, the
   tender allocation and a MANDATORY idempotency key, and gets back
   a provider reference. The only implementation in this build is
   `LocalPaymentsDouble` — A LOCAL DOUBLE, a stand-in for tests and
   demos. It moves no real money. In production the port is backed
   by the configured AgentCore Payments provider (x402/Stripe-style
   rails), and that provider — never this double — is what moves
   money. The double is itself idempotent (a repeated key returns
   the original provider reference and records no second movement),
   and it can be told to fail per key so the service's failure
   handling is graded, not assumed.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from .models import TenderAllocation
from .policy import PolicyPack

TIER_AUTO = "auto"
TIER_SINGLE = "single"
TIER_DUAL = "dual"

# How many distinct named approvers each tier requires. "Different"
# is enforced by the service: one person approving twice is one
# approval, and the second act is refused at record time.
APPROVERS_REQUIRED = {TIER_AUTO: 0, TIER_SINGLE: 1, TIER_DUAL: 2}

# The provider name every artefact written through the double
# carries. It says what it is: a local double, not a payment rail.
DOUBLE_PROVIDER = "local-double (AgentCore Payments stand-in)"


def approval_tier(pack: PolicyPack, total: Decimal) -> str:
    """The approval requirement a refund total falls under."""
    if total <= pack.auto_approve_cap:
        return TIER_AUTO
    if total <= pack.dual_approve_cap:
        return TIER_SINGLE
    return TIER_DUAL


class PaymentsProviderError(RuntimeError):
    """The payments provider failed mid-execution. No money moved."""


class ProviderReceipt(BaseModel):
    """What the port returns on a confirmed movement."""

    provider: str
    provider_ref: str
    status: str = "executed"


class PaymentsPort(Protocol):
    """The port contract. Production binds an AgentCore Payments
    provider here; the local double below is the offline stand-in."""

    provider_name: str

    def execute_refund(
        self,
        *,
        case_id: str,
        computation_id: str,
        amount: Decimal,
        tender_allocations: list[TenderAllocation],
        idempotency_key: str,
    ) -> ProviderReceipt: ...


class LocalPaymentsDouble:
    """LOCAL DOUBLE for the payments port — moves NO real money.

    Stands in for the configured AgentCore Payments provider so the
    gates, the idempotency contract and the failure handling can be
    built and graded offline. Production money movement rides the
    configured provider, never this class.

    Behaviour contract:
    - Idempotent by key: executing the same key twice returns the
      original provider reference; `movement_count` — the number of
      actual (simulated) money movements — rises exactly once.
    - Failure injection: `fail_times` maps an idempotency key to how
      many executions of that key fail (raising
      PaymentsProviderError, recording an attempt but NO movement
      and NO receipt) before the key succeeds. That is the
      mid-execution failure the service must survive cleanly.
    """

    provider_name = DOUBLE_PROVIDER

    def __init__(self, fail_times: dict[str, int] | None = None):
        self._fail_times = dict(fail_times or {})
        self._by_key: dict[str, ProviderReceipt] = {}
        self.movement_count = 0
        self.attempt_count = 0

    def execute_refund(
        self,
        *,
        case_id: str,
        computation_id: str,
        amount: Decimal,
        tender_allocations: list[TenderAllocation],
        idempotency_key: str,
    ) -> ProviderReceipt:
        if not idempotency_key:
            raise PaymentsProviderError("an idempotency key is mandatory on every money call")
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            return existing
        self.attempt_count += 1
        if self._fail_times.get(idempotency_key, 0) > 0:
            self._fail_times[idempotency_key] -= 1
            raise PaymentsProviderError(
                f"injected provider failure for key {idempotency_key!r} "
                "(simulated mid-execution failure: no money moved)"
            )
        blob = json.dumps(
            {
                "case_id": case_id,
                "computation_id": computation_id,
                "idempotency_key": idempotency_key,
                "amount": str(amount),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        receipt = ProviderReceipt(
            provider=self.provider_name,
            provider_ref="double-payref-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
        )
        self._by_key[idempotency_key] = receipt
        self.movement_count += 1
        return receipt
