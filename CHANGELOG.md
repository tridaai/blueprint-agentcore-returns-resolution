# Changelog — Returns Resolution & Disposition Agent (AWS Bedrock AgentCore)

## 1.0.0 — 2026-10-28

The blueprint at v1.0: the full twenty-round build, frozen by a
release-hardening pass (R20) that re-ran every gate on the
finished tree — the full test suite on the default profile and
on PostgreSQL 16, the eval gate on every store profile with
cross-profile parity graded at zero differences, the production
smoke walking a case over HTTP through its whole lifecycle, the
console correctness check, and the demo end to end. The README,
architecture, trap-suite and red-team docs were audited against
the code as shipped; the drift found (a mapping row promising
evaluator configs and telemetry spans that were never built, a
stale "later round" line for Policy, a deploy-artefacts claim
the R1 scaffold had already overtaken) was corrected in the
docs, and the round sections stand in build order again.

The gaps that remain are stated in the README's Honest status
and are unchanged by this release: the payments, carrier,
messaging, memory, gateway and policy integrations are local
doubles behind their ports, labelled on every artefact they
produce — no live AgentCore, Bedrock or carrier/payment call
has been made; the hosted model profiles are stub-tested and
fail-closed unbound; access enforcement governs calls that
carry a principal, with principal-less calls remaining the
trusted-local path (in production, AgentCore Identity
terminates identity in front of the surfaces); the
split-household link and opener/reviewer separation for fraud
reviews remain open — the first is a data decision for the
retailer, the second a rule the scoring pack does not state;
and the Docker image build is reviewed statically rather than
run (no Docker daemon in the build sandbox). The README's
work-in-progress notice stays on: hands-on developer testing
and the companion blog post are still outstanding.

### The twenty rounds

- **R1 — Core.** Domain core with Decimal money and one
  rounding rule; the Cascade Outfitters catalogue and orders
  gold by construction; the versioned policy pack and the
  eligibility engine (windows by category, exclusions,
  final-sale, customer-damage bar — the window edge graded on
  both sides); an idempotent intake feed; the store port with
  migration 0001 (SQLite default, Postgres dialect, JSON text
  payloads in both); an S3-shaped evidence port; the model
  port with a fail-closed hosted profile; the figure verifier;
  the real `BedrockAgentCoreApp` Runtime entrypoint with a
  contract-identical fallback; the FastAPI surface; demo,
  harness and eval gate. Laws 1–2 land here: eligibility is
  policy-as-data, and the model never computes money.
- **R2 — The refund computation.** The money of record becomes
  a content-addressed artefact: a Decimal ledger whose every
  line carries its derivation, whose missing inputs are named
  `not_computable` rather than zero-filled, and whose stored
  figures a verifier re-performs and tamper-checks
  (`verify_computation`). The customer summary quotes the
  computation; hand-computed fixtures are exact to the cent.
- **R3 — The disposition matrix.** Law 3: where an item goes
  is read off a versioned matrix (36 primary cells over
  category × declared condition × age band, plus stated
  fallbacks) and a versioned channel table — never improvised.
  Closed channels reroute to the cell's stated next cell with
  the hop on the record; an undecidable fact set returns a
  stored `not_decidable` naming the missing dimension;
  HOLD_FOR_INSPECTION names the unknowns a later round must
  establish. Decisions are content-addressed, tamper-checked
  and basis-labelled `declared`.
- **R4 — Fraud scoring.** Law 4: five deterministic signals
  computed in code from stored artefacts only, as a versioned
  pack with a fail-closed loader; scores are content-addressed
  artefacts, re-scored by supersession, never edited; signals
  without evidence say `insufficient_data` / `not_computable`
  and name the missing input. FRAUD_REVIEW is a block on the
  refund path with exactly one door — a named fraud-review
  decision act with a coded outcome and reason. Planted truth
  graded exactly: the serial returner at 95.00 FRAUD_REVIEW,
  the wardrobing pattern at 36.00 MONITOR.
- **R5 — Approval gates + the payments port.** Law 5: the
  pack's caps ($50 auto / $150 dual, the second approver
  necessarily a different person) make approval a recorded act
  pinned to the current computation; execution is
  precondition-checked in code and moves money only through
  the payments port under a mandatory idempotency key — a
  same-key retry replays the original receipt, a different key
  refuses as `duplicate_refund`. The payments implementation
  is `LocalPaymentsDouble`, labelled on every receipt; it
  moves no real money. Provider failures leave a recorded
  `failed` execution, no receipt, and the case executable.
- **R6 — Carrier tools + the timeline.** Law 6: labels, voids,
  tracking reads and pickups are named MCP-style tools invoked
  only through a gateway-shaped port that hashes inputs and
  records every call. Label costs reconcile against the refund
  computation on the artefact itself — the planted $1.55
  variance stands on the record, never absorbed. The carrier
  implementation is a deterministic local double, labelled as
  such. Voiding is a named act; re-issue supersedes, never
  replaces. The case timeline is derived from artefacts, never
  stored, with its gaps named.
- **R7 — Warehouse grading + the supersession cascade.**
  Receipt exists only when the carrier's record shows the
  parcel received; the receiver cannot grade. Grading in one
  stated order: the disposition is superseded by a graded-basis
  walk, the refund is re-priced on the graded condition with
  the delta stated (the planted boots: $130.50 → $116.00,
  delta −$14.50), an already-executed delta becomes an
  adjustment position — a recovery as a recorded receivable
  with **no clawback execution anywhere**, a top-up executed
  only through the R5 gates in a namespaced key domain — and
  the summary is re-issued figure-verified. Held electronics
  resolve through the pack's inspection table (an unprovable
  wipe is never resold). Wild grading disagreement with wear
  evidence re-scores fraud through the R4 machinery.
- **R8 — Customer messaging.** Law 8: every customer-facing
  message is a typed template rendered from artefacts; the
  messaging verifier checks figures, stated facts and
  forbidden content (a versioned disclosure pack whose fraud
  vocabulary derives from the scoring pack, so the ban cannot
  drift) before a message may be sent; the lifecycle —
  draft → verified → queued → sent — is recorded acts through
  a messaging port with a labelled local double; sends are
  idempotent per message id; a message superseded while queued
  is withdrawn and re-drafted, never sent stale. The fraud
  notice cannot leak by construction: its template has no slot
  for a signal, score or reason code.
- **R9 — Customer memory.** Law 7 completed: return history
  behind a Memory-shaped port with a labelled local double —
  event-shaped records derived in code at the four case
  milestones, retrieval by exact customer id only with every
  read on an access log, retention as pack data (730 days)
  with expiry as tombstone counts, never deletion. The fraud
  engine's memory path produces the identical content-addressed
  artefact the store path produces, graded case by case; the
  messaging verifier's figure universe extends to the case's
  own customer's memory records only.
- **R10 — The Postgres production profile + smoke.** The full
  suite runs on real PostgreSQL 16 via a conftest schema
  redirect (234 passed, 0 skipped on the R10 run); the eval
  gate runs every store profile with cross-profile
  artefact-id parity at zero differences; the production
  smoke (`make smoke`) drives the planted case through its
  whole lifecycle over HTTP in 21 asserted steps. Two dialect
  defects found and fixed: SQLite's thread-bound connections
  500'd every API store read under threaded serving (the local
  profile now opens with `check_same_thread=False`), and a
  `schema,public` search path let an unmigrated private schema
  silently resolve against a migrated public one (harness
  profiles use schema-only search paths).
- **R11 — The Gateway port, generalized, + the Policy pack.**
  Every tool-shaped operation — carrier tools, refund
  execution, the grading callback — flows through the one
  gateway; money moves only against a prepared execution
  record the handler re-verifies. A Cedar-shaped Policy pack,
  versioned data evaluated first-match-deny before every
  invocation; a denial is not an HTTP error — it returns as
  HTTP 200 with a JSON-RPC −32002 envelope, recorded with the
  denying policy's id. The gateway and policy implementations
  are local doubles, plainly labelled. The PG harness becomes
  hermetic: run-scoped schemas everywhere and a
  conflict-tolerant migration ledger, graded by double runs
  against one cluster.
- **R12 — The model gateway + cost ledger.** Every
  model-shaped call routes through one gateway: the offline
  profile byte-identical to the bare model port's output
  (graded), Bedrock-shaped and NIM-shaped profiles
  stub-tested only and fail-closed unbound — never a silent
  fallback. Responses enter only as Pydantic-validated
  structured outputs; a well-formed hosted response stating a
  wrong figure still meets the figure verifier. Every call is
  a cost-ledger artefact priced in code from a versioned price
  pack (unit prices quantise to the micro-unit — a defect the
  metering test caught and fixed in-round), with a per-case
  call budget whose refusals are recorded by name.
- **R13 — The operations console.** One static HTML file, no
  build step, served at `GET /console`: case board grouped by
  furthest lifecycle stage, artefact-chain detail, the review
  / approval / recovery queues acting only through the
  existing gated endpoints, money totals that are sums over
  receipt and adjustment artefacts (the console decides
  nothing and prices nothing), the gateway invocation log and
  the policy pack rendered from reads. `make console-check`
  executes the console's own JavaScript against a seeded store
  and proves the rendered totals equal the artefact sums.
- **R14 — The trap suite.** Adversarial fixtures across every
  law (`docs/trap-suite.md`), each landing as a test, an eval
  counter and a doc line: approval caps on the cent, a top-up
  settling exactly the graded delta, fraud band edges,
  grading timing and the matrix age edge, message staleness
  under adversarial timing, the $500 role cap at $500.00 /
  $500.01. Two engine defects found and fixed, traps kept as
  regressions: (1) a model note promising "a 20% discount"
  verified clean — the disclosure pack now names promotion
  vocabulary as forbidden content; (2) a superseded-but-approved
  computation satisfied the gateway's approval fact and died
  only at the handler — the gateway now requires the named
  computation to be the case's current one and denies at the
  gateway, as the policy's stated reason always claimed. Named
  honestly as open: the 94.99 fraud figure is unreachable by
  construction (whole-point contributions, graded as an
  integrality invariant), and wardrobing split across two
  accounts of one household scores per customer — the merged
  figure is an identity-resolution gap, not a score.
- **R15 — The injection quarantine.** Customer free text is
  evidence, not input: an insert-only quarantine store
  (migration 0011) receives every customer-controlled text
  byte-exact before any engine reads it; a versioned screening
  pack grades instruction shapes, PII shapes, link lures and
  oversized/encoded payloads. Text leaves only as a released
  view — automatic verbatim/masked for screened text,
  named-act escaped-only for instruction-risk text, with
  figures neutralised so a released "$999.99 refund approved"
  can never enter a verified message as a live figure.
  Downstream surfaces read views or name gaps; the console's
  quarantine queue carries metadata only, probe-tested.
- **R16 — Access control.** A versioned access pack states six
  roles' readable surfaces and act permissions as data.
  Principals are authenticated, never defaulted — missing,
  malformed or unknown-role principals are refused by name and
  recorded; customer scoping hides existence byte-for-byte
  (another customer's case answers exactly as a non-existent
  one on the API, the Runtime and the console); the auditor
  reads everything and every act attempt is refused
  `read_only_role` and recorded; acts bind their named actor
  to the calling principal; every refusal lands in an
  insert-only denial log (migration 0012). Stated scope:
  enforcement governs calls that carry a principal;
  principal-less calls remain the trusted-local operations
  path.
- **R17 — Management reporting.** A versioned report pack
  states each report's population rule, window rule and
  measures as data; five reports are insert-only artefacts
  (migration 0013) — return rate over the orders table (the
  denominator stated, never invented), disposition mix
  summing to its decided population exactly, fraud
  precision/recall over known outcomes only (unknown outcomes
  excluded and counted as excluded, never negatives — true
  negatives are exactly 0 on the planted record), a money
  summary graded equal to the console's own sums, and a
  cumulative cost summary. Every figure is re-performed by an
  independently written verifier; recomputation over moved
  inputs mints a superseding report, never an edit. Reports
  are a supervisor + auditor surface; customer probes are
  answered in the existence-hiding shape.
- **R18 — Carrier exception flows.** When the journey breaks:
  a versioned exception pack (detection windows 5/10 days,
  per-kind paths with stated consequences, the claim formula,
  the $250 settlement supervisor threshold, the transit
  disposition row, customer wording, blame terms). Exceptions
  are raised in code from the carrier event record — migration
  0014, insert-only, re-performable — and a named act chooses
  among the pack's paths for that kind, so a case never sits
  in limbo; while an exception stands open or escalated,
  refund execution is refused in code, and a customer-kept
  resolution refuses it permanently. **Transit attribution:**
  a carrier damage event plus a materially worse grade
  attributes the damage to the carrier — the refund is not
  re-priced against the customer, no adjustment opens, the
  fraud re-score does not fire, and the disposition resolves
  through the pack's transit row (`EX-TRANSIT-DAMAGE` →
  `RETURN_TO_VENDOR`); the same grade jump without the
  carrier's damage event is R7's cascade, unchanged. Carrier
  claims are assembled only from artefacts of record under an
  evidence fingerprint, settle as their own money-summary
  line, and never move the customer's totals.
- **R19 — The red team.** No new surface: the built system
  attacked as one system (`docs/red-team.md`), aimed at the
  R15–R18 surfaces and the seams where they compose with the
  money, fraud and messaging machinery. Nine defects found
  and fixed, each kept as a regression and a zero-tolerance
  `redteam_*` eval counter. The three HIGH money defects,
  named plainly: **RT-01** — the claim act fold ordered by
  caller-supplied date and content hash let a same-day settle
  sort before its acknowledgement, so a claim read unsettled
  while a settled act stood, the write door admitted a second
  settlement, and the money layer summed every settled act —
  the probe read $251.95 settled against a $151.95 claim;
  **RT-02** — the same fold let a claim settle after a
  back-dated denial the fold never honoured; **RT-03** —
  `execute_top_up` never called the carrier guard
  `execute_refund` honours, and paid out under an open
  exception. Also fixed: **RT-04** (MEDIUM) a damaged
  exception whose item graded unharmed could never resolve —
  permanent limbo, refund barred forever — now resolved as
  the derived state `grading_cleared`, with no claim opened;
  **RT-05** (MEDIUM) the messaging verifier never loaded the
  exception pack's blame vocabulary, so "your fault" and
  "you damaged" verified clean as model notes and were
  delivered — the verified universe now derives its forbidden
  terms from the pack; **RT-06** (LOW) claim filing judged
  the existence lookup before the caller's act, confirming
  exception existence to customers who may not file at all;
  **RT-07** (LOW) the Runtime's three R18 actions silently
  recorded a payload actor that was not the calling
  principal; **RT-08** (LOW) the access layer
  whitespace-stripped a carried principal, so
  `" staff_supervisor "` authenticated as the role — the pair
  is now judged exactly as carried; **RT-09** (LOW) claim
  verification accepted a binding to any computation in the
  chain — it names the current computation of record now.
  The attacks that held are recorded in the same doc, and the
  R14 named gaps were re-examined: 94.99 still unreachable by
  construction; the split-household link still open and still
  a data decision; opener/reviewer separation still absent
  from the scoring pack — there is nothing to enforce — while
  grading's stated receiver/grader separation is enforced and
  re-graded.
- **R20 — Release hardening.** Version 1.0.0; this changelog;
  `docs/release-1.0.0.md`; the README's final honest-status
  pass; a consistency sweep of README, architecture,
  trap-suite and red-team docs against the code as shipped
  (the Evaluations/Observability mapping row corrected to
  what was actually built, the Policy intro line brought up
  to date, the architecture's deploy-round and
  deploy-artefacts claims corrected, the round sections
  restored to build order); and the full freeze — every gate
  re-run on the finished tree, on the default and Postgres
  profiles alike.
