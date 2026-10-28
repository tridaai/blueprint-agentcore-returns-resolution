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
        ├─ refunds.compute_refund(refund schedule) -> RefundComputation (R2)
        ├─ disposition.decide_disposition(matrix, channels)   [eligible cases only]
        |     -> DispositionDecision {cell, channel, reroutes[]} | not_decidable (R3)
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
  design, wired in across rounds (Payments R5, the Gateway-shaped
  tool port R6; Memory R9, Policy R11, Identity R16). In R1 the only
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

## The refund computation (R2)

`refunds.py` is the money of record. From the decision, the order as
stored and the pack's refund schedule (restocking rates by declared
condition; free-return categories; the flat return-shipping fee) it
produces a `RefundComputation`: named lines (item credit, restocking
fee, return-shipping deduction), each carrying its derivation —
pinned inputs, the rule, the policy reference — plus totals and a
tender allocation. The artefact is content-addressed: its id hashes
the decision + order fingerprints, the pack version and the engine
version, and `verify_computation` re-performs the whole computation
and compares every field, so a tampered figure is detected rather
than believed. A component whose input is missing (a condition with
no rate in the pack; an order with no tender breakdown) is recorded
`not_computable` *naming the input* — never a silent zero; while a
money line is not computable the total is `None` and `is_final` is
False. The decision's R1 `refund_due` remains the pre-fee eligibility
figure; from R2 the computation's `refund_total` is the refund. The
figure verifier's amount universe extends to the computation's
lines, totals and allocations when a computation is supplied, so the
summary quotes the computed refund — and still nothing else.

## The disposition matrix (R3)

`disposition.py` decides where a returned item goes. The matrix is a
versioned pack (data, fail-closed loader): 36 primary cells over
(category x declared condition x age band) plus stated fallback
cells, each naming a disposition from a closed vocabulary (RESTOCK /
REFURBISH / OUTLET / LIQUIDATE / RETURN_TO_VENDOR / RECYCLE /
HOLD_FOR_INSPECTION) and the resale channel it flows through. Item
age is banded from the request and delivery dates (fresh 0–30,
season 31–90, aged 91+). Channel availability is a separate versioned
table: when a cell's channel is closed for the category, the walk
falls to the cell's *stated next cell* and records the hop as a
reroute step on the decision — the shipped table closes the apparel
liquidator, so aged used apparel lands on its fallback cell (RECYCLE
via the recycler) with the reroute on the record. A fact set with no
covering cell — an unknown category, a declared condition outside
the matrix vocabulary, an age that bands nowhere, a closed channel
with no stated next — returns `not_decidable` *naming the missing
dimension*; there is no default disposition. Decisions are
content-addressed artefacts pinned to the case, the computation,
both pack versions and the channel table's fingerprint, and
`verify_disposition` re-performs the walk to detect tampering,
exactly as the refund verifier re-performs money. The R3 basis is
the customer's *declared* condition (`basis: declared`); warehouse
grading in R7 supersedes with a new decision linked by `supersedes`.
HOLD_FOR_INSPECTION cells name the facts inspection must establish
(for used electronics: functional test result, data-wipe status).
The matrix covers the full grid, including cells the current policy
windows make unreachable at intake — the matrix is the disposition
policy of record for an item that arrives anyway, not an intake-only
table.

## The fraud scoring pack (R4)

`fraud.py` scores cases for fraud. The pack is versioned data with a
fail-closed loader: parameters for five signals, their tiers and
caps, the band thresholds (CLEAR < 25 <= MONITOR < 60 <=
FRAUD_REVIEW) and the review vocabulary (outcome -> coded reasons).
The engine is a pure function over *facts read from stored
artefacts* — the customer's stored orders and stored cases — and
sums Decimal signal contributions:

- velocity: prior cases inside 90 days beyond an allowance of 2,
  8 points each, capped at 40;
- serial-returner share: returned/total orders, tiered to 35, and
  `insufficient_data` below 4 orders — a share over too little
  history is named, not computed;
- wardrobing: wear-category (apparel/footwear) cases declared
  new-with-tags at 21+ days of ownership, 12 each, capped at 36;
- weight mismatch: the return's scanned weight vs the product
  master's shipped weight — ratio under 0.50 scores 30 (empty-box
  band), under 0.80 scores 10; a missing scan or a missing master
  weight is `not_computable` naming the input;
- card churn: distinct tender methods across the customer's
  orders, tiered to 20; no tender breakdown anywhere on record is
  `insufficient_data` naming `payment_tender_breakdown`.

A score is a content-addressed artefact pinned to the case, the
pack version and the input artefact ids under a content
fingerprint; `verify_fraud_score` re-performs it exactly as the
refund and disposition verifiers re-perform theirs. Re-scoring is
supersession by link, never an edit. The case carries a derived
`fraud_state`: a FRAUD_REVIEW band puts the case in `fraud_review`
— its disposition stands, its refund path is blocked, and
`assert_refund_path_open` is the guard the R5 money gates call.
The only act that changes a blocked state is a recorded
fraud-review decision: named reviewer, outcome and reason from the
pack vocabulary, pinned to the score it decides on.
`confirmed_fraud` is sticky (it survives re-scoring);
`cleared_with_reason` lifts the block for that score only, and the
score artefact is never modified by the decision — the arithmetic
and the act stay separate on the record. History is ordered by
(requested_on, case_id) with a same-day case-id tie-break, so a
score never depends on the order cases happened to be ingested in.

## Approval gates and the payments port (R5)

Law 5 has two halves, both mechanical. The **approval gate** reads
its thresholds from the policy pack (`auto_approve_cap`,
`dual_approve_cap`): a final refund total at or below the auto cap
is tier `auto` (no acts required), above it tier `single` (one
named approver), above the dual cap tier `dual` (two approvers,
necessarily different people). An approval is a recorded act —
approver, role, explicit `as_of`, the pack version, and the pinned
computation id + amount. Acts are counted only when pinned to the
case's *current* computation, so an approval of a superseded
computation is inert on the record and refused at record time when
named explicitly. The act refuses, each by name: an unnamed
approver or role, the submitting customer as their own approver, a
denied or not-final computation, an already-executed refund, and —
through the R4 guard `assert_refund_path_open`, called, not
reimplemented — any case under a fraud hold.

The **payments port** is the only path money takes. Its contract is
AgentCore-Payments-shaped: case, pinned computation, amount, the
computation's tender allocation, and a mandatory idempotency key
in; a provider reference out. The sole implementation here is
`LocalPaymentsDouble` — a local stand-in that moves no real money
and says so in its module, its class and the provider string on
every receipt; production binds the configured AgentCore Payments
provider to the same port. Execution checks its preconditions in
code (final eligible computation, fraud path open, decided
disposition, tier satisfied — each refusal names the gap), then
drives a stored execution record through
`requested → executing → executed/failed`, transitions on the
record. Idempotency is structural at three levels: the service
replays a receipt by key before any check that could refuse, the
store makes receipts unique per computation and per key, and the
double itself returns the original reference for a repeated key
without a second movement. A different key on an executed
computation is refused as `duplicate_refund`. A provider failure
(injectable per key on the double) lands as a `failed` execution
with the reason recorded and **no receipt** — the case stays
executable, and the retry continues the same execution record when
it reuses the key.

## Carrier tools behind the gateway, and the timeline (R6)

Tool-shaped work leaves the service through one choke point. The
`ToolGateway` holds MCP-style definitions for the round's four tools
and is the only path that can invoke them: it canonicalises the
inputs to JSON, hashes them, mints a call id from (tool, case,
inputs hash, call date, per-case sequence), and records EVERY call —
success or refusal — through the store before returning. Artefacts
produced by a call are stamped with its id (`via_call_id`), so any
label, void or booking can be traced to the exact recorded act and
its exact inputs. Unknown tools refuse at the gateway; handler
refusals are recorded with status `error` and re-raised in their
original type, so surfaces map them precisely (409, not a generic
500). Tool arguments cross the gateway as JSON; date fields arrive
as ISO strings and parse strictly in the handlers.

Behind the gateway sits the carrier port. Its only implementation
is the local double — labelled in code and on every artefact —
which is deterministic by construction: a label reference hashes
the exact issue inputs including a per-act request reference, so
replaying one issuance act returns its original reference while a
fresh act after a void (new reference, recorded `supersedes`) gets
a new one; `label_count` counts distinct references and is how the
tests grade "the port issued exactly once". Its event script is a
pure function of (label, as_of): tendered on the issue date,
in transit the next day, received three days out — with void state
passed in from the void artefact of record rather than the double's
own memory, because a voided parcel must never read as received
even across a port restart.

The service handler for issuance is a chain of named refusals:
case exists → fraud path open (the R4 guard, called, not
reimplemented) → decision eligible (denial clauses named) → product
of record → shipped weight of record → pack schedule entry. Only
then does idempotency apply: an active label for the case is
returned as-is, before any carrier call. The label artefact pins
its schedule inputs (category, parcel weight, service level, cost)
and its reconciliation against the computation's return-shipping
line. The reconciliation is deliberately a field, computed at
issuance: matched / retailer_absorbed (only when the deduction is
zero AND the category is in the pack's free-return set — the pack's
stated intent, cited in the detail) / variance (exact figure
standing) / not_reconcilable (no computation, or the shipping line
itself not computed — the missing input named). Nothing downstream
ever has to guess whether a cost difference was seen.

When a case already carries a customer summary, issuance extends it
through the model port's label sentence and re-verifies the whole
text BEFORE the label is stored — the verifier's universe gains
exactly one figure (the label cost, as the artefact states it), so
a summary quoting a label cost the artefact doesn't carry fails and
no label lands. Voiding mirrors issuance's discipline: named actor,
stated reason, carrier void first, then the void artefact and the
label's status flip; a second void refuses because a void is a
single act, not a state to re-enter.

The timeline is the read model over all of it: a pure function
assembling the case's stored artefacts into (recorded date, stage
rank, insertion) order, one entry per artefact id. Its gap rules
are expectations derived from state, checked in order — missing
computation, missing disposition, then the label expectation (voided
without replacement / executed without one / ship-by passed), then
an undecided fraud block — each producing a named gap with the
concrete fact (the ship-by date, the void) in its detail. It stores
nothing; two reads of the same record produce the same view.

## What is deliberately not here yet

Warehouse grading (R7),
messaging verifier depth (R8), Memory port (R9), injection quarantine
(R10), Gateway + Policy ports (R11), model cost ledger (R12), console
(R13), Postgres parity + production smoke (R15), access control (R16),
reporting (R17), carrier exceptions (R18), red team (R19), deploy
artefacts (R20). The README's honest status is the authoritative
per-round account.
