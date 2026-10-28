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
  Migrations are numbered, recorded in a ledger, applied once. R10
  verified the Postgres dialect end to end (see the section below);
  the SQLite connection opens with `check_same_thread=False` because
  the HTTP surfaces serve from a threadpool and a thread-bound
  connection 500s every threaded store read.
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

## Warehouse grading and the supersession cascade (R7)

Everything before R7 priced and routed a return on the customer's
*declared* condition. R7 is where the parcel comes back and the
declaration meets the item. Two recorded acts open it: the
**warehouse receipt**, which exists only when the carrier port's
event script shows the case's active label `received` as of the
receipt date — the warehouse cannot conjure a parcel the carrier
has not delivered, and replaying the same act returns the same
receipt while a different act against a receipted label refuses —
and the **grading report**, one per case, by a named grader who
must not be the person who received the parcel (the approval
ladder's separation of duties, applied to the warehouse floor).

The report carries the graded condition from the declared
vocabulary, damage notes, the parcel's received weight, and, for
held electronics, the two facts R3's hold named as unknowns:
functional test result and data-wipe status. A held case's grading
that omits either refuses to finalise, naming what inspection had
to establish; with both present, the hold resolves through the
grading pack's inspection table (`grading.py`, versioned data):
pass + verified wipe → REFURBISH via the store; a failed test →
RECYCLE whatever the wipe; pass + unverified wipe → RECYCLE,
because a device that cannot be proven wiped is never resold. The
successor is a new disposition decision whose cell id is the
grading rule and whose `supersedes` names the hold.

The cascade then runs in code, in one order, each step a new
linked artefact. (1) A graded condition different from the declared
one supersedes the disposition: a fresh matrix walk with
`basis: graded`, both conditions on the record, channel routability
re-checked for the graded season. (2) The refund re-prices on the
graded condition — `compute_refund` with a condition override and
a `supersedes` link; the fee lines record declared and graded
conditions, the fingerprint covers the override, and the grading
report states the **delta** between the old and new totals as its
own figure. (3) If the old computation already executed, the delta
is never absorbed: a negative delta becomes a `recovery_due`
**adjustment** — amount, reason, both computation ids, standing
open as a recorded receivable with its approval requirement noted,
and deliberately no execution path (no clawback is built); a
positive delta becomes a `top_up_due` adjustment executed through
the R5 machinery again — fraud guard, approval status re-derived
against the NEW computation (approvals pinned to the superseded
one are simply not counted), and keys namespaced `topup:` so the
two domains can never collide. The top-up pays the per-tender
difference between the computations, so its receipt can never
exceed what the graded truth says was underpaid; and a plain
`execute_refund` against the new computation refuses once the case
settled elsewhere, which is what makes the adjustment path the
only door. (4) The summary re-issues against the new computation
through the same figure-verified compose, so the customer reads
the graded figures and nothing else. Because a failed verification
must leave no half-cascade, the whole cascade is computed in
memory and verified before the first artefact is stored.

Two supporting changes make the chains real. Migration 0007
rebuilds `refund_computations` and `disposition_decisions` without
their one-per-case uniqueness (artefact ids were always the keys)
and adds `warehouse_receipts`, `grading_reports` and
`refund_adjustments`; both stores resolve a case's current
computation or disposition by following the chain — an artefact is
the head when no other names it as `supersedes` — and list the
whole chain for the timeline and the graders. And the fraud pack
gains its second trigger: the report's received weight replaces
the intake-scan weight as the scoring fact (the request artefact
is never edited; the override lives in the service's fact
assembly), and a grading at least two condition ranks worse than
declared, with wear evidence on the report, re-scores the case
through the unchanged R4 engine — a new score artefact, linked,
never an edit. The timeline grows receipt, grading and adjustment
stages and names the `warehouse_grading` gap for a parcel received
but not yet graded.

## Customer messaging: templates, the verifier, the lifecycle (R8)

R1's figure verifier guarded exactly one text: the summary the
composer drafted. R8 generalises it into the messaging verifier
and puts every customer-facing word the system can produce behind
it. The unit of control is the **message type**: six typed
templates (`messaging.py`) — return summary, label instructions,
grading-outcome notice, fraud-review outcome notice,
refund-executed notice, adjustment notice — each rendered by a
pure function from a `MessageFacts` assembly the service builds
out of the case's artefacts of record. A type whose artefacts do
not exist refuses to draft, naming the missing one (no label, no
label instructions; no review decision, no fraud notice). The one
free-form surface is a stated note slot appended to a template's
text: model prose may live there and nowhere else, and it passes
through the same verifier as the template's own words, so the slot
is not a bypass.

`verify_message_text` runs three checks against a `MessageContext`
— the verified universe for that case. **Figures**: the R1 checks
(amounts, dates) plus percentages, each required to equal a value
some artefact computed. **Facts**: dispositions and conditions are
recognised only in statement constructions (the templates' own
phrasings and their close variants — a bare "used" is ordinary
English, and "restocking fee" is fee vocabulary, not a disposition
claim; both traps are handled explicitly in the pattern table) and
must belong to the record's fact sets, which include superseded
artefacts: history may be described, the present may not be
misstated. **Forbidden content**: a versioned disclosure pack
states banned terms and patterns, and the service extends the term
list at context-build time with the scoring pack's signal ids and
review reason codes — derived, so the ban cannot drift from the
pack it protects. Internal artefact ids are recognised by shape
and admitted only when they are the case's own public references
(its case and order ids, its carrier and provider references);
another customer's ids never are. A failed verification is not an
error swallowed or a text quietly repaired: the message is stored
`refused`, violations listed on the artefact.

The lifecycle is a state machine of recorded acts — draft,
verified, queued, sent, with refused and withdrawn as terminal
side-states — each transition appended with date and actor, and
sending delegated to a messaging port whose local double is
idempotent by message id. Staleness is handled by **pin +
fingerprint**: at creation, a message pins the artefacts its type
renders against (the summary pins the decision, the computation
chain, the disposition chain and the label — grading moves the
record under a summary even when no figure changes, because the
disposition chain is part of what its facts were checked against).
Issuing a successor message of the same type withdraws any unsent
predecessor; anything else that moves the pin is caught at send
time, where the fingerprint is recomputed and a mismatch withdraws
the message, drafts and verifies a replacement, and refuses the
send naming it. The R1–R7 summary path is migrated, not replaced:
ingest, label issuance and the grading cascade record their
summary as a message through the same service path, and the
summary text itself is byte-identical to what R1–R7 produced —
the eval gate asserts every feed case's standing summary verified
clean against the generalised verifier.

## Customer memory (R9)

Memory is a port (`MemoryPort`), shaped on AgentCore Memory's
long-term records, with one implementation: `LocalMemoryDouble`,
a store-backed stand-in labelled on every record it writes. The
port's surface is deliberately narrow — `write`, `find` (the
write-side existence read), `existing_keys`, `retrieve`,
`retention_status` — and its read takes an exact customer id and
nothing else: no search, no filter, no cross-customer surface
exists anywhere in the system, and the store enforces the
predicate. Retrieval filters expired records and writes an
access-log entry (customer, purpose, actor, returned ids) before
returning; the log is a first-class read surface, because a
memory nobody audits is a memory nobody should trust.

Records are event-shaped and content-addressed. The service
derives them in code at four milestones — case decided, refund
executed, case graded, fraud review closed — from the artefacts
of record: amounts, disposition, graded condition, refund delta,
the fraud band (never score internals), the review outcome. A
decision record also embeds the exact `CaseFact` the engine
scored, which is what makes memory a complete history source for
the engine rather than a summary of it. Writes are idempotent
per (case, event, source artefact); `sync_memory` backfills
milestones the live hooks predate, planted history included.

Retention is data: the memory pack (`2026.10.1`) states the
window (730 days) and each record's expiry derives from its
event date. Expired records leave every consumer's view and are
counted by the retention read — tombstones, never deletions; no
code path in this build deletes a record.

The two consumers are graded against the record. The fraud
engine's history input is an explicit provenance-tagged bundle
(`HistoryFacts.source: store | memory`); the score artefact
carries the tag but excludes it from its content fingerprint, so
a memory-sourced score over identical facts IS the store-path
artefact — parity is an equality check, not a similarity check.
Grading records' weigh-ins supersede intake weights on the
memory path exactly as warehouse grading supersedes them on the
store path. The messaging verifier's figure universe extends to
the case's own customer's memory records (retrieved through the
port, purpose `message_verification`): a prior refund is sayable
only as memory states it, only for that customer, and only while
the record is unexpired.

## The Postgres profile and the production smoke (R10)

The store's second dialect is no longer a claim. The test suite
gains a Postgres profile via a conftest that redirects each
sqlite-path store to a private schema (path-hashed) on the test
database — schema-only `search_path`, because a `,public` tail let
an unmigrated schema silently resolve against public's tables and
masked exactly the failures the profile exists to catch. The eval
gate runs its whole scenario body per profile and compares the
content-addressed artefact ids across in-memory, SQLite and
Postgres: identity across dialects is the parity claim, and it
holds because payloads are JSON text and every ordering the domain
depends on (chain heads by supersession graph, memory logs by
recorded sequence) is computed in code, not inherited from a
dialect's row order. `scripts/prod_smoke.py` is the deploy-shaped
check: a Runtime subprocess and the API on one store walk a planted
case across every round's machinery over HTTP and print the ledger.

## The gateway and the policy pack (R11)

R6 built the gateway as the carrier tools' road; R11 makes it the
only road. Every tool-shaped operation — the four carrier tools,
refund execution, the warehouse grading callback — is a named
tool with an MCP-style definition, invoked through one path, and
recorded in one invocation log: tool, case, actor, digests of the
arguments and of the result, outcome. The money tool adds a
prepared-execution contract on top: the service's governed flow
prepares an execution record (gates passed, status `executing`),
and the handler verifies that record — case, computation, amount —
before the payments port is called, so a bare tool invocation
cannot become a money path that skips the ledger.

In front of the handlers stands the policy pack
(`gateway_policy.py`): versioned data in the Cedar shape — named
policies, each governing named tools under one stated deny
condition over a closed kind vocabulary (actor blocked, actor
permit, required fact, amount-not-above-fact, per-role amount
cap) — evaluated first-match in pack order by a small evaluator
in code. The service builds the facts from the artefacts of
record; an unestablishable fact denies. The pack governs actors
on every tool and the money tool's state and arguments; the
carrier handlers keep their own state gates as the second line,
so their named domain refusals (ineligible, fraud-held) survive
unchanged behind the pack.

The denial shape is AgentCore's, mirrored deliberately: a Policy
denial is not an HTTP error. The transport succeeds and the
denial rides back as a JSON-RPC error payload, code -32002,
naming the denying policy; the invocation stands in the log with
status `denied` and that policy id, and the handler never ran.
The harness hermeticity fix belongs to this round too: every
Postgres harness schema now carries a run-scoped suffix and the
migration ledger write is conflict-tolerant, because a gate that
only passes on a fresh cluster is grading the cluster, not the
code.

## What is deliberately not here yet

Injection quarantine, model cost ledger, console, access
control, reporting, carrier exceptions, red team, deploy
artefacts. The README's honest status is the authoritative
per-round account.
