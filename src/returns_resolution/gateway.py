"""The gateway port — Law 6 made mechanical (R6, generalized R11).

Every tool-shaped operation in the blueprint flows through this
one gateway: the carrier tools of R6 (label, void, tracking read,
pickup), refund execution (R5's payments call, R11), and the
warehouse grading callback (R7's report, R11). The gateway is
shaped on AgentCore Gateway: named tools with typed input/output
schemas (MCP-style definitions, listed in code below), one
invocation path, and an invocation log that is the audit surface —
every invocation recorded as an artefact: the tool, the case, the
actor, a digest of the exact arguments, a digest of the result,
the timestamp, and the outcome.

`ToolGateway` below is the LOCAL DOUBLE for that port — a stand-in
for tests and demos. It calls no real gateway and no real target.
In production the port is bound to AgentCore Gateway, whose MCP
targets front the retailer's OMS, carrier, warehouse and payments
integrations; the tool definitions, the invocation log and the
policy evaluation in front of the handlers do not change.

Two refusal shapes, kept distinct on the record:

- **A tool refusal** — unknown tool, or the handler's own gates
  refuse (the message names the reason): recorded with status
  "error" and re-raised as the handler's error type, so surfaces
  map it precisely. The record is written either way.
- **A policy denial (R11)** — the gateway policy pack
  (gateway_policy.py) is evaluated BEFORE the handler runs; a
  denial means the handler is never reached and nothing executes.
  In AgentCore a Policy denial is NOT an HTTP error: the transport
  succeeds and the denial rides back as a JSON-RPC error payload
  (code -32002). The double mirrors that shape —
  `policy_denial_envelope` — records the invocation with status
  "denied" and the denying policy's id, and raises
  PolicyDeniedError carrying the denial, so a surface can render
  the envelope instead of an HTTP failure.

Produced artefacts are stamped with the call's id (`via_call_id`
on labels and pickups), so an artefact names the call that made it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Callable

from pydantic import BaseModel

from .gateway_policy import PolicyDenial
from .models import ToolCallRecord

# The JSON-RPC error code AgentCore Policy denials carry. A denial
# is delivered over a successful (HTTP 200) transport inside this
# payload shape — it is not an HTTP error, and the double mirrors
# that rather than inventing a friendlier shape.
POLICY_DENIAL_CODE = -32002


class ToolError(RuntimeError):
    """A gateway invocation was refused — unknown tool, or the
    tool's own gates refused (the message names the reason)."""


class PolicyDeniedError(ToolError):
    """The gateway policy pack denied this invocation. The handler
    never ran and nothing executed. Carries the denial of record
    (and the invocation id), so a surface can render the AgentCore
    denial envelope — HTTP 200 + JSON-RPC -32002 — instead of an
    HTTP error."""

    def __init__(self, denial: PolicyDenial, *, invocation_id: str):
        super().__init__(
            f"gateway policy denial [{denial.policy_id}] on {denial.tool} "
            f"for actor {denial.actor!r}: {denial.reason}"
        )
        self.denial = denial
        self.invocation_id = invocation_id

    def envelope(self) -> dict[str, Any]:
        return policy_denial_envelope(self.denial, invocation_id=self.invocation_id)


def policy_denial_envelope(denial: PolicyDenial, *, invocation_id: str) -> dict[str, Any]:
    """The AgentCore-shaped denial: a JSON-RPC error payload that
    rides a successful transport. This — not an HTTP status — is
    how a Policy denial reaches a caller in production."""
    return {
        "jsonrpc": "2.0",
        "id": invocation_id,
        "error": {
            "code": POLICY_DENIAL_CODE,
            "message": denial.reason,
            "data": {
                "policy_id": denial.policy_id,
                "policy_pack_version": denial.pack_version,
                "tool": denial.tool,
                "actor": denial.actor,
            },
        },
    }


class ToolDefinition(BaseModel):
    """One tool, as the gateway lists it (MCP-style)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_description: str


class GatewayRequest(BaseModel):
    """One invocation as the port carries it: the tool, the exact
    arguments, the case it acts on, and the invoking actor."""

    tool: str
    arguments: dict[str, Any]
    case_id: str
    actor: str = "staff"
    called_on: date


class GatewayResponse(BaseModel):
    """The port's response envelope. `outcome` is "result" (the
    handler ran; `result` carries its JSON), "denied" (a policy
    denial; `error` carries the JSON-RPC -32002 payload), or
    "error" (a tool refusal; `error` carries the named reason)."""

    invocation_id: str
    tool: str
    outcome: str  # result | denied | error
    result: Any | None = None
    error: dict[str, Any] | None = None


TOOL_DEFINITIONS: list[ToolDefinition] = [
    ToolDefinition(
        name="create_return_label",
        description=(
            "Issue the return shipping label for an eligible case. The service level and "
            "cost are read off the policy pack's carrier schedule; the label's cost is "
            "reconciled against the case's refund computation on the artefact. Idempotent "
            "per case: re-issuing returns the active label."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "issued_on": {"type": "string", "format": "date"},
            },
            "required": ["case_id", "issued_on"],
        },
        output_description="The ReturnLabel artefact (or the case's existing active label).",
    ),
    ToolDefinition(
        name="void_return_label",
        description=(
            "Void a case's active label, by a named actor with a stated reason. Produces a "
            "void artefact; the label itself is never edited beyond its status, and a fresh "
            "label may issue afterwards."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "label_id": {"type": "string"},
                "voided_by": {"type": "string"},
                "reason": {"type": "string"},
                "voided_on": {"type": "string", "format": "date"},
            },
            "required": ["case_id", "label_id", "voided_by", "reason", "voided_on"],
        },
        output_description="The LabelVoid artefact.",
    ),
    ToolDefinition(
        name="get_carrier_events",
        description=(
            "Read a label's tracking events from the carrier port, as of a date. Read-only: "
            "the call is recorded, nothing is written but the record."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "label_id": {"type": "string"},
                "as_of": {"type": "string", "format": "date"},
            },
            "required": ["case_id", "label_id", "as_of"],
        },
        output_description="The label's CarrierEvent list as of the date.",
    ),
    ToolDefinition(
        name="book_pickup",
        description=(
            "Book a carrier pickup for the case's active label. Only categories the policy "
            "pack flags as bulky may book a pickup; any other category is refused naming "
            "that rule."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "scheduled_on": {"type": "string", "format": "date"},
                "label_id": {"type": "string"},
            },
            "required": ["case_id", "scheduled_on"],
        },
        output_description="The PickupBooking artefact.",
    ),
    ToolDefinition(
        name="execute_refund",
        description=(
            "Execute a refund through the payments port — the only money-moving tool. "
            "Invocable only against an execution the service has prepared (its gates "
            "passed and an execution record stands in 'executing'): the handler verifies "
            "the prepared record and the amount before the port is called, and the "
            "gateway policy pack denies the call up front when the case's fraud path "
            "is blocked, the named computation is not the approved one, it has already "
            "been executed under another key, or the amount exceeds the computed "
            "figure or the invoking role's cap. Idempotent by the execution's key."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "computation_id": {"type": "string"},
                "amount": {"type": "string", "description": "Decimal string, the computed refund total"},
                "tender_allocations": {"type": "array"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["case_id", "computation_id", "amount", "idempotency_key"],
        },
        output_description="The payments port's ProviderReceipt.",
    ),
    ToolDefinition(
        name="submit_grading_report",
        description=(
            "The warehouse's grading callback: record what actually arrived — the "
            "graded condition, the inspection facts, the weigh-in — and run the "
            "supersession cascade (disposition, computation, adjustment, summary, "
            "fraud re-score) in its stated order. The grader may not be the receiver "
            "of record (separation of duties, enforced by the handler)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "grader": {"type": "string"},
                "graded_condition": {"type": "string"},
                "graded_on": {"type": "string", "format": "date"},
                "functional_test_result": {"type": ["string", "null"]},
                "data_wipe_status": {"type": ["string", "null"]},
                "damage_notes": {"type": "string"},
                "received_weight_grams": {"type": ["integer", "null"]},
                "wear_evidence": {"type": "boolean"},
            },
            "required": ["case_id", "grader", "graded_condition", "graded_on"],
        },
        output_description="The GradingReport artefact.",
    ),
]


def inputs_hash(arguments: dict[str, Any]) -> str:
    """Hash of the exact invocation inputs, canonical JSON."""
    blob = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _result_json(result: Any) -> Any:
    """The result as the envelope carries it: JSON, via each
    artefact's own JSON form."""
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    if isinstance(result, list):
        return [_result_json(item) for item in result]
    return result


def result_hash(result: Any) -> str:
    """Digest of the produced result, canonical JSON — the result
    half of the invocation record's provability."""
    blob = json.dumps(_result_json(result), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# The policy evaluator the service binds: given the tool, the
# actor, the case and the exact arguments, it returns the pack's
# denial of record or None. The gateway owns WHEN evaluation
# happens (before the handler, always); the service owns the facts
# (it holds the store), the pack owns the rules.
PolicyEvaluator = Callable[[str, str, str, dict[str, Any]], PolicyDenial | None]


class ToolGateway:
    """The local double for the gateway port — the one invocation
    path for tools.

    Handlers are bound callables supplied by the service; the
    gateway owns the definitions, the call ids, the policy
    evaluation in front of the handlers, and the record. `record`
    is the store's put for a call record payload.
    """

    def __init__(
        self,
        handlers: dict[str, Callable[..., Any]],
        record: Callable[[dict[str, Any]], None],
        count_calls: Callable[[str], int],
        policy_evaluator: PolicyEvaluator | None = None,
    ):
        self._handlers = dict(handlers)
        self._record = record
        self._count_calls = count_calls
        self._policy_evaluator = policy_evaluator
        self._definitions = {d.name: d for d in TOOL_DEFINITIONS}
        unregistered = set(self._definitions) - set(self._handlers)
        if unregistered:
            raise ValueError(f"tools defined but not handled: {sorted(unregistered)}")

    def definitions(self) -> list[ToolDefinition]:
        return list(TOOL_DEFINITIONS)

    def definition(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def _run(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        case_id: str,
        called_on: date,
        actor: str,
    ) -> tuple[ToolCallRecord, Any, Exception | None]:
        """One invocation, recorded whatever happens. Returns
        (record, result, error): exactly one of result/error is
        set, and the record is already written either way."""
        digest = inputs_hash(arguments)
        seq = self._count_calls(case_id)
        blob = json.dumps(
            {
                "tool": tool_name,
                "case_id": case_id,
                "inputs_hash": digest,
                "called_on": called_on.isoformat(),
                "seq": seq,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        call_id = "TC-" + hashlib.sha256(blob.encode()).hexdigest()[:16]
        record = ToolCallRecord(
            call_id=call_id,
            tool=tool_name,
            case_id=case_id,
            inputs_hash=digest,
            status="ok",
            called_on=called_on,
            actor=actor,
        )
        handler = self._handlers.get(tool_name)
        if handler is None:
            record.status = "error"
            record.error = f"unknown tool: {tool_name!r} is not a gateway tool"
            self._record(record.model_dump(mode="json"))
            return record, None, ToolError(record.error)
        if self._policy_evaluator is not None:
            denial = self._policy_evaluator(tool_name, actor, case_id, arguments)
            if denial is not None:
                # Denied: the handler never runs, nothing executes,
                # and the denial stands on the invocation log with
                # the policy that made it.
                record.status = "denied"
                record.policy_id = denial.policy_id
                record.error = denial.reason
                self._record(record.model_dump(mode="json"))
                return record, None, PolicyDeniedError(denial, invocation_id=call_id)
        try:
            result = handler(call_id=call_id, **arguments)
        except Exception as exc:
            record.status = "error"
            record.error = str(exc)
            self._record(record.model_dump(mode="json"))
            return record, None, exc
        record.result_ref = getattr(result, "result_ref", None) or _result_ref(result)
        record.result_hash = result_hash(result)
        self._record(record.model_dump(mode="json"))
        return record, result, None

    def invoke(
        self, tool_name: str, arguments: dict[str, Any], *, case_id: str, called_on: date,
        actor: str = "staff",
    ) -> tuple[ToolCallRecord, Any]:
        """Invoke one tool and record the call, whatever happens.

        Returns (record, handler result). A refusal — unknown tool,
        a policy denial, or a handler's named refusal — is recorded
        and re-raised (a denial as PolicyDeniedError, a handler
        refusal as the handler's own error type, preserved so
        surfaces can map it precisely).
        """
        record, result, error = self._run(
            tool_name, arguments, case_id=case_id, called_on=called_on, actor=actor
        )
        if error is not None:
            raise error
        return record, result

    def invoke_envelope(self, request: GatewayRequest) -> GatewayResponse:
        """Invoke through the port's envelope, AgentCore-shaped:
        the transport never fails for a tool or policy outcome —
        a denial arrives as outcome "denied" with the JSON-RPC
        -32002 payload, a tool refusal as outcome "error" with
        its named reason."""
        record, result, error = self._run(
            request.tool,
            request.arguments,
            case_id=request.case_id,
            called_on=request.called_on,
            actor=request.actor,
        )
        if isinstance(error, PolicyDeniedError):
            return GatewayResponse(
                invocation_id=record.call_id,
                tool=request.tool,
                outcome="denied",
                error=error.envelope()["error"],
            )
        if error is not None:
            code = -32601 if self.definition(request.tool) is None else -32603
            return GatewayResponse(
                invocation_id=record.call_id,
                tool=request.tool,
                outcome="error",
                error={"code": code, "message": str(error)},
            )
        return GatewayResponse(
            invocation_id=record.call_id,
            tool=request.tool,
            outcome="result",
            result=_result_json(result),
        )


def _result_ref(result: Any) -> str | None:
    """The artefact id a call produced, where the result carries one."""
    for attr in ("label_id", "void_id", "pickup_id", "provider_ref", "report_id"):
        value = getattr(result, attr, None)
        if value:
            return str(value)
    if isinstance(result, list) and result:
        return getattr(result[0], "carrier_ref", None)
    return None
