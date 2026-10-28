"""Model gateway + cost ledger (R12).

R1 built a model PORT: one place a model is called, with an
offline deterministic profile and a hosted profile that refused
loudly. R12 completes that port into a GATEWAY — the single path
every model-shaped call in the repo takes:

- **Summary composition** (the R1–R7 surface, consumed by ingest,
  label issuance, grading and the R8 `return_summary` template)
  goes through the gateway's `compose_*` methods.
- **The R8 model note** — free-form model text a caller hands to
  `draft_message` — is submitted to the gateway before it can be
  drafted: validated as a structured output, checked against the
  case's budget, and recorded in the ledger. A note never enters
  a message unrecorded.

Three profiles, one contract:

- `offline` (default): the deterministic composer behind the port
  since R1. Its output is byte-identical to R1–R11 — the gateway
  meters and records the call, it does not touch the text. The
  profile is local and unmetered by any provider, so the gateway
  ESTIMATES its usage from the serialised inputs and the composed
  output at the price pack's stated rate, and the pack prices the
  profile at zero: the recorded cost of an offline call is $0.00.
- `bedrock`: a Bedrock-shaped profile — Converse-style request and
  response envelopes, the model id as configuration. STUB-TESTED
  ONLY: no live AWS call is possible from this build environment
  and none is attempted; the transport is an injectable callable
  and the default gateway has none bound, so every call fails
  closed (below). A deployment binds the real Converse transport
  behind the same envelope contract.
- `nim`: an NVIDIA NIM-shaped profile — OpenAI-style chat
  completions envelopes, the model id as configuration. Likewise
  stub-tested only, likewise fail-closed unbound.

Fail-closed, never fall back. A profile that is configured but
unreachable — no transport bound, a transport error, no model id —
raises `ModelProfileUnavailableError` and the drafting surface
reports the failure. The gateway never substitutes another
profile's output, and in particular never degrades a hosted
profile to the offline composer: a silent fallback would put
words in front of a customer that nobody chose.

Structured outputs only. Whatever a profile produces enters the
system as a `DraftOutput` — a Pydantic artefact. Hosted profiles
must return the envelope whose content is a JSON object carrying
the text; a malformed envelope, non-JSON content, or a payload
that does not validate is REJECTED: recorded in the ledger as
`rejected_invalid` and raised as `InvalidModelOutputError`,
never coerced, never partially used. And structure is not truth:
a well-formed hosted response stating a wrong figure still meets
the figure verifier downstream (service/messaging), exactly as
the offline composer's text does.

The cost ledger. Every gateway call — composed, noted, rejected,
refused, failed — is recorded as a `CostEntry` artefact: profile,
model id, purpose, the usage (from the profile's envelope, or the
gateway's stated estimate), the unit prices it was charged at,
and the cost COMPUTED IN CODE from the versioned price pack. No
figure in the ledger is taken from a model's word: a hosted
envelope's usage counts are its metering report (as with any
provider bill), but the prices and the multiplication are the
pack's and the gateway's. `verify_cost_entry` re-performs an
entry's arithmetic from its recorded tokens and prices, so a
tampered ledger entry is detectable after the fact.

The runaway guard: the price pack states a per-case call budget.
A drafting call that would exceed the case's budget is refused
with `ModelBudgetError` — the budget and the pack version named
in the refusal — and recorded as `refused_budget`. Refusals do
not themselves consume budget.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Protocol

from pydantic import BaseModel, field_validator

from .modelport import OfflineComposer, _facts
from .models import EligibilityDecision, RefundComputation, ReturnLabel, money

PRICE_PACK_VERSION = "2026.10.1"

PROFILE_OFFLINE = "offline"
PROFILE_BEDROCK = "bedrock"
PROFILE_NIM = "nim"

PURPOSE_SUMMARY = "compose_summary"
PURPOSE_REFUND_SUMMARY = "compose_refund_summary"
PURPOSE_LABEL_SUMMARY = "compose_label_summary"
PURPOSE_NOTE = "message_note"

STATUS_OK = "ok"
STATUS_REJECTED_INVALID = "rejected_invalid"
STATUS_REFUSED_BUDGET = "refused_budget"
STATUS_FAILED_CLOSED = "failed_closed"

USAGE_ENVELOPE = "envelope"
USAGE_ESTIMATE = "gateway-estimate"

# Statuses that consumed a real model call and so count against
# the per-case budget. A budget refusal is recorded too, but it
# reached no model, so it does not consume budget itself.
_BILLABLE_STATUSES = (STATUS_OK, STATUS_REJECTED_INVALID, STATUS_FAILED_CLOSED)


class ModelGatewayError(RuntimeError):
    """A gateway call failed. The drafting surface reports it;
    nothing was composed, and no other profile was substituted."""


class ModelBudgetError(ModelGatewayError):
    """The case's per-case call budget is exhausted. The refusal
    names the budget and the price pack that states it."""


class InvalidModelOutputError(ModelGatewayError):
    """A profile's output did not validate as a structured
    DraftOutput. Rejected and recorded — never coerced."""


class ModelProfileUnavailableError(ModelGatewayError):
    """The configured profile cannot be reached (no transport
    bound, no model id, a transport error). Fail-closed."""


# ---------------------------------------------------------------------------
# The price pack — versioned data.
# ---------------------------------------------------------------------------


PRICE_QUANTUM = Decimal("0.000001")


def unit_price(value: Decimal | str | int) -> Decimal:
    """Quantise a per-1k-token unit price. Unit prices are
    sub-cent rate-card data (thousandths of a cent and up), so
    they quantise to the micro-unit — NEVER to cents: the cost
    they produce is money, the prices themselves are not."""
    return Decimal(value).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)


class ProfilePrice(BaseModel):
    """One profile's rate card: prices per 1,000 tokens, as data.

    These are CONFIGURED rates — the deployment's own rate card
    for the model it configured — not a claim about any vendor's
    list price. A price change is a new pack version, never an
    edit, and every ledger entry pins the prices it was charged
    at, so history never reprices.
    """

    profile: str
    input_per_1k: Decimal
    output_per_1k: Decimal

    @field_validator("input_per_1k", "output_per_1k", mode="before")
    @classmethod
    def _price(cls, v: object) -> Decimal:
        return unit_price(Decimal(str(v)))


class PricePack(BaseModel):
    """The pack: rate cards, the usage estimator's rate, and the
    per-case call budget (the runaway guard)."""

    version: str
    chars_per_token: int
    max_calls_per_case: int
    profiles: list[ProfilePrice]

    def price_for(self, profile: str) -> ProfilePrice:
        for row in self.profiles:
            if row.profile == profile:
                return row
        raise ValueError(f"no price row for profile {profile!r} in pack {self.version}")


PRICE_PACK_V1 = PricePack(
    version=PRICE_PACK_VERSION,
    chars_per_token=4,
    max_calls_per_case=40,
    profiles=[
        # The offline composer is local and deterministic: no
        # provider meters it, so its rate card is zero — the
        # ledger records its calls at $0.00, with estimated usage.
        ProfilePrice(profile=PROFILE_OFFLINE, input_per_1k="0", output_per_1k="0"),
        ProfilePrice(profile=PROFILE_BEDROCK, input_per_1k="0.003", output_per_1k="0.015"),
        ProfilePrice(profile=PROFILE_NIM, input_per_1k="0.001", output_per_1k="0.002"),
    ],
)


def load_price_pack(version: str | None = None) -> PricePack:
    """Fail-closed loader: only the shipped pack exists. An
    unknown version is an error, never a silent fall back — a
    ledger entry must name the prices it was actually charged at."""
    if version is None or version == PRICE_PACK_V1.version:
        return PRICE_PACK_V1
    raise ValueError(f"unknown price pack version: {version!r}")


def compute_cost(price: ProfilePrice, tokens_in: int, tokens_out: int) -> Decimal:
    """The ledger's one multiplication, in code, Decimal-exact:
    tokens × the pack's per-1k prices, quantised to cents."""
    raw = (Decimal(tokens_in) * price.input_per_1k + Decimal(tokens_out) * price.output_per_1k) / Decimal(1000)
    return money(raw)


def estimate_tokens(text: str, chars_per_token: int) -> int:
    """The gateway's stated estimator for usage no provider
    envelope reports: ceil(chars / chars_per_token). Named an
    estimate wherever it is used (`usage_source` on the entry)."""
    if not text:
        return 0
    return math.ceil(len(text) / chars_per_token)


# ---------------------------------------------------------------------------
# Structured outputs + the ledger artefact.
# ---------------------------------------------------------------------------


class DraftOutput(BaseModel):
    """The ONLY shape model text enters the system in. Pydantic
    does not coerce a non-string `text`, so a profile returning a
    number, an object or nothing at all fails validation here."""

    text: str


class CostEntry(BaseModel):
    """One gateway call, of record. `cost` is computed by the
    gateway from `tokens_*` and the `*_per_1k` prices pinned here
    (copied from the price pack at call time, so a later pack
    version never reprices this entry); `verify_cost_entry`
    re-performs it. Non-ok entries carry cost $0.00 — a rejected,
    refused or failed call is recorded, never charged."""

    entry_id: str
    seq: int
    case_id: str
    purpose: str
    profile: str
    model_id: str
    status: str
    usage_source: str
    tokens_in: int
    tokens_out: int
    input_per_1k: Decimal
    output_per_1k: Decimal
    cost: Decimal
    price_pack_version: str
    detail: str = ""

    @field_validator("input_per_1k", "output_per_1k", mode="before")
    @classmethod
    def _price(cls, v: object) -> Decimal:
        return unit_price(Decimal(str(v)))

    @field_validator("cost", mode="before")
    @classmethod
    def _money(cls, v: object) -> Decimal:
        return money(Decimal(str(v)))


def verify_cost_entry(entry: CostEntry, pack: PricePack | None = None) -> list[str]:
    """Re-perform one entry's arithmetic from its own record:
    the pinned prices must be the pack's row for its profile (at
    the entry's pack version), and the cost must be exactly the
    computed figure for ok entries, exactly zero otherwise."""
    problems: list[str] = []
    pack = pack or load_price_pack()
    if entry.price_pack_version != pack.version:
        problems.append(
            f"entry priced under pack {entry.price_pack_version}, current pack is {pack.version}"
        )
        return problems
    try:
        row = pack.price_for(entry.profile)
    except ValueError:
        problems.append(f"no price row for profile {entry.profile!r}")
        return problems
    if entry.input_per_1k != row.input_per_1k or entry.output_per_1k != row.output_per_1k:
        problems.append("pinned unit prices differ from the price pack's row for the profile")
    if entry.status == STATUS_OK:
        expected = compute_cost(row, entry.tokens_in, entry.tokens_out)
        if entry.cost != expected:
            problems.append(f"cost {entry.cost} != recomputed {expected}")
    elif entry.cost != Decimal("0.00"):
        problems.append(f"a {entry.status} entry must carry cost 0.00, carries {entry.cost}")
    return problems


# ---------------------------------------------------------------------------
# The gateway.
# ---------------------------------------------------------------------------


class ModelGateway(Protocol):
    """The port contract every surface consumes. The service holds
    a gateway as its model port; profile implementations differ,
    the metering, validation, budget and ledger do not."""

    profile_name: str
    model_id: str

    def compose_summary(self, decision: EligibilityDecision, product_name: str) -> str: ...
    def compose_refund_summary(
        self, decision: EligibilityDecision, product_name: str, computation: RefundComputation
    ) -> str: ...
    def compose_label_summary(
        self,
        decision: EligibilityDecision,
        product_name: str,
        computation: RefundComputation,
        label: ReturnLabel,
    ) -> str: ...
    def submit_note(self, *, case_id: str, text: str) -> str: ...


# A profile's raw production: the composed text plus the usage the
# profile reports (envelope) or the gateway estimates (offline).
Production = tuple[str, int, int, str]


class BaseGateway:
    """Metering, validation, budget and ledger — shared by every
    profile. A profile implements `_produce(purpose, case_id, args)`
    and nothing else; everything around the call lives here."""

    profile_name: str = ""
    model_id: str = ""

    def __init__(self, pack: PricePack | None = None):
        self._pack = pack or load_price_pack()
        self._record: Callable[[dict], None] | None = None
        self._list: Callable[[str], list[dict]] | None = None
        self._local: list[CostEntry] = []

    # -- store binding ------------------------------------------------
    def bind_store(
        self,
        *,
        record_entry: Callable[[dict], None],
        list_entries: Callable[[str], list[dict]],
    ) -> None:
        """Bind the ledger's persistence (the service does this at
        construction, the way the memory double is bound). Unbound,
        the gateway keeps its entries in memory — the unit-test and
        embedding path; the ledger's semantics are identical."""
        self._record = record_entry
        self._list = list_entries

    @property
    def price_pack(self) -> PricePack:
        return self._pack

    def config_view(self) -> dict:
        """The profile configuration read: profile, model id, the
        pack and its budget, and this profile's rate card."""
        row = self._pack.price_for(self.profile_name)
        return {
            "profile": self.profile_name,
            "model_id": self.model_id,
            "price_pack_version": self._pack.version,
            "max_calls_per_case": self._pack.max_calls_per_case,
            "chars_per_token": self._pack.chars_per_token,
            "input_per_1k": str(row.input_per_1k),
            "output_per_1k": str(row.output_per_1k),
        }

    # -- ledger plumbing ----------------------------------------------
    def _entries_for(self, case_id: str) -> list[CostEntry]:
        if self._list is not None:
            return [CostEntry.model_validate(p) for p in self._list(case_id)]
        return [e for e in self._local if e.case_id == case_id]

    def _append(self, entry: CostEntry) -> CostEntry:
        if self._record is not None:
            self._record(entry.model_dump(mode="json"))
        else:
            self._local.append(entry)
        return entry

    def _record_call(
        self,
        *,
        case_id: str,
        purpose: str,
        status: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        usage_source: str = USAGE_ESTIMATE,
        detail: str = "",
    ) -> CostEntry:
        existing = self._entries_for(case_id)
        seq = len(existing) + 1
        row = self._pack.price_for(self.profile_name)
        cost = (
            compute_cost(row, tokens_in, tokens_out)
            if status == STATUS_OK
            else Decimal("0.00")
        )
        blob = json.dumps(
            {
                "case_id": case_id,
                "seq": seq,
                "purpose": purpose,
                "profile": self.profile_name,
                "model_id": self.model_id,
                "status": status,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        entry = CostEntry(
            entry_id="MC-" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            seq=seq,
            case_id=case_id,
            purpose=purpose,
            profile=self.profile_name,
            model_id=self.model_id,
            status=status,
            usage_source=usage_source,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            input_per_1k=row.input_per_1k,
            output_per_1k=row.output_per_1k,
            cost=cost,
            price_pack_version=self._pack.version,
            detail=detail,
        )
        return self._append(entry)

    def _check_budget(self, case_id: str, purpose: str) -> None:
        billable = sum(
            1 for e in self._entries_for(case_id) if e.status in _BILLABLE_STATUSES
        )
        if billable >= self._pack.max_calls_per_case:
            self._record_call(
                case_id=case_id,
                purpose=purpose,
                status=STATUS_REFUSED_BUDGET,
                detail=(
                    f"per-case budget of {self._pack.max_calls_per_case} model calls "
                    f"(price pack {self._pack.version}) already consumed by this case"
                ),
            )
            raise ModelBudgetError(
                f"model call refused for case {case_id}: the per-case budget of "
                f"{self._pack.max_calls_per_case} calls (price pack "
                f"{self._pack.version}) is exhausted"
            )

    def _invoke(self, purpose: str, case_id: str, produce: Callable[[], Production]) -> str:
        """One metered gateway call: budget first, then the
        profile's production, then structured validation, then
        the ledger. Every outcome — ok, invalid, unavailable —
        is recorded exactly once."""
        self._check_budget(case_id, purpose)
        try:
            text, tokens_in, tokens_out, usage_source = produce()
        except InvalidModelOutputError as exc:
            self._record_call(
                case_id=case_id, purpose=purpose,
                status=STATUS_REJECTED_INVALID, detail=str(exc),
            )
            raise
        except ModelGatewayError as exc:
            self._record_call(
                case_id=case_id, purpose=purpose,
                status=STATUS_FAILED_CLOSED, detail=str(exc),
            )
            raise
        except Exception as exc:  # a backend/transport failure: fail closed
            self._record_call(
                case_id=case_id, purpose=purpose,
                status=STATUS_FAILED_CLOSED, detail=f"{type(exc).__name__}: {exc}",
            )
            raise ModelProfileUnavailableError(
                f"{self.profile_name} profile failed for case {case_id}: {exc}"
            ) from exc
        try:
            draft = DraftOutput.model_validate({"text": text})
        except Exception as exc:
            self._record_call(
                case_id=case_id, purpose=purpose,
                status=STATUS_REJECTED_INVALID,
                detail=f"profile output did not validate as a structured draft: {exc}",
            )
            raise InvalidModelOutputError(
                f"{self.profile_name} profile returned output that is not a "
                f"structured draft for case {case_id}"
            ) from exc
        self._record_call(
            case_id=case_id, purpose=purpose, status=STATUS_OK,
            tokens_in=tokens_in, tokens_out=tokens_out, usage_source=usage_source,
        )
        return draft.text

    # -- the drafting surface ------------------------------------------
    def compose_summary(self, decision: EligibilityDecision, product_name: str) -> str:
        return self._invoke(
            PURPOSE_SUMMARY, decision.case_id,
            lambda: self._produce(PURPOSE_SUMMARY, decision.case_id, (decision, product_name)),
        )

    def submit_note(self, *, case_id: str, text: str) -> str:
        """Adopt caller-supplied model text (the R8 note slot) at
        the gateway boundary: budget-checked, validated as a
        structured draft, recorded — and returned VERBATIM. The
        note arrived without a provider envelope, so its usage is
        the gateway's estimate over the text, stated as such."""
        def _produce() -> Production:
            return (
                text,
                0,
                estimate_tokens(text, self._pack.chars_per_token),
                USAGE_ESTIMATE,
            )

        return self._invoke(PURPOSE_NOTE, case_id, _produce)

    def _produce(self, purpose: str, case_id: str, args: tuple) -> Production:
        raise NotImplementedError


class OfflineGateway(BaseGateway):
    """The offline profile: the model port's deterministic
    composer, metered. Output is exactly the composer's — the
    gateway validates and records it, never edits it — so with
    this profile every artefact of R1–R11 stays byte-identical.

    Usage is the gateway's estimate over the serialised inputs
    and the composed text (no provider meters a local composer);
    the pack prices the profile at zero, so the recorded cost is
    $0.00 whatever the estimate says.

    The richer composers (refund / label summaries) exist only
    when the wrapped composer provides them — the service's
    capability probes (`getattr`) see exactly the wrapped
    composer's surface, as they did before the gateway existed.
    """

    profile_name = PROFILE_OFFLINE
    model_id = "offline-deterministic-composer"

    def __init__(self, backend: Any = None, pack: PricePack | None = None):
        super().__init__(pack)
        self._backend = backend or OfflineComposer()

    def supports(self, name: str) -> bool:
        return hasattr(self._backend, name)

    def __getattr__(self, name: str):
        # Only the optional composers delegate; anything else is a
        # plain AttributeError, so hasattr/getattr probes behave.
        if name in (PURPOSE_REFUND_SUMMARY, PURPOSE_LABEL_SUMMARY):
            backend = self.__dict__.get("_backend")
            fn = getattr(backend, name, None) if backend is not None else None
            if fn is None:
                raise AttributeError(name)
            return lambda *args: self._invoke(
                name, args[0].case_id,
                lambda: self._produce(name, args[0].case_id, (fn, args)),
            )
        raise AttributeError(name)

    def _produce(self, purpose: str, case_id: str, args: tuple) -> Production:
        if purpose == PURPOSE_SUMMARY:
            decision, product_name = args
            text = self._backend.compose_summary(decision, product_name)
            inputs = _facts(decision, product_name)
        else:
            fn, fn_args = args
            text = fn(*fn_args)
            inputs = json.dumps(
                [a.model_dump(mode="json") if hasattr(a, "model_dump") else str(a) for a in fn_args],
                sort_keys=True, default=str,
            )
        return (
            text,
            estimate_tokens(inputs, self._pack.chars_per_token),
            estimate_tokens(text, self._pack.chars_per_token),
            USAGE_ESTIMATE,
        )


# ---------------------------------------------------------------------------
# Hosted profiles — Bedrock-shaped and NIM-shaped, stub-tested only.
# ---------------------------------------------------------------------------


def _refund_facts(decision: EligibilityDecision, product_name: str, computation: RefundComputation) -> str:
    lines = [_facts(decision, product_name)]
    for line in computation.lines:
        lines.append(f"computation_line: {line.line_id} {line.kind} {line.amount} [{line.status}]")
    lines.append(f"refund_total: {computation.refund_total}")
    lines.append(f"computation_final: {computation.is_final}")
    return "\n".join(lines)


def _label_facts(
    decision: EligibilityDecision,
    product_name: str,
    computation: RefundComputation,
    label: ReturnLabel,
) -> str:
    return _refund_facts(decision, product_name, computation) + "\n" + "\n".join(
        [
            f"label_ref: {label.carrier_ref}",
            f"label_service_level: {label.service_level}",
            f"label_cost: {label.cost}",
        ]
    )


_PROMPT = (
    "You draft one customer-facing returns summary for Cascade Outfitters. "
    "Use ONLY the facts below; state no figure, date or policy term that is "
    "not among them. Reply with a single JSON object: {\"text\": \"<the summary>\"}.\n\n"
)


class _BedrockContent(BaseModel):
    text: str


class _BedrockMessage(BaseModel):
    content: list[_BedrockContent]


class _BedrockOutput(BaseModel):
    message: _BedrockMessage


class _BedrockUsage(BaseModel):
    inputTokens: int
    outputTokens: int


class _BedrockEnvelope(BaseModel):
    """The Bedrock Converse-shaped response envelope, validated.
    Anything else — a missing usage block, empty content — is not
    a response the gateway will meter or use."""

    output: _BedrockOutput
    usage: _BedrockUsage


class BedrockGateway(BaseGateway):
    """The Bedrock profile (stub-tested). Builds a Converse-shaped
    request, hands it to the bound transport, validates the
    envelope and the structured draft inside it. Unbound — the
    default in this build — every call fails closed: no live AWS
    call is possible from here, and none is attempted."""

    profile_name = PROFILE_BEDROCK

    def __init__(
        self,
        *,
        model_id: str | None = None,
        transport: Callable[[dict], dict] | None = None,
        pack: PricePack | None = None,
    ):
        super().__init__(pack)
        self.model_id = model_id or ""
        self._transport = transport

    def compose_refund_summary(
        self, decision: EligibilityDecision, product_name: str, computation: RefundComputation
    ) -> str:
        return self._invoke(
            PURPOSE_REFUND_SUMMARY, decision.case_id,
            lambda: self._produce(
                PURPOSE_REFUND_SUMMARY, decision.case_id,
                (decision, product_name, computation),
            ),
        )

    def compose_label_summary(
        self,
        decision: EligibilityDecision,
        product_name: str,
        computation: RefundComputation,
        label: ReturnLabel,
    ) -> str:
        return self._invoke(
            PURPOSE_LABEL_SUMMARY, decision.case_id,
            lambda: self._produce(
                PURPOSE_LABEL_SUMMARY, decision.case_id,
                (decision, product_name, computation, label),
            ),
        )

    def _facts_for(self, purpose: str, args: tuple) -> str:
        if purpose == PURPOSE_SUMMARY:
            return _facts(args[0], args[1])
        if purpose == PURPOSE_REFUND_SUMMARY:
            return _refund_facts(args[0], args[1], args[2])
        return _label_facts(args[0], args[1], args[2], args[3])

    def _produce(self, purpose: str, case_id: str, args: tuple) -> Production:
        if not self.model_id:
            raise ModelProfileUnavailableError(
                "bedrock profile selected but no model id is configured "
                "(RETURNS_BEDROCK_MODEL_ID); refusing rather than falling back"
            )
        if self._transport is None:
            raise ModelProfileUnavailableError(
                "bedrock profile has no transport bound in this build; live "
                "Bedrock is stub-tested only and no call was attempted"
            )
        request = {
            "modelId": self.model_id,
            "messages": [
                {"role": "user", "content": [{"text": _PROMPT + self._facts_for(purpose, args)}]}
            ],
            "inferenceConfig": {"maxTokens": 1024, "temperature": 0},
        }
        try:
            raw = self._transport(request)
        except ModelGatewayError:
            raise
        except Exception as exc:
            raise ModelProfileUnavailableError(
                f"bedrock transport failed: {exc}"
            ) from exc
        try:
            envelope = _BedrockEnvelope.model_validate(raw)
            content = envelope.output.message.content[0].text
            draft = DraftOutput.model_validate(json.loads(content))
        except Exception as exc:
            raise InvalidModelOutputError(
                f"bedrock response did not validate as a structured draft: {exc}"
            ) from exc
        return draft.text, envelope.usage.inputTokens, envelope.usage.outputTokens, USAGE_ENVELOPE


class _NimMessage(BaseModel):
    content: str


class _NimChoice(BaseModel):
    message: _NimMessage


class _NimUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int


class _NimEnvelope(BaseModel):
    """The NIM (OpenAI chat-completions-shaped) response envelope,
    validated the same way as the Bedrock one."""

    choices: list[_NimChoice]
    usage: _NimUsage


class NimGateway(BaseGateway):
    """The NVIDIA NIM profile (stub-tested). OpenAI-style chat
    completions envelopes; the model id is configuration; unbound,
    every call fails closed — no live NIM call is attempted."""

    profile_name = PROFILE_NIM

    def __init__(
        self,
        *,
        model_id: str | None = None,
        transport: Callable[[dict], dict] | None = None,
        pack: PricePack | None = None,
    ):
        super().__init__(pack)
        self.model_id = model_id or ""
        self._transport = transport

    def compose_refund_summary(
        self, decision: EligibilityDecision, product_name: str, computation: RefundComputation
    ) -> str:
        return self._invoke(
            PURPOSE_REFUND_SUMMARY, decision.case_id,
            lambda: self._produce(
                PURPOSE_REFUND_SUMMARY, decision.case_id,
                (decision, product_name, computation),
            ),
        )

    def compose_label_summary(
        self,
        decision: EligibilityDecision,
        product_name: str,
        computation: RefundComputation,
        label: ReturnLabel,
    ) -> str:
        return self._invoke(
            PURPOSE_LABEL_SUMMARY, decision.case_id,
            lambda: self._produce(
                PURPOSE_LABEL_SUMMARY, decision.case_id,
                (decision, product_name, computation, label),
            ),
        )

    def _facts_for(self, purpose: str, args: tuple) -> str:
        if purpose == PURPOSE_SUMMARY:
            return _facts(args[0], args[1])
        if purpose == PURPOSE_REFUND_SUMMARY:
            return _refund_facts(args[0], args[1], args[2])
        return _label_facts(args[0], args[1], args[2], args[3])

    def _produce(self, purpose: str, case_id: str, args: tuple) -> Production:
        if not self.model_id:
            raise ModelProfileUnavailableError(
                "nim profile selected but no model id is configured "
                "(RETURNS_NIM_MODEL_ID); refusing rather than falling back"
            )
        if self._transport is None:
            raise ModelProfileUnavailableError(
                "nim profile has no transport bound in this build; live NIM "
                "is stub-tested only and no call was attempted"
            )
        request = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": _PROMPT + self._facts_for(purpose, args)}],
            "temperature": 0,
            "max_tokens": 1024,
        }
        try:
            raw = self._transport(request)
        except ModelGatewayError:
            raise
        except Exception as exc:
            raise ModelProfileUnavailableError(f"nim transport failed: {exc}") from exc
        try:
            envelope = _NimEnvelope.model_validate(raw)
            content = envelope.choices[0].message.content
            draft = DraftOutput.model_validate(json.loads(content))
        except Exception as exc:
            raise InvalidModelOutputError(
                f"nim response did not validate as a structured draft: {exc}"
            ) from exc
        return draft.text, envelope.usage.prompt_tokens, envelope.usage.completion_tokens, USAGE_ENVELOPE


def load_model_gateway(
    profile: str,
    *,
    bedrock_model_id: str | None = None,
    nim_model_id: str | None = None,
    backend: Any = None,
    pack: PricePack | None = None,
) -> BaseGateway:
    """Build the gateway for a configured profile name. `offline`
    wraps the given composer backend (the deterministic composer
    by default); `bedrock` (and its R1-era alias `hosted-bedrock`)
    and `nim` build the hosted profiles, fail-closed until a
    transport is bound by a deployment. An unknown profile is an
    error — never a silent offline substitution."""
    if profile == PROFILE_OFFLINE:
        return OfflineGateway(backend, pack=pack)
    if profile in (PROFILE_BEDROCK, "hosted-bedrock"):
        return BedrockGateway(model_id=bedrock_model_id, pack=pack)
    if profile == PROFILE_NIM:
        return NimGateway(model_id=nim_model_id, pack=pack)
    raise ValueError(f"unknown model profile: {profile!r}")
