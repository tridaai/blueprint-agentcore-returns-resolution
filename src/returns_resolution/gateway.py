"""The tool gateway — Law 6 made mechanical (R6).

Carrier and label actions are tools, not prose. Every such action
is invoked through this one gateway, in the shape AgentCore Gateway
presents: named tools with typed input/output schemas (MCP-style
tool definitions, listed in code below), one invocation path, and a
record of every call. The gateway:

- refuses an unknown tool by name — there is no call path around
  the definitions;
- hashes the exact invocation inputs onto the call record, so what
  was asked is provable afterwards;
- records EVERY call — success or refusal — through the record
  sink (the store), with the outcome: what the call produced, or
  the refusal's named reason;
- stamps the produced artefact with the call's id (`via_call_id`
  on labels and pickups), so an artefact names the call that made
  it.

This round wires the carrier tools (label, void, tracking reads,
pickup) through the gateway. The general Gateway port — OMS,
inventory and warehouse targets under the same contract, with the
Policy pack enforcing caps at the gateway — is R11; this module is
the carrier slice of that shape, built now because R6 needs it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Callable

from pydantic import BaseModel

from .models import ToolCallRecord


class ToolError(RuntimeError):
    """A gateway invocation was refused — unknown tool, or the
    tool's own gates refused (the message names the reason)."""


class ToolDefinition(BaseModel):
    """One tool, as the gateway lists it (MCP-style)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_description: str


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
]


def inputs_hash(arguments: dict[str, Any]) -> str:
    """Hash of the exact invocation inputs, canonical JSON."""
    blob = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class ToolGateway:
    """The one invocation path for tools.

    Handlers are bound callables supplied by the service; the
    gateway owns the definitions, the call ids and the record.
    `record` is the store's put for a call record payload.
    """

    def __init__(
        self,
        handlers: dict[str, Callable[..., Any]],
        record: Callable[[dict[str, Any]], None],
        count_calls: Callable[[str], int],
    ):
        self._handlers = dict(handlers)
        self._record = record
        self._count_calls = count_calls
        self._definitions = {d.name: d for d in TOOL_DEFINITIONS}
        unregistered = set(self._definitions) - set(self._handlers)
        if unregistered:
            raise ValueError(f"tools defined but not handled: {sorted(unregistered)}")

    def definitions(self) -> list[ToolDefinition]:
        return list(TOOL_DEFINITIONS)

    def definition(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def invoke(
        self, tool_name: str, arguments: dict[str, Any], *, case_id: str, called_on: date
    ) -> tuple[ToolCallRecord, Any]:
        """Invoke one tool and record the call, whatever happens.

        Returns (record, handler result). A refusal — unknown tool
        or a handler's named refusal — is recorded with status
        "error" and re-raised as ToolError (or the handler's own
        error type, preserved so surfaces can map it precisely);
        the record is written either way.
        """
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
        )
        handler = self._handlers.get(tool_name)
        if handler is None:
            record.status = "error"
            record.error = f"unknown tool: {tool_name!r} is not a gateway tool"
            self._record(record.model_dump(mode="json"))
            raise ToolError(record.error)
        try:
            result = handler(call_id=call_id, **arguments)
        except Exception as exc:
            record.status = "error"
            record.error = str(exc)
            self._record(record.model_dump(mode="json"))
            raise
        record.result_ref = getattr(result, "result_ref", None) or _result_ref(result)
        self._record(record.model_dump(mode="json"))
        return record, result


def _result_ref(result: Any) -> str | None:
    """The artefact id a call produced, where the result carries one."""
    for attr in ("label_id", "void_id", "pickup_id"):
        value = getattr(result, attr, None)
        if value:
            return str(value)
    if isinstance(result, list) and result:
        return getattr(result[0], "carrier_ref", None)
    return None
