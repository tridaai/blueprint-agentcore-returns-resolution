<p><img src="https://trida.ai/logos-rounded/trida-rounded.png" width="96" alt="Trida AI"></p>

# Blueprint — Returns Resolution & Disposition Agent (AWS Bedrock AgentCore)

A post-purchase returns agent for a fictional outdoor retailer, **Cascade Outfitters**, built on **Amazon Bedrock AgentCore**. A return request becomes a governed case: eligibility is computed in code from a versioned policy pack, the model only drafts the customer-facing summary, and a figure verifier checks every amount and date in that draft against the computed decision before the summary may exist. The agent runs behind the AgentCore Runtime contract (`BedrockAgentCoreApp`, `POST /invocations`, `GET /ping`), with the rest of AgentCore — Memory, Gateway, Policy, Payments — mapped as ports that later rounds wire in.

> **Work in progress** — this blueprint is built and passing its automated test and eval gates, but it has not yet finished hands-on local testing, and its companion blog post is not yet published. Expect changes. This notice comes off when both are done.

## Why this shape

AWS's own retail demos stop at the shopping cart. Post-purchase — returns, refunds, disposition — is where the money, the fraud and the policy risk actually sit, and it is open ground. This blueprint takes the position a bank-grade blueprint would: **the model talks, the code decides.** Eligibility, windows and exclusions are a policy pack evaluated by a deterministic engine; the model's one job in R1 is drafting the customer summary, and even that draft is verified figure by figure before it is stored.

## The laws (enforced in code)

1. **Eligibility is policy-as-data** — return windows, category exclusions and condition rules live in a versioned policy pack (`2026.10.1`); every decision records the pack version and the exact clauses that fired, including the one-day edge: a case requested on its window's last day passes, one day later fails, both graded.
2. **The model never computes money** — amounts are Decimal math in code from order + policy inputs; a figure verifier rejects any amount or date in a drafted summary that does not equal a computed value. From R2 the money of record is the **refund computation**: a content-addressed artefact whose every line carries its derivation, whose missing inputs are named `not_computable` rather than zero-filled, and whose stored figures are re-performed and tamper-checked by a verifier.
3. **Disposition is a matrix, not an opinion** — where a returned item goes is read off a versioned matrix (category × declared condition × item age band → disposition + resale channel), never improvised. A closed channel falls to the matrix's stated next cell with the reroute recorded on the decision; a fact set with no cell is `not_decidable` naming the missing dimension, never a default guess (R3).
4. **Fraud signals are scored in code** — *arrives in R4.*
5. **Refunds are dual-threshold and idempotent** — approval gates and the payments port *arrive in R5*; in R1 **no money moves at all**.
6. **Carrier and label actions are tools, not prose** — *arrives in R6.*
7. **Customer history is memory with limits** — *the AgentCore Memory port arrives in R9.*
8. **Every customer-facing message is verified** — the figure verifier ships in R1 for the summary; the full messaging verifier is *R8*.

## What R1 builds

- **Domain core** — return-case and order models (money = Decimal, one rounding rule); the Cascade Outfitters catalogue and orders are gold-by-construction: every order total is provable from catalogue prices in tests.
- **Eligibility engine v1** — evaluates the versioned policy pack: return windows by category (apparel/footwear 60 days, gear 90, electronics 30), excluded categories (gift cards, consumables), final-sale exclusion, customer-damage condition bar. Decisions carry the clause list an auditor would replay.
- **Intake port + reference feed** — eight planted requests against the seed orders, including the window edge pair (REQ-2003 inside by zero days, REQ-2004 outside by one). Ingestion is idempotent by request id.
- **Store port** — SQLite local default (`./data/returns.db`), Postgres dialect behind `DATABASE_URL`; numbered migrations with a runner (migration 0001); photo/receipt evidence behind an S3-shaped store port with SHA-256 recorded at put time and re-verified on read.
- **AgentCore Runtime shape** — the entrypoint uses the pinned `bedrock-agentcore` SDK's `BedrockAgentCoreApp` (see Honest status), plus a local harness (`make harness`) that starts the real server and drives `/ping` + `/invocations` over HTTP, and a demo (`make demo`) walking the eligible and ineligible edge cases end to end.
- **The agent step** — behind a model port (offline deterministic default; `hosted-bedrock` profile fail-closed until a model ARN is configured), the agent assembles the customer-facing return summary from the computed decision. The figure verifier runs over the final text: a wrong-figure probe fails in tests and in the eval gate.
- **API** — FastAPI surface for intake, cases, decisions and orders, sharing the one `CaseService` the Runtime entrypoint uses.

## What R2 adds — the refund computation engine

- **A Decimal ledger, line by line** — from the eligibility decision, the order as stored and the policy pack's refund schedule (restocking rates by declared condition: new-with-tags 0%, like-new 10%, used 20%; free return shipping for apparel and footwear; a flat $6.95 return-shipping deduction otherwise), the engine computes item credit, restocking fee and shipping deduction, each a named line carrying its derivation: pinned inputs, the rule, the policy reference.
- **Content-addressed and tamper-checked** — the computation's id hashes the decision + order fingerprints, pack version and engine version; `verify_computation` re-performs the arithmetic from the pinned inputs and detects any altered line, total or allocation (graded in tests and the eval gate).
- **Not computable is a stated state, never a zero** — a declared condition with no rate in the pack makes the fee `not_computable` naming the missing rate, the refund total stays open (`is_final` false) and the customer summary says exactly what it is waiting on. An order with no tender breakdown gets a final total but a named, missing tender allocation — where the money goes is not invented.
- **The summary quotes the computation** — the customer-facing refund is now the computed `refund_total` (the decision's `refund_due` remains the pre-fee eligibility figure), and the figure verifier's allowed universe extends to the computation's lines, totals and allocations: sayable figures are still a closed computed set.
- **Surfaces** — migration 0002 (`computations` table), `GET /cases/{id}/computation` (+ `/verify`, `GET /computations`), Runtime dispatch actions `get_computation` / `verify_computation`, and the demo prints the full breakdown. Hand-computed fixtures (multi-line order, fee-bearing categories, a shipping-deducted case) are exact to the cent in tests and evals.

## What R3 adds — the disposition matrix

- **A matrix, as data** — the disposition pack (`2026.10.1`) holds 36 primary cells over (category × declared condition × age band: fresh 0–30 days since delivery, season 31–90, aged 91+) plus stated fallback cells. Each cell names a disposition from a closed vocabulary — RESTOCK, REFURBISH, OUTLET, LIQUIDATE, RETURN_TO_VENDOR, RECYCLE, HOLD_FOR_INSPECTION — and the resale channel it flows through. New-with-tags resells at full price while current; fashion categories stale into the outlet with age while gear barely stales; used items are refurbished young and liquidated old; aged sealed electronics go back to the vendor.
- **Channel availability is data too, and reroutes are on the record** — a separate versioned channel table states which channels are open per category. The shipped table closes the apparel liquidator, so aged used apparel falls to its cell's stated next cell (RECYCLE via the recycler) and the decision records the hop: from-cell, the closed channel, to-cell. No decision ever lands on a non-primary cell without its reroute path attached.
- **Not decidable is a stated outcome** — an unknown category, a declared condition outside the matrix vocabulary, an age that bands nowhere, or a closed channel with no stated next cell returns a stored `not_decidable` artefact naming the missing dimension. There is no default disposition anywhere in the engine.
- **Content-addressed, tamper-checked, basis-labelled** — each decision is pinned to the case, the refund computation it was decided alongside, the matrix and channel-table versions (plus the table's content fingerprint) and the exact facts evaluated; `verify_disposition` re-performs the walk and detects any altered disposition, cell or reroute. Every R3 decision carries `basis: declared` — it rests on the customer's declared condition, and warehouse grading (R7) will supersede it with a new linked decision rather than editing it.
- **Holds name their unknowns** — used electronics are HOLD_FOR_INSPECTION at any young age, and the decision names exactly what inspection must establish (functional test result, data-wipe status) for a later round to consume.
- **Surfaces** — migration 0003 (`dispositions` table), `GET /cases/{id}/disposition` (+ `/verify`, `GET /dispositions`), matrix inspection reads (`GET /disposition/matrix`, `/disposition/matrix/cells/{cell_id}`, `/disposition/channels`), Runtime dispatch actions `get_disposition` / `verify_disposition` / `get_matrix` / `get_matrix_cell` / `get_channels` / `list_dispositions`, and the demo prints each case's disposition. Only eligible cases receive a decision; denied cases carry none. The eval gate sweeps all 36 rows against a hand-derived map with zero-tolerance counters for wrong dispositions, unexercised rows, undecidable-but-decided facts and silent reroutes.

## AgentCore mapping

| AgentCore component | This blueprint | Round |
|---|---|---|
| Runtime | `BedrockAgentCoreApp` entrypoint; one session per return case | R1 |
| Memory | Customer return history behind a memory port (local double) | R9 |
| Gateway | OMS / inventory / warehouse / carrier APIs as MCP-shaped tool ports | R11 |
| Policy | Cedar cap pack in `infra/` enforcing refund caps per role at the Gateway | R11 |
| Payments | Refund execution behind a payments port (AgentCore Payments-shaped); demo refunds run against a payments double only | R5 |
| Identity | Customer (3LO) vs staff (2LO) flows separated at the port | R16 |
| Evaluations + Observability | Evaluator configs in `infra/`; OTel-shaped spans per case step | R20 |

## Quickstart

```bash
make install        # uv sync (or: make install-pip)
make demo           # eligible + ineligible returns, end to end
make harness        # the Runtime HTTP contract, exercised locally
make test           # pytest
make evals          # the eval gate
```

Run the Runtime server itself with `make serve` (port 8080), or the API
with `uv run uvicorn returns_resolution.api:create_app --factory` after
wiring a service — the demo and harness cover the common paths.

Configuration is environment-only (see `.env.example`): AWS/Bedrock
profile first, offline deterministic model profile by default, SQLite
unless `DATABASE_URL` points at Postgres. `docker compose up` brings
the agent, Postgres+pgvector and the SeaweedFS S3 gateway together.
The Dockerfile is multi-stage and non-root; AgentCore Runtime deploys
want an ARM64 image (`docker buildx build --platform linux/arm64`).

## Honest status

- **Built and gated in R1:** domain core, policy pack + eligibility engine, intake feed, store + migration 0001, evidence port, Runtime entrypoint, model port + figure verifier, API, demo, harness, eval gate.
- **Built and gated in R2:** refund computation engine (Decimal lines with derivations, content-addressed artefacts, re-performance/tamper verification, not-computable naming, tender allocation where tender data exists), migration 0002, computation reads on API + Runtime dispatch, summary/verifier consuming the computation.
- **Built and gated in R3:** the disposition matrix (versioned matrix pack + channel table as data, pure engine, content-addressed decisions pinned to case + computation + both pack versions, reroutes recorded hop by hop, `not_decidable` naming the missing dimension, HOLD_FOR_INSPECTION unknowns named, re-performance/tamper verification), migration 0003, disposition + matrix-inspection reads on API + Runtime dispatch, demo disposition printout, full-matrix eval sweep. Decisions rest on the customer's *declared* condition (`basis: declared`) — warehouse grading and supersession arrive in R7.
- **The AgentCore SDK is real here:** `bedrock-agentcore==1.25.1` installs in this environment and the entrypoint is the SDK's `BedrockAgentCoreApp`. If the SDK ever cannot be imported, the same module serves the identical HTTP contract (`POST /invocations`, `GET /ping`) via a fallback app — the container's external shape does not change.
- **Live AWS is stub-tested until an account is wired:** no AWS account is connected in this environment, so nothing here has called Bedrock, AgentCore Runtime, Memory, Gateway, Policy or Payments live. The `hosted-bedrock` model profile refuses loudly until `RETURNS_BEDROCK_MODEL_ARN` is configured — it never silently falls back to the offline composer.
- **No money moves in R1.** Fraud scoring is R4, approval gates + the idempotent payments port R5, carrier tools R6, warehouse grading R7, the messaging verifier R8, the Memory port R9, Gateway/Policy ports R11. Demo refunds, when they arrive in R5, execute against a payments double only.
- **Docker image build was not run** in the build sandbox (no daemon); the Dockerfile and compose file follow the portfolio's verified pattern and are reviewed statically.

## Layout

```
src/returns_resolution/
  models.py        domain models (Decimal money)
  seed.py          Cascade Outfitters catalogue + orders (gold by construction)
  policy.py        the versioned policy pack (data + fail-closed loader)
  eligibility.py   the eligibility engine
  refunds.py       the refund computation engine (R2)
  disposition.py   the disposition matrix pack + channel table + engine (R3)
  intake.py        intake port + planted reference feed
  store.py         store port: SQLite / Postgres, migrations 0001–0003 + runner
  evidence.py      S3-shaped evidence store port (local double)
  modelport.py     model port: offline default, hosted-bedrock fail-closed
  verifier.py      the figure verifier
  service.py       the one workflow every surface shares
  runtime_app.py   AgentCore Runtime entrypoint (+ contract fallback)
  api.py           FastAPI surface
scripts/           demo.py, local_harness.py
evals/             the eval gate
docs/              architecture.md
```

Apache 2.0. A Trida AI blueprint — patterns are the point; the fictional retailer's data is ours, generated for this repo.
