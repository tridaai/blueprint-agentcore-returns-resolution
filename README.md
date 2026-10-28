<p><img src="https://trida.ai/logos-rounded/trida-rounded.png" width="96" alt="Trida AI"></p>

# Blueprint — Returns Resolution & Disposition Agent (AWS Bedrock AgentCore)

A post-purchase returns agent for a fictional outdoor retailer, **Cascade Outfitters**, built on **Amazon Bedrock AgentCore**. A return request becomes a governed case: eligibility is computed in code from a versioned policy pack, the model only drafts the customer-facing summary, and a figure verifier checks every amount and date in that draft against the computed decision before the summary may exist. The agent runs behind the AgentCore Runtime contract (`BedrockAgentCoreApp`, `POST /invocations`, `GET /ping`), with the rest of AgentCore — Memory, Gateway, Policy, Payments — mapped as ports that later rounds wire in.

> **Work in progress** — this blueprint is built and passing its automated test and eval gates, but it has not yet finished hands-on local testing, and its companion blog post is not yet published. Expect changes. This notice comes off when both are done.

## Why this shape

AWS's own retail demos stop at the shopping cart. Post-purchase — returns, refunds, disposition — is where the money, the fraud and the policy risk actually sit, and it is open ground. This blueprint takes the position a bank-grade blueprint would: **the model talks, the code decides.** Eligibility, windows and exclusions are a policy pack evaluated by a deterministic engine; the model's one job in R1 is drafting the customer summary, and even that draft is verified figure by figure before it is stored.

## The laws (enforced in code)

1. **Eligibility is policy-as-data** — return windows, category exclusions and condition rules live in a versioned policy pack (`2026.10.1`); every decision records the pack version and the exact clauses that fired, including the one-day edge: a case requested on its window's last day passes, one day later fails, both graded.
2. **The model never computes money** — amounts are Decimal math in code from order + policy inputs; a figure verifier rejects any amount or date in a drafted summary that does not equal the computed decision's values.
3. **Disposition is a matrix, not an opinion** — *arrives in R3.*
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
- **The AgentCore SDK is real here:** `bedrock-agentcore==1.25.1` installs in this environment and the entrypoint is the SDK's `BedrockAgentCoreApp`. If the SDK ever cannot be imported, the same module serves the identical HTTP contract (`POST /invocations`, `GET /ping`) via a fallback app — the container's external shape does not change.
- **Live AWS is stub-tested until an account is wired:** no AWS account is connected in this environment, so nothing here has called Bedrock, AgentCore Runtime, Memory, Gateway, Policy or Payments live. The `hosted-bedrock` model profile refuses loudly until `RETURNS_BEDROCK_MODEL_ARN` is configured — it never silently falls back to the offline composer.
- **No money moves in R1.** The disposition matrix is R3, fraud scoring R4, approval gates + the idempotent payments port R5, carrier tools R6, warehouse grading R7, the messaging verifier R8, the Memory port R9, Gateway/Policy ports R11. Demo refunds, when they arrive in R5, execute against a payments double only.
- **Docker image build was not run** in the build sandbox (no daemon); the Dockerfile and compose file follow the portfolio's verified pattern and are reviewed statically.

## Layout

```
src/returns_resolution/
  models.py        domain models (Decimal money)
  seed.py          Cascade Outfitters catalogue + orders (gold by construction)
  policy.py        the versioned policy pack (data + fail-closed loader)
  eligibility.py   the eligibility engine
  intake.py        intake port + planted reference feed
  store.py         store port: SQLite / Postgres, migration 0001 + runner
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
