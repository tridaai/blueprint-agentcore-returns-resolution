"""Access control — principals, roles, existence hiding (R16).

Rounds 1–15 built the engines and their gates: who may move money
is decided by the approval tiers (R5), what a tool call may do by
the gateway policy pack (R11), and what customer text may reach by
the quarantine (R15). None of them answered the prior question —
WHO is calling, and what may that caller SEE? Every surface took
actor strings on trust: an approval names its approver, a review
its reviewer, but nothing checked that the caller was entitled to
the case in front of them, and a case id was a capability — anyone
who could guess or learn one could read the case.

R16 is that layer, and it follows the blueprint's one pattern: the
rules are a versioned pack, evaluated in code, and every refusal
is a recorded artefact.

- **The access pack (2026.10.1, below)** states, per role, the
  readable surfaces and the acts the role may perform, and — for
  the refusal messages — the roles each act requires. A permission
  change is a new pack version, never an edit; every denial names
  the pack version it was taken under.
- **A principal is an (id, role) pair.** Surfaces carry it
  explicitly — `X-Principal-Id` / `X-Principal-Role` headers on
  the API, a `principal` object on a Runtime payload. A carried
  principal is always authenticated against the pack: an unknown
  role or a malformed pair is refused with a named error, never
  silently downgraded. There is no default principal at this
  layer: the guarded methods refuse a missing principal outright.
  (The pre-R16 surface calls that carry no principal remain the
  trusted local operations path, exactly as they have been since
  R1 — in a deployment, AgentCore Identity terminates customer
  and staff identity in front of these surfaces and every call
  presents a principal. The README states this scope plainly.)
- **Customers are scoped to their own cases.** A customer
  principal reads only cases whose customer id equals the
  principal id — and another customer's case is HIDDEN, not
  refused: the response is the not-found response, byte for
  byte, so a case id cannot be probed for existence. Customer
  reads are narrowed views: their own decision (eligibility
  cites policy clauses — Law 7), their own refund figures,
  their messages, and the STATE of their own quarantined texts.
  Never fraud internals, never screening findings, never the
  cost ledger, never another customer's anything.
- **Staff are tiered by act, not by screen.** A staff agent
  reads and works cases (grading, labels, messaging, refund
  execution through its gates) but cannot perform the
  supervisor acts: refund approvals, fraud-review decisions,
  quarantine releases of held text. Attempts are refused with
  the required role named. The fraud reviewer role holds the
  review act and the fraud surface. The supervisor holds every
  staff act.
- **The auditor reads everything and does nothing.** Every
  surface — artefacts, cost ledger, gateway log, the denial
  log itself — is readable; every act is refused, and the
  refusal recorded like any other.
- **Every refusal is an artefact.** A denial lands in the
  insert-only denial log (migration 0012) BEFORE the error
  propagates: principal, role, surface, target, reason, pack
  version. The log is itself a surface (auditor + supervisor).
  A denial never leaks what it protects: for a hidden case the
  recorded target is the requested id string — which the
  requester supplied — and no field of the denial says whether
  the case exists.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Mapping

from pydantic import BaseModel

ACCESS_PACK_VERSION = "2026.10.1"

ROLE_CUSTOMER = "customer"
ROLE_STAFF_AGENT = "staff_agent"
ROLE_STAFF_SUPERVISOR = "staff_supervisor"
ROLE_FRAUD_REVIEWER = "fraud_reviewer"
ROLE_AUDITOR = "auditor"
ROLE_SYSTEM = "system"
ROLES = [
    ROLE_CUSTOMER,
    ROLE_STAFF_AGENT,
    ROLE_STAFF_SUPERVISOR,
    ROLE_FRAUD_REVIEWER,
    ROLE_AUDITOR,
    ROLE_SYSTEM,
]

# Readable surfaces — the closed vocabulary the pack grants over.
SURFACE_CASE = "case"
SURFACE_ORDER = "order"
SURFACE_MONEY = "money"
SURFACE_FRAUD = "fraud"
SURFACE_GATEWAY = "gateway"
SURFACE_COST_LEDGER = "cost_ledger"
SURFACE_QUARANTINE = "quarantine"
SURFACE_MEMORY = "memory"
SURFACE_CONSOLE = "console"
SURFACE_DENIAL_LOG = "denial_log"
SURFACE_PACKS = "packs"
SURFACES = [
    SURFACE_CASE,
    SURFACE_ORDER,
    SURFACE_MONEY,
    SURFACE_FRAUD,
    SURFACE_GATEWAY,
    SURFACE_COST_LEDGER,
    SURFACE_QUARANTINE,
    SURFACE_MEMORY,
    SURFACE_CONSOLE,
    SURFACE_DENIAL_LOG,
    SURFACE_PACKS,
]

# Acts — the named operations the pack gates. These are the
# surface-level acts; the engines keep their own gates behind
# them (approval tiers, the fraud guard, the gateway pack).
ACT_INTAKE_RETURN = "intake_return"
ACT_MANAGE_CASE = "manage_case"
ACT_EXECUTE_REFUND = "execute_refund"
ACT_APPROVE_REFUND = "approve_refund"
ACT_CONFIRM_FRAUD_REVIEW = "confirm_fraud_review"
ACT_RELEASE_QUARANTINE = "release_quarantine"
ACTS = [
    ACT_INTAKE_RETURN,
    ACT_MANAGE_CASE,
    ACT_EXECUTE_REFUND,
    ACT_APPROVE_REFUND,
    ACT_CONFIRM_FRAUD_REVIEW,
    ACT_RELEASE_QUARANTINE,
]

# Denial reasons — the closed vocabulary of the denial log.
REASON_PRINCIPAL_REQUIRED = "principal_required"
REASON_MALFORMED_PRINCIPAL = "malformed_principal"
REASON_UNKNOWN_ROLE = "unknown_role"
REASON_SURFACE_NOT_READABLE = "surface_not_readable"
REASON_ACT_NOT_PERMITTED = "act_not_permitted"
REASON_READ_ONLY_ROLE = "read_only_role"
REASON_CASE_NOT_VISIBLE = "case_not_visible"
REASON_CUSTOMER_SCOPE = "customer_scope"


class AccessDeniedError(RuntimeError):
    """An access refusal — the message names the gap (the missing
    principal, the unknown role, the role the act requires). The
    denial artefact is recorded before this is raised, so catching
    it never erases the record."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class Principal(BaseModel):
    """A caller, as the surfaces carry it: a stable id and one
    pack role. The id is the person's (or customer's, or system's)
    own identifier — for a customer it IS the customer id the
    scoping rule compares against."""

    id: str
    role: str


class RoleGrant(BaseModel):
    """One role's standing in the pack: the surfaces it may read
    and the acts it may perform."""

    role: str
    read_surfaces: list[str]
    acts: list[str]


class AccessPack(BaseModel):
    """The access pack as data: role grants, plus — for refusal
    messages that must name what an act requires — the roles each
    act takes, stated once here rather than derived at call sites."""

    version: str
    roles: list[RoleGrant]
    act_requirements: dict[str, list[str]]

    def grant(self, role: str) -> RoleGrant | None:
        for grant in self.roles:
            if grant.role == role:
                return grant
        return None

    def can_read(self, role: str, surface: str) -> bool:
        grant = self.grant(role)
        return grant is not None and surface in grant.read_surfaces

    def can_act(self, role: str, act: str) -> bool:
        grant = self.grant(role)
        return grant is not None and act in grant.acts

    def required_roles(self, act: str) -> list[str]:
        return list(self.act_requirements.get(act, []))


_STAFF_READS = [
    SURFACE_CASE,
    SURFACE_ORDER,
    SURFACE_MONEY,
    SURFACE_GATEWAY,
    SURFACE_QUARANTINE,
    SURFACE_MEMORY,
    SURFACE_CONSOLE,
    SURFACE_PACKS,
]

ACCESS_PACK_V1 = AccessPack(
    version=ACCESS_PACK_VERSION,
    roles=[
        RoleGrant(
            role=ROLE_CUSTOMER,
            read_surfaces=[
                SURFACE_CASE,
                SURFACE_ORDER,
                SURFACE_MONEY,
                SURFACE_QUARANTINE,
                SURFACE_MEMORY,
                SURFACE_PACKS,
            ],
            acts=[ACT_INTAKE_RETURN],
        ),
        RoleGrant(
            role=ROLE_STAFF_AGENT,
            read_surfaces=list(_STAFF_READS),
            acts=[ACT_INTAKE_RETURN, ACT_MANAGE_CASE, ACT_EXECUTE_REFUND],
        ),
        RoleGrant(
            role=ROLE_STAFF_SUPERVISOR,
            read_surfaces=_STAFF_READS
            + [SURFACE_FRAUD, SURFACE_COST_LEDGER, SURFACE_DENIAL_LOG],
            acts=list(ACTS),
        ),
        RoleGrant(
            role=ROLE_FRAUD_REVIEWER,
            read_surfaces=[
                SURFACE_CASE,
                SURFACE_ORDER,
                SURFACE_FRAUD,
                SURFACE_QUARANTINE,
                SURFACE_CONSOLE,
                SURFACE_PACKS,
            ],
            acts=[ACT_CONFIRM_FRAUD_REVIEW],
        ),
        RoleGrant(
            role=ROLE_AUDITOR,
            read_surfaces=list(SURFACES),
            acts=[],
        ),
        RoleGrant(
            role=ROLE_SYSTEM,
            read_surfaces=list(SURFACES),
            acts=list(ACTS),
        ),
    ],
    act_requirements={
        ACT_INTAKE_RETURN: [
            ROLE_CUSTOMER,
            ROLE_STAFF_AGENT,
            ROLE_STAFF_SUPERVISOR,
            ROLE_SYSTEM,
        ],
        ACT_MANAGE_CASE: [ROLE_STAFF_AGENT, ROLE_STAFF_SUPERVISOR, ROLE_SYSTEM],
        ACT_EXECUTE_REFUND: [ROLE_STAFF_AGENT, ROLE_STAFF_SUPERVISOR, ROLE_SYSTEM],
        ACT_APPROVE_REFUND: [ROLE_STAFF_SUPERVISOR, ROLE_SYSTEM],
        ACT_CONFIRM_FRAUD_REVIEW: [
            ROLE_FRAUD_REVIEWER,
            ROLE_STAFF_SUPERVISOR,
            ROLE_SYSTEM,
        ],
        ACT_RELEASE_QUARANTINE: [ROLE_STAFF_SUPERVISOR, ROLE_SYSTEM],
    },
)


def load_access_pack(version: str | None = None) -> AccessPack:
    """The loader, fail-closed like every pack loader in this
    blueprint: an unknown version is an error, never a silent
    fall back to v1."""
    if version is None or version == ACCESS_PACK_V1.version:
        return ACCESS_PACK_V1
    raise ValueError(f"unknown access pack version: {version!r}")


class AccessDenial(BaseModel):
    """One recorded refusal. Insert-only: a denial is history,
    never state to rewrite. `target` is the identifier the
    requester supplied (a case id, a customer id, an act name) —
    for a hidden case it is the requested string and nothing in
    the record says whether the case exists."""

    denial_id: str
    seq: int  # the log's append order (the count at write time)
    principal_id: str
    role: str
    surface: str
    target: str
    reason: str
    detail: str
    pack_version: str
    denied_on: date


class AccessControl:
    """The guard, bound over the store's denial log. The service
    holds one; the surfaces reach it through the service's guarded
    methods. Every check that refuses records its denial BEFORE
    raising, through the one `deny` path — a refusal that is not
    on the log does not exist as far as this layer is concerned.
    """

    def __init__(self, store, pack: AccessPack | None = None):
        self._store = store
        self.pack = pack or load_access_pack()

    # -- authentication ------------------------------------------------
    def authenticate(
        self,
        raw: Mapping[str, Any] | None,
        *,
        surface: str,
        target: str = "",
        on: date | None = None,
    ) -> Principal:
        """Validate a carried principal against the pack. A missing
        principal, a malformed pair and an unknown role are each
        refused with their named reason — there is no default
        principal and no silent downgrade."""
        if raw is None:
            self.deny(
                principal_id="",
                role="",
                surface=surface,
                target=target,
                reason=REASON_PRINCIPAL_REQUIRED,
                detail=(
                    "no principal on the call — a principal (id + role) "
                    "is required; there is no default principal"
                ),
                on=on,
            )
        principal_id = raw.get("id") if isinstance(raw, Mapping) else None
        role = raw.get("role") if isinstance(raw, Mapping) else None
        if (
            not isinstance(principal_id, str)
            or not principal_id.strip()
            or not isinstance(role, str)
            or not role.strip()
        ):
            self.deny(
                principal_id=str(principal_id or ""),
                role=str(role or ""),
                surface=surface,
                target=target,
                reason=REASON_MALFORMED_PRINCIPAL,
                detail="a principal is an (id, role) pair with both parts named",
                on=on,
            )
        principal_id = principal_id.strip()
        role = role.strip()
        if self.pack.grant(role) is None:
            self.deny(
                principal_id=principal_id,
                role=role,
                surface=surface,
                target=target,
                reason=REASON_UNKNOWN_ROLE,
                detail=(
                    f"unknown role {role!r} — access pack {self.pack.version} "
                    f"knows: {', '.join(ROLES)}"
                ),
                on=on,
            )
        return Principal(id=principal_id, role=role)

    # -- authorisation ---------------------------------------------------
    def require_read(
        self,
        principal: Principal,
        surface: str,
        *,
        target: str = "",
        on: date | None = None,
    ) -> None:
        if not self.pack.can_read(principal.role, surface):
            self.deny(
                principal_id=principal.id,
                role=principal.role,
                surface=surface,
                target=target,
                reason=REASON_SURFACE_NOT_READABLE,
                detail=(
                    f"surface {surface} is not readable by role {principal.role}"
                ),
                on=on,
            )

    def require_act(
        self,
        principal: Principal,
        act: str,
        *,
        surface: str,
        target: str = "",
        on: date | None = None,
    ) -> None:
        if self.pack.can_act(principal.role, act):
            return
        if principal.role == ROLE_AUDITOR:
            self.deny(
                principal_id=principal.id,
                role=principal.role,
                surface=surface,
                target=target,
                reason=REASON_READ_ONLY_ROLE,
                detail=(
                    f"role auditor is read-only: it performs no acts "
                    f"(attempted act: {act})"
                ),
                on=on,
            )
        required = self.pack.required_roles(act)
        self.deny(
            principal_id=principal.id,
            role=principal.role,
            surface=surface,
            target=target,
            reason=REASON_ACT_NOT_PERMITTED,
            detail=(
                f"act {act} requires role "
                f"{' or '.join(required) if required else '(none in the pack)'}; "
                f"principal {principal.id} holds {principal.role}"
            ),
            on=on,
        )

    # -- the one recording path -------------------------------------------
    def deny(
        self,
        *,
        principal_id: str,
        role: str,
        surface: str,
        target: str,
        reason: str,
        detail: str,
        on: date | None = None,
    ) -> None:
        """Record the denial, then raise it. Always raises."""
        denied_on = on or date.today()
        seq = self._store.count_access_denials()
        blob = json.dumps(
            {
                "seq": seq,
                "principal_id": principal_id,
                "role": role,
                "surface": surface,
                "target": target,
                "reason": reason,
                "denied_on": denied_on.isoformat(),
                "pack_version": self.pack.version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        denial = AccessDenial(
            denial_id="AD-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            seq=seq,
            principal_id=principal_id,
            role=role,
            surface=surface,
            target=target,
            reason=reason,
            detail=detail,
            pack_version=self.pack.version,
            denied_on=denied_on,
        )
        self._store.put_access_denial(denial.model_dump(mode="json"))
        raise AccessDeniedError(reason, f"access denied: {detail}")

    def denials(self) -> list[AccessDenial]:
        return [
            AccessDenial.model_validate(p)
            for p in self._store.list_access_denials()
        ]
