# Release notes — 1.0.0

Returns Resolution & Disposition Agent on AWS Bedrock AgentCore
(Trida AI Blueprint #18), for the fictional retailer Cascade
Outfitters. This is the frozen twenty-round build: every gate
re-run on the finished tree at release (see CHANGELOG.md for
the round-by-round account and the README's Honest status for
the per-round evidence).

## What a deployer gets

A governed post-purchase returns agent. A return request
becomes a case; eligibility, refund figures, disposition,
fraud scoring, approvals and carrier exceptions are computed
in code from versioned packs — the model drafts customer
text, and a verifier checks every figure, fact and forbidden
term in that draft before it may exist as a message. In the
box:

- The full case lifecycle: intake → eligibility → refund
  computation → disposition → fraud score and review →
  approval ladder → idempotent refund execution → carrier
  label and tracking → warehouse receipt and grading with
  the supersession cascade (re-price, adjustments, top-ups)
  → customer messaging → carrier exceptions and claims →
  management reporting.
- Access control over every surface: six roles from a
  versioned pack, customer scoping with existence hiding,
  a read-only auditor, and an insert-only denial log.
- The operations console: one static HTML file at
  `GET /console` — case board, artefact-chain detail,
  review / approval / recovery queues, money sums over the
  artefacts of record.
- The gates that grade it all: the pytest suite, an eval
  gate with zero-tolerance counters and cross-profile
  parity, a production smoke over HTTP, a console
  correctness check, and a demo that walks the planted
  record end to end.

## How to run it

```bash
make install        # uv sync --extra dev (pinned, frozen constraints)
make demo           # the planted record walked end to end
make test           # pytest (SQLite / in-memory by default)
make evals          # the eval gate, every store profile
make smoke          # production smoke: the full lifecycle over HTTP
make console-check  # the console's rendered sums vs the artefacts
make serve          # the AgentCore Runtime entrypoint (port 8080)
```

No `uv`? `make install-pip` is the two-step fallback: install
the frozen `constraints.txt`, then the project with
`--no-deps -e .`.

**Postgres:** set `DATABASE_URL` (e.g.
`postgresql://postgres@host:5432/returns`) and the same code
runs on PostgreSQL 16 — payloads stay JSON text in both
dialects, migrations 0001–0014 apply through the store's
runner. For the test suite's Postgres profile, set
`RETURNS_TEST_DATABASE_URL` (or `DATABASE_URL`) before
`make test`; without it those legs skip by design. The eval
gate and the smoke pick Postgres up from `DATABASE_URL` the
same way.

**The console:** start the API
(`uv run uvicorn returns_resolution.api:create_app --factory`
after wiring a service — `scripts/demo.py` and
`scripts/local_harness.py` show the wiring) and open
`/console`. It reads a stated operator name and role in the
page and acts only through the gated endpoints.

**Configuration** is environment-only; see `.env.example`.
The offline deterministic model profile is the default; the
hosted profiles (Bedrock-shaped, NIM-shaped) are fail-closed
until configured — they never fall back silently.

`docker compose up` brings up the agent, Postgres+pgvector
and the SeaweedFS S3 gateway in the portfolio's standard
shape; the Dockerfile is multi-stage and non-root, and
AgentCore Runtime deploys want an ARM64 image
(`docker buildx build --platform linux/arm64`).

## The honest limits

- **No real money, parcels or messages move.** Payments,
  carrier, messaging, memory, gateway and policy are local
  doubles behind their ports, labelled on every artefact
  they produce. Production binds the real providers —
  AgentCore Payments, the retailer's carrier integration
  behind the Gateway port, its messaging provider — behind
  the same ports; none is connected here, and no live AWS
  call of any kind has been made from this build.
- **The hosted model profiles are stub-tested only.** The
  offline profile is the graded path; a configured hosted
  profile with no transport bound fails closed and says so.
- **Access enforcement governs calls that carry a
  principal.** Principal-less calls remain the trusted-local
  operations path they have been since R1; in production,
  AgentCore Identity terminates caller identity in front of
  these surfaces and every call carries one.
- **Two named gaps stay open.** The split-household link:
  wardrobing split across two accounts of one household
  scores per customer, because no artefact of record links
  the accounts — closing it is a data decision for the
  retailer. Opener/reviewer separation in fraud review: the
  scoring pack states no such rule, so there is nothing to
  enforce (grading's receiver/grader separation, which the
  pack does state, is enforced).
- **The Docker image build is reviewed statically**, not
  run — the build sandbox has no Docker daemon.
- **The README's work-in-progress notice stays on.** The
  automated gates are green; hands-on developer testing and
  the companion blog post are still outstanding, and the
  notice comes off when both are done — not before.
