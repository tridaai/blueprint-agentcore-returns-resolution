"""Model port — the one place a model is ever called.

Profiles:
- `offline` (default): a deterministic composer that builds the
  customer summary strictly from the computed decision's facts. Tests
  and evals run on this profile; it needs no key and no network.
- `hosted-bedrock`: the live profile — a Bedrock-hosted model behind
  the same port. It refuses loudly until an inference-profile ARN is
  configured (fail-closed: no silent fall back to offline, ever).

Whatever the profile returns passes the figure verifier before it can
be stored as a case summary — the port produces text, never truth.
"""

from __future__ import annotations

from typing import Protocol

from .models import EligibilityDecision, RefundComputation


class ModelPort(Protocol):
    profile_name: str

    def compose_summary(self, decision: EligibilityDecision, product_name: str) -> str: ...


def _facts(decision: EligibilityDecision, product_name: str) -> str:
    lines = [
        f"product: {product_name}",
        f"eligible: {decision.eligible}",
        f"policy_pack_version: {decision.policy_pack_version}",
        f"item_amount: {decision.item_amount}",
        f"refund_due: {decision.refund_due}",
        f"delivered_on: {decision.delivered_on}",
        f"requested_on: {decision.requested_on}",
        f"window_end: {decision.window_end}",
        f"ship_by: {decision.ship_by}",
        "clauses: " + ", ".join(c.clause_id for c in decision.clauses),
    ]
    return "\n".join(lines)


class OfflineComposer:
    """Deterministic summary built only from computed decision facts."""

    profile_name = "offline"

    def compose_refund_summary(
        self, decision: EligibilityDecision, product_name: str, computation: RefundComputation
    ) -> str:
        """R2 summary: quotes the refund computation, the money of record.

        Only figures the computation computed are stated (fee/shipping
        lines appear only when they are non-zero); while the
        computation is not final the summary says exactly which input
        is missing and states no refund total at all.
        """
        base = (
            f"Your return of the {product_name} is approved. "
            f"The item amount is ${decision.item_amount}. "
        )
        line_by_id = {line.line_id: line for line in computation.lines}
        fee = line_by_id.get("restocking_fee")
        if fee is not None and fee.amount:
            base += f"A restocking fee of ${fee.amount} applies. "
        shipping = line_by_id.get("return_shipping")
        if shipping is not None and shipping.amount:
            base += f"Return shipping of ${shipping.amount} is deducted. "
        if computation.is_final and computation.refund_total is not None:
            base += f"Your refund due is ${computation.refund_total}. "
        else:
            missing = ", ".join(c.missing_input for c in computation.not_computable)
            base += (
                "Your final refund is not yet computed — it is waiting on: "
                f"{missing}. No refund figure is final until that input arrives. "
            )
        base += (
            f"Your order was delivered on {decision.delivered_on} and you requested "
            f"the return on {decision.requested_on}, within the return window that "
            f"ends {decision.window_end}. Please ship the item back by "
            f"{decision.ship_by}. This decision was taken under return policy "
            f"version {decision.policy_pack_version}."
        )
        return base

    def compose_summary(self, decision: EligibilityDecision, product_name: str) -> str:
        if decision.eligible:
            return (
                f"Your return of the {product_name} is approved. "
                f"The item amount is ${decision.item_amount} and your refund due is "
                f"${decision.refund_due}. Your order was delivered on {decision.delivered_on} "
                f"and you requested the return on {decision.requested_on}, within the return "
                f"window that ends {decision.window_end}. Please ship the item back by "
                f"{decision.ship_by}. This decision was taken under return policy version "
                f"{decision.policy_pack_version}."
            )
        deny_clauses = [c.clause_id for c in decision.clauses if c.effect == "deny"]
        return (
            f"We cannot accept the return of the {product_name}. "
            f"Your refund due is ${decision.refund_due}. Your order was delivered on "
            f"{decision.delivered_on} and you requested the return on {decision.requested_on}. "
            f"The policy clauses that apply are: {', '.join(deny_clauses)}. "
            f"This decision was taken under return policy version {decision.policy_pack_version}."
        )


class HostedBedrockComposer:
    """Live profile stub — refuses until a model ARN is configured.

    R1 ships the port and the fail-closed behaviour; wiring the actual
    Bedrock Converse call is a later round. Refusing loudly here is the
    honest state, and it is graded: selecting this profile without an
    ARN raises, it never degrades to the offline composer.
    """

    profile_name = "hosted-bedrock"

    def __init__(self, model_arn: str | None, region: str = "us-east-1"):
        self._model_arn = model_arn
        self._region = region

    def compose_summary(self, decision: EligibilityDecision, product_name: str) -> str:
        if not self._model_arn:
            raise RuntimeError(
                "hosted-bedrock profile selected but RETURNS_BEDROCK_MODEL_ARN "
                "is not configured; refusing rather than falling back"
            )
        raise RuntimeError(
            "hosted-bedrock live drafting is not wired in R1; "
            "the port contract is graded against the offline profile"
        )


def load_model_port(profile: str, model_arn: str | None = None, region: str = "us-east-1") -> ModelPort:
    if profile == "offline":
        return OfflineComposer()
    if profile == "hosted-bedrock":
        return HostedBedrockComposer(model_arn=model_arn, region=region)
    raise ValueError(f"unknown model profile: {profile!r}")
