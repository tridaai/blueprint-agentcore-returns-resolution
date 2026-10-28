# Architecture — Returns Resolution & Disposition Agent

## The one-paragraph version

A return request enters through the intake port and becomes a case.
The eligibility engine — pure code over a versioned policy pack —
decides. The model, behind a port, drafts the customer summary from
the decision's facts. A figure verifier checks every amount and date
in the draft against the decision before the summary is stored. Every
surface (AgentCore Runtime entrypoint, FastAPI, demo, harness) drives
the same `CaseService`, so there is exactly one workflow to audit.

## Flow (R1)

```
planted feed / POST /returns / Runtime invocation
        |
        v
  CaseService.ingest ── idempotent by request_id
        |
        ├─ order lookup (store port; seeded gold-by-construction orders)
        ├─ eligibility.evaluate(pack, order, product, request)
        |     -> EligibilityDecision {eligible, pack version, clauses[], amounts, dates}
        ├─ model port .compose_summary(decision)   [offline | hosted-bedrock]
        ├─ verifier.verify_summary(text, decision) -> violations?
        |     -> any violation: case stored summary_failed, NO summary stored
        └─ case + decision + summary records (store port)
```

## AgentCore mapping (honest)

- **Runtime** is the real contract, not a metaphor: the entrypoint is
  the `bedrock-agentcore` SDK's `BedrockAgentCoreApp`, serving
  `POST /invocations` + `GET /ping` on port 8080, one runtime session
  per return case in deployment. A fallback app in the same module
  serves the identical contract if the SDK cannot be imported; the
  dispatch (`handle_payload`) is shared, so behaviour cannot drift
  between the two shapes.
- **Memory, Gateway, Policy, Payments, Identity** are ports in the
  design, wired in later rounds (R5/R9/R11/R16). In R1 the only
  AgentCore-adjacent port with an implementation is the model port,
  whose `hosted-bedrock` profile is deliberately fail-closed: selected
  without a configured inference-profile ARN it raises, because a
  silent fall back to a deterministic composer would be a lie about
  what produced a customer-facing text.
- **Evaluations**: the local eval gate (`evals/run_evals.py`) is the
  R1 stand-in for AgentCore Evaluations — same discipline (planted
  truth, zero-tolerance counters), run in CI-shaped form locally.
  Evaluator configs for the live service land with the deploy round.

## Data and stores

- **Store port**: one SQL implementation, two dialects. SQLite is the
  local default (`./data/returns.db`); `DATABASE_URL` selects
  Postgres. Payloads are JSON **text** columns in both dialects —
  never JSONB — so a record reads back byte-identical on either.
  Migrations are numbered, recorded in a ledger, applied once.
- **Evidence port**: S3-shaped URIs (`s3://returns-evidence/...`),
  content-hash file names, SHA-256 recorded at put and re-verified on
  read. The local double writes to a directory; docker-compose runs
  SeaweedFS as the S3 gateway for the same shape.
- **Money**: `Decimal` end to end, quantised to cents with one
  rounding rule (HALF_UP) at model construction. Floats never touch a
  figure, which is what makes the verifier's equality checks sound.

## The policy pack

`policy.py` holds the pack as data: windows by category, excluded
categories, barred conditions, ship-within days, and a version string
(`2026.10.1`). The engine evaluates the data; decisions name the pack
version and every clause that fired. The loader is fail-closed — an
unknown version raises rather than substituting v1 — because a
decision that names the wrong pack is worse than no decision.

## The figure verifier

`verifier.py` extracts `$amounts` and ISO dates from the *final
rendered* summary text and compares each against the decision's
allowed sets (item amount, refund due; delivered/requested/window-end/
ship-by dates). It runs regardless of which profile drafted the text.
A case whose draft fails verification is stored as `summary_failed`
with the violations recorded and **no summary** — the failure is a
state an operator can see, not a message a customer receives.

## What is deliberately not here yet

Disposition matrix (R3), fraud scoring (R4), approval gates and the
payments port (R5), carrier tools (R6), warehouse grading (R7),
messaging verifier depth (R8), Memory port (R9), injection quarantine
(R10), Gateway + Policy ports (R11), model cost ledger (R12), console
(R13), Postgres parity + production smoke (R15), access control (R16),
reporting (R17), carrier exceptions (R18), red team (R19), deploy
artefacts (R20). The README's honest status is the authoritative
per-round account.
