# Provenant

**A trust layer for AI agents that spend money.**

Provenant makes agent checkout resistant to prompt injection by tracing every field of a PayPal
order back to the user's signed intent or the merchant's signed catalog. When a purchase still
goes wrong, it works out who caused it (user, merchant or agent) and moves the money accordingly
on PayPal rails: void, partial refund, liability payout, or a dispute evidence pack.

Built for the PayPal AI Hackathon 2026.

> **Status: Phase 0 (PayPal behavior spikes).** Everything in "What works today" below is
> implemented and proven against the real PayPal sandbox. The Lineage and Blackbox subsystems are
> designed (see [`CLAUDE.md`](CLAUDE.md)) and land in Phases 1 to 3. Progress and raw results:
> [`docs/progress.md`](docs/progress.md).

---

## The problem

Shopping agents read untrusted web content (product pages, reviews, promo banners) and then
create payments. Two things are unsolved:

1. **Hijacking.** A poisoned review can tell the agent to swap the payee, inflate the price, bump
   the quantity or change the shipping address. Published 2026 benchmarks put prompt injection
   success against shopping agents at roughly 40 to 70 percent.
2. **Liability.** When an agent buys the wrong thing, there is no rule for who pays: the person
   who asked, the merchant whose listing misled the agent, or the company running the agent.

## How Provenant works

Two subsystems share one spine.

```
   user request ----> Buyer App (intent capture, approvals, provenance graph, complaints)
                                   |
                     +-------------v--------------+
                     |  Provenant API             |
                     |                            |
                     |  LINEAGE (prevent)         |
                     |   signed Intent Mandate    |
                     |   planner sees no web text |
                     |   quarantined extractor    |
                     |   label propagation        |
                     |   field contract checker   |
                     |                            |
                     |  BLACKBOX (recover)        |
                     |   counterfactual replay    |
                     |   exact Shapley attribution|
                     |   remedy router            |
                     |                            |
                     |  FLIGHT RECORDER           |
                     |   hash-chained event log   |
                     +---+----------+---------+---+
                         |          |         |
               merchant sims    Postgres   PayPal sandbox
               (signed          (recorder, (Orders, Payments,
                manifests)       ledger)    Payouts, Disputes,
                                            Webhooks)
```

- **Lineage** labels every value by where it came from (`USER`, `MERCHANT_SIGNED`, `DERIVED`,
  `UNTRUSTED`). An order is created only if the payee, price, item and quantity trace back to the
  user's signed mandate or the merchant's signed manifest. Untrusted page content can never bind
  an authority-bearing field.
- **Blackbox** replays a disputed purchase under counterfactual interventions on each party and
  computes exact Shapley fault shares with bootstrap confidence intervals. Deterministic code
  routes the remedy; an LLM only narrates the numbers.
- **Flight Recorder** binds the money to the decision: the session hash goes into the PayPal
  order's `custom_id`, so any order, authorization or capture can be traced to its full decision
  trace.

LLMs plan, extract and narrate. They never decide whether a payment is allowed and never compute
fault shares.

## What works today

All proven against `api-m.sandbox.paypal.com`, with redacted request and response logs.

| Spike | What it proves | Result |
|---|---|---|
| S0 | OAuth for all 5 REST apps (4 merchants, 1 operator) and their scopes | PASS |
| S1 | Create, buyer approve, authorize and capture an `AUTHORIZE` order; `custom_id` and `invoice_id` survive order, authorization and capture | PASS 14/14 |
| S2 | Void an authorization; repeat a partial refund 3 times with one `PayPal-Request-Id` and get exactly one refund | PASS |
| S3 | Liability payout from the operator account to two buyers; our ledger refuses a duplicate payout without calling PayPal | PASS 8/8 |
| S7 | Negative testing with `PayPal-Mock-Response` on create, authorize, capture and refund; failures never corrupt the ledger or the resource | PASS 35/35 |
| S8 | JS SDK v6 button approves a server-created `AUTHORIZE` order; authorized and verified server-side | PASS 8/8 |
| S4, S5, S6 | Vaulted autonomous checkout, webhooks over a cloudflared tunnel, dispute lifecycle | Next |

### Payment safety built in from day one

- **Idempotency is enforced by our own ledger** (`paypal/ledger.py`), not inferred from PayPal
  status codes. Every POST is persisted with its operation key, `PayPal-Request-Id` and resulting
  resource id before and after sending. A succeeded operation is never sent again, an unknown
  outcome is retried with the same request id, and failures are recorded with their issue code.
- **Any 2xx is success.** No code branches on 200 vs 201; state is read from the resource and
  verified with a GET after every state change. Every POST asks for the full resource with
  `Prefer: return=representation`.
- **No secrets in logs or git.** Tokens, client secrets, payer emails and names are redacted
  before anything is printed or stored. gitleaks runs as a pre-commit hook with extra rules for
  PayPal client ids, secrets and access tokens.

## How PayPal is used

| Endpoint | Purpose | Proven in |
|---|---|---|
| `POST /v1/oauth2/token` | Server token per REST app; browser-safe client token for JS SDK v6 | S0, S8 |
| `POST /v2/checkout/orders` | `AUTHORIZE` order with `custom_id` (decision trace hash) and `invoice_id` | S1 |
| `POST /v2/checkout/orders/{id}/authorize` | Place the hold after buyer approval | S1, S2 |
| `GET /v2/checkout/orders/{id}` | Verify state after every change | S1, S2 |
| `POST /v2/payments/authorizations/{id}/capture` | Capture on fulfillment | S1, S2 |
| `POST /v2/payments/authorizations/{id}/void` | Remedy: cancel before capture | S2 |
| `POST /v2/payments/captures/{id}/refund` | Remedy: full or partial merchant refund | S2 |
| `POST /v1/payments/payouts`, `GET /v1/payments/payouts/{id}` | Remedy: agent liability payout to the buyer | S3 |
| `PayPal-Mock-Response` header | Failure-path testing | S7 |
| JS SDK v6 (`web-sdk/v6/core`) | Buyer approval in the Buyer App | S8 |
| Webhooks, vault, Disputes API | Event processing, autonomous mode, evidence upload | S4 to S6 (next) |

## How AI is used (Phases 1 to 3)

Each role's model is set by environment variable; nothing is hardcoded.

| Role | Sees | Decides |
|---|---|---|
| Mandate extractor | Only the user's own words | Proposes a structured mandate; the user confirms and signs it |
| Planner (P-LLM) | The confirmed mandate and tool signatures, never web content | Emits a plan in a small JSON DSL |
| Quarantined extractor (Q-LLM) | Untrusted page text, no tools | Schema-validated JSON, always labeled `UNTRUSTED` |
| Vision verifier | Buyer's photo of the delivered item | Structured observations the buyer confirms |
| Narrator | Computed attribution numbers only | Plain-language verdict; may not change any number |

## Getting started

### Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- A PayPal Developer account with sandbox accounts and REST apps, set up as described in
  [`scripts/setup_sandbox.md`](scripts/setup_sandbox.md)

### Install

```bash
git clone https://github.com/bytes06runner/Provenant.git
cd Provenant
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/bin/pre-commit install
cp .env.example .env   # then fill in your sandbox credentials
```

### Run the tests

```bash
.venv/bin/pytest          # unit tests, no network
.venv/bin/ruff check .
.venv/bin/mypy
```

### Run the spikes against the sandbox

```bash
.venv/bin/python spikes/s0_oauth_check.py
.venv/bin/python spikes/s1_order_authorize_capture.py --open
.venv/bin/python spikes/s2_void_and_idempotent_refund.py --open
.venv/bin/python spikes/s3_payouts.py
.venv/bin/python spikes/s7_negative_testing.py --authorization-id <voided auth> --capture-id <capture>
.venv/bin/python spikes/s8_jssdk/server.py   # then open http://localhost:8708
```

Spikes that need buyer approval print a link (or open it with `--open`). Log in as a sandbox
Personal account and approve; the spike continues on its own. Redacted logs are written to
`spikes/out/`, which is gitignored.

## Repository layout

```
paypal/       PayPal REST client, request ledger, redaction        (built)
spikes/       Phase 0 sandbox proofs                               (built)
config/       app, merchant and spike configuration                (built)
lineage/      mandate, manifests, labels, DSL, interpreter, contracts  (Phase 1)
blackbox/     recorder, intake, facts, replay, attribution, remedy     (Phase 2)
merchants/    storefront simulators incl. an attacker shop             (Phase 1)
baseline/     conventional Agent Toolkit agent for comparison          (Phase 3)
lab/, eval/   attack dataset and evaluation harness                    (Phase 3)
web/          Buyer App and Ops Console                                (Phase 3)
docs/         progress log, threat model, demo script
```

## Limitations (current and by design)

- Phase 0 only: no agent, recorder or attribution code exists yet.
- Lineage guarantees a purchase satisfies the user's mandate, not that it is the best choice.
  Untrusted reviews can still influence which compliant item is picked; Blackbox exists to
  handle that residual risk.
- Some `PayPal-Mock-Response` codes are not supported on some endpoints (PayPal returns 403 with
  an empty body). The supported list is in `docs/progress.md`.
- Sandbox only. Nothing here touches live PayPal.

## Tools used

- **PayPal REST APIs and JS SDK v6** (sandbox), see the table above
- **Claude** (Claude Code) for implementation; Anthropic models for the agent roles from Phase 1
- **gitleaks** and **pre-commit** for secret scanning, **ruff**, **mypy** and **pytest** for quality
- **GitHub Actions** for CI

Planned: PayPal Agent Toolkit (baseline agent), AG Grid (Ops Console), Render (hosting).

## License

[Apache-2.0](LICENSE)
