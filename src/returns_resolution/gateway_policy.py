"""The gateway policy pack — AgentCore Policy, as data (R11).

Law 6 says carrier and label actions are tools, not prose. R11
completes the other half of that law: the tools sit behind a
Gateway, and a Policy pack decides — BEFORE a tool runs — whether
this actor may invoke this tool with these arguments. The pack is
modelled on AgentCore Policy (Cedar-shaped): named policies, each
stating the tools it governs and the condition under which it
DENIES. Evaluation is first-match in pack order: the first policy
whose condition denies is the denial of record; if no policy
denies, the invocation is permitted. The pack is versioned data,
like every other pack in this blueprint — a policy change is a new
pack version, never an edit, and every denial names the policy id
and the pack version it was taken under.

What the pack governs in v1, and what it deliberately does not:

- **Actors, on every tool.** A customer never invokes a gateway
  tool (customer actions arrive as requests the service gates);
  carrier writes are staff work; tracking reads are open to staff,
  warehouse and auditor; the grading callback belongs to the
  warehouse (and staff); money moves only for staff actors.
- **The money tool's state and arguments.** Payment execution is
  denied at the gateway when the case's fraud path is blocked,
  when the computation the call names is not the approved one,
  when that computation has already been executed under another
  key, when the amount argument exceeds the computed figure, and
  when the amount exceeds the invoking role's cap in the pack —
  the design brief's Cedar example (a $500-capped role cannot
  invoke a $900 refund call) as a pack row, evaluated in code.
- **Not the carrier tools' own gates.** Eligibility and the fraud
  hold also guard label issuance, but those refusals are the
  tools' named domain errors (CarrierError, FraudHoldError),
  raised by the handlers since R6; the pack does not restate them.
  Policy here is the gateway's line — actors, and the money
  tool's state and arguments — and the handlers remain the second
  line, exactly as a real deployment keeps target-side checks
  behind AgentCore Policy.

A denial is NOT an HTTP error. In AgentCore, a Policy denial comes
back over a successful transport as a JSON-RPC error payload
(code -32002); the gateway mirrors that shape in its denial
envelope (see gateway.policy_denial_envelope) and records the
denied invocation — with the denying policy's id — in the same
invocation log as every permitted call. No tool executes on a
denial: the handler is never reached.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from .models import money

GATEWAY_POLICY_PACK_VERSION = "2026.10.1"

# The actor vocabulary at the gateway. Surfaces state who is
# invoking: the operations API and the Runtime act as "staff";
# approvals name "returns_lead" as a role; the warehouse's grading
# callback arrives as "warehouse"; auditors read; "customer" is
# the actor a customer-facing surface would carry — and no policy
# in this pack ever permits it a tool.
ACTOR_STAFF = "staff"
ACTOR_RETURNS_LEAD = "returns_lead"
ACTOR_WAREHOUSE = "warehouse"
ACTOR_AUDITOR = "auditor"
ACTOR_CUSTOMER = "customer"

# Policy condition kinds — the closed vocabulary the evaluator
# implements. A pack row names one kind; the evaluator is code.
KIND_ACTOR_BLOCKED = "actor_blocked"  # deny when the actor is in `actors`
KIND_ACTOR_PERMIT = "actor_permit"  # deny when the actor is NOT in `actors`
KIND_FACT_REQUIRED = "fact_required"  # deny when facts[fact] is not `expected`
KIND_AMOUNT_NOT_ABOVE_FACT = "amount_not_above_fact"  # deny when amount > facts[fact]
KIND_AMOUNT_CAP = "amount_cap"  # deny when amount > caps[actor]


class GatewayPolicy(BaseModel):
    """One policy row: the tools it governs, the condition kind,
    the condition's own data, and the reason stated on a denial."""

    policy_id: str
    tools: list[str]  # tool names, or ["*"] for every tool
    kind: str
    reason: str
    actors: list[str] = []  # actor_blocked / actor_permit
    fact: str | None = None  # fact_required / amount_not_above_fact
    expected: bool | None = None  # fact_required
    caps: dict[str, str] = {}  # amount_cap: actor -> Decimal string


class ToolFacts(BaseModel):
    """The facts a policy evaluation reads, built by the service
    from the artefacts of record for the case and the invocation's
    own arguments. A fact the service cannot establish is None —
    and a required fact that is None denies (fail-closed)."""

    case_exists: bool = False
    eligible: bool | None = None
    fraud_blocked: bool | None = None
    disposition_decided: bool | None = None
    approval_satisfied: bool | None = None
    computation_executed: bool | None = None
    amount: Decimal | None = None  # the invocation's amount argument
    computed_amount: Decimal | None = None  # the computation's total of record


class PolicyDenial(BaseModel):
    """A denial of record: which policy denied, under which pack,
    for which tool and actor, and the reason as stated."""

    policy_id: str
    pack_version: str
    tool: str
    actor: str
    reason: str


class GatewayPolicyPack(BaseModel):
    """The pack: an ordered policy list. Order is semantics —
    evaluation is first-match deny, in the order rows stand here."""

    version: str
    policies: list[GatewayPolicy]


GATEWAY_POLICY_PACK_V1 = GatewayPolicyPack(
    version=GATEWAY_POLICY_PACK_VERSION,
    policies=[
        GatewayPolicy(
            policy_id="GP-CUSTOMER-NO-TOOLS",
            tools=["*"],
            kind=KIND_ACTOR_BLOCKED,
            actors=[ACTOR_CUSTOMER],
            reason=(
                "a customer never invokes a gateway tool; customer actions arrive "
                "as return requests, which the service gates"
            ),
        ),
        GatewayPolicy(
            policy_id="GP-CARRIER-WRITE-ACTORS",
            tools=["create_return_label", "void_return_label", "book_pickup"],
            kind=KIND_ACTOR_PERMIT,
            actors=[ACTOR_STAFF, ACTOR_RETURNS_LEAD],
            reason="carrier writes (label issue/void, pickup booking) are staff tools",
        ),
        GatewayPolicy(
            policy_id="GP-CARRIER-READ-ACTORS",
            tools=["get_carrier_events"],
            kind=KIND_ACTOR_PERMIT,
            actors=[ACTOR_STAFF, ACTOR_RETURNS_LEAD, ACTOR_WAREHOUSE, ACTOR_AUDITOR],
            reason="tracking reads are open to staff, warehouse and auditor actors only",
        ),
        GatewayPolicy(
            policy_id="GP-GRADING-ACTORS",
            tools=["submit_grading_report"],
            kind=KIND_ACTOR_PERMIT,
            actors=[ACTOR_WAREHOUSE, ACTOR_STAFF],
            reason="the grading callback belongs to the warehouse (staff may record it)",
        ),
        GatewayPolicy(
            policy_id="GP-PAYMENT-ACTORS",
            tools=["execute_refund"],
            kind=KIND_ACTOR_PERMIT,
            actors=[ACTOR_STAFF, ACTOR_RETURNS_LEAD],
            reason="refund execution is a staff tool; no other actor may invoke it",
        ),
        GatewayPolicy(
            policy_id="GP-PAYMENT-FRAUD-HOLD",
            tools=["execute_refund"],
            kind=KIND_FACT_REQUIRED,
            fact="fraud_blocked",
            expected=False,
            reason=(
                "the case's fraud path is blocked (an unresolved FRAUD_REVIEW); "
                "no refund executes at the gateway while the hold stands"
            ),
        ),
        GatewayPolicy(
            policy_id="GP-PAYMENT-APPROVAL",
            tools=["execute_refund"],
            kind=KIND_FACT_REQUIRED,
            fact="approval_satisfied",
            expected=True,
            reason=(
                "the computation this call names is not the case's approved "
                "computation (its approval tier is unmet or it is not current)"
            ),
        ),
        GatewayPolicy(
            policy_id="GP-PAYMENT-UNEXECUTED",
            tools=["execute_refund"],
            kind=KIND_FACT_REQUIRED,
            fact="computation_executed",
            expected=False,
            reason=(
                "this computation has already been executed under a different "
                "idempotency key; a second execution is a duplicate refund"
            ),
        ),
        GatewayPolicy(
            policy_id="GP-PAYMENT-AMOUNT",
            tools=["execute_refund"],
            kind=KIND_AMOUNT_NOT_ABOVE_FACT,
            fact="computed_amount",
            reason="the amount argument exceeds the computation's refund total of record",
        ),
        GatewayPolicy(
            policy_id="GP-PAYMENT-ROLE-CAP",
            tools=["execute_refund"],
            kind=KIND_AMOUNT_CAP,
            caps={ACTOR_STAFF: "500.00", ACTOR_RETURNS_LEAD: "500.00"},
            reason="the amount exceeds the invoking role's cap in the gateway policy pack",
        ),
    ],
)


def load_gateway_policy_pack(version: str | None = None) -> GatewayPolicyPack:
    """Fail-closed loader: only the shipped pack exists. An unknown
    version is an error, never a silent fall back — a denial must
    name the pack it was actually taken under."""
    if version is None or version == GATEWAY_POLICY_PACK_V1.version:
        return GATEWAY_POLICY_PACK_V1
    raise ValueError(f"unknown gateway policy pack version: {version!r}")


def _denies(policy: GatewayPolicy, *, actor: str, facts: ToolFacts) -> bool:
    """One policy's condition, evaluated in code. An amount kind
    with no amount of record does not deny here — the approval and
    computed-amount rows above it in the pack are the ones that
    fail closed on a missing computation."""
    if policy.kind == KIND_ACTOR_BLOCKED:
        return actor in policy.actors
    if policy.kind == KIND_ACTOR_PERMIT:
        return actor not in policy.actors
    if policy.kind == KIND_FACT_REQUIRED:
        return getattr(facts, policy.fact or "") is not policy.expected
    if policy.kind == KIND_AMOUNT_NOT_ABOVE_FACT:
        limit = getattr(facts, policy.fact or "")
        return (
            facts.amount is not None
            and limit is not None
            and money(facts.amount) > money(limit)
        )
    if policy.kind == KIND_AMOUNT_CAP:
        if facts.amount is None or actor not in policy.caps:
            return False
        return money(facts.amount) > money(Decimal(policy.caps[actor]))
    raise ValueError(f"unknown gateway policy kind: {policy.kind!r}")


def evaluate_gateway_policy(
    pack: GatewayPolicyPack, *, tool: str, actor: str, facts: ToolFacts
) -> PolicyDenial | None:
    """First-match deny, in pack order. Returns the denial of
    record, or None when no policy denies the invocation."""
    for policy in pack.policies:
        if "*" not in policy.tools and tool not in policy.tools:
            continue
        if _denies(policy, actor=actor, facts=facts):
            return PolicyDenial(
                policy_id=policy.policy_id,
                pack_version=pack.version,
                tool=tool,
                actor=actor,
                reason=policy.reason,
            )
    return None
