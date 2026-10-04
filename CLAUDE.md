# PROVENANT: Build Brief for Claude Code

> Save this file as `CLAUDE.md` in the repo root. It is the single source of truth for this project. Re-read it at the start of every session. If something in the code contradicts this file, stop and ask before continuing.

---

## 0. Mission

We are building **Provenant** for the PayPal AI Hackathon 2026 (Devpost, deadline Nov 12, 2026, 12:00 PM PST).

**One line:** Provenant is a trust layer for AI agents that spend money. It makes agent checkout impossible to hijack through prompt injection, and when a purchase still goes wrong, it works out *who caused it* and moves the money accordingly through PayPal.

Two subsystems share one spine:

1. **Lineage (prevent):** every field of a PayPal order (payee, amount, item, quantity, shipping address) carries cryptographic and data-flow provenance. An order is only created if each field traces back to the user's signed intent or the merchant's signed catalog. Untrusted web content can never bind an authority-bearing field.
2. **Blackbox (recover):** a flight recorder captures every step of every agent purchase. When the user reports a problem, Blackbox replays the decision under counterfactual interventions, computes a causal attribution of fault across **user, merchant, and agent**, then executes the remedy on PayPal rails: void, partial refund, liability payout, or a dispute evidence pack, split in proportion to the attribution.
3. **Flight Recorder (spine):** an append-only, hash-chained log shared by both. Lineage writes it, Blackbox reads and replays it.

**Why this wins:** agent liability is an openly unsolved industry problem (no rule currently assigns fault among consumer, AI provider, and merchant for agent purchases), and prompt injection on shopping agents is measured at roughly 40 to 70 percent attack success in 2026 benchmarks. Research prototypes exist for each half (CaMeL / PACT for provenance, Causal Agent Replay for attribution). Nobody has connected them to real money movement. We do.

---

## 1. Non-negotiable ground rules

1. **Nothing hardcoded.** No hardcoded IDs, prices, credentials, model names, merchant data, LLM outputs, or demo results. All config comes from environment variables or YAML under `config/`. Demo data is produced by seed scripts and generators, never inlined in application code.
2. **No fake runtime.** Runtime code paths never return mocked PayPal or LLM responses. Every PayPal call hits the real sandbox (`https://api-m.sandbox.paypal.com`). Every LLM call hits a real model. Unit tests may use recorded fixtures; integration tests hit the real sandbox.
3. **Verify, don't assume.** Before building on any PayPal behavior, prove it with a spike script in `spikes/` (see Section 13). If a spike fails, report back with the error and options. Do not silently work around it.
4. **Deterministic security core.** Policy checks, contract checks, label propagation, signature verification, and attribution math are plain deterministic code. LLMs plan, extract, and narrate. LLMs never decide whether a payment is allowed and never compute the attribution numbers.
5. **Use the PayPal AI-Toolkit plugin** (`github.com/paypal/AI-Toolkit`, loaded with `claude --plugin-dir`). Consult its `paypal-best-practices` skill before writing any PayPal integration code. Use `/paypal:explain-error` on any PayPal error.
6. **Small, verified steps.** Work phase by phase (Section 12). At the end of each phase, run the acceptance checks, commit, and write a short status note in `docs/progress.md`. Ask me before starting the next phase.
7. **Secrets** live only in `.env` (gitignored). Ship `.env.example` with every key documented. Never log tokens, client secrets, or full payer emails.
8. **Copy style.** All user-facing text (UI, README, demo script) should read human and professional. No em dashes anywhere.

---

## 2. Actors and sandbox accounts

| Actor | What it is | PayPal sandbox setup |
|---|---|---|
| **User (buyer)** | Person delegating shopping to the agent | 2 sandbox Personal accounts (`buyer_a`, `buyer_b`) |
| **Merchants** | 3 demo storefronts with distinct behavior profiles | 3 sandbox Business accounts, each with its own REST app (client id/secret) |
| **Attacker merchant** | Storefront that tries to hijack agents | 1 sandbox Business account + REST app |
| **Provenant operator** | Runs the agent, holds the agent liability pool | 1 sandbox Business account + REST app, funded for Payouts |

Merchants "onboard" to Provenant by registering their sandbox REST credentials and a signing public key (this mirrors how a commerce platform app is granted API access by a merchant). Provenant uses merchant credentials only to create/capture/refund orders *within the limits that merchant signed in its policy* (Section 4.2).

Account creation is manual in the PayPal Developer Dashboard. Write `scripts/setup_sandbox.md` documenting exactly which accounts to create, and `scripts/register_merchants.py` that reads credentials from `.env` and registers them.

---

## 3. Architecture

```
                         +-------------------------------+
   user request  ---->   |  Buyer App (Next.js)          |
                         |  intent capture, approvals,   |
                         |  provenance graph, complaints |
                         +---------------+---------------+
                                         |
                         +---------------v---------------+
                         |  Provenant API (FastAPI)      |
                         |                               |
                         |  LINEAGE                      |
                         |   - Intent Mandate service    |
                         |   - P-LLM planner (trusted)   |
                         |   - Plan interpreter + labels |
                         |   - Q-LLM extractor (no tools)|
                         |   - Field contract checker    |
                         |   - PayPal order builder      |
                         |                               |
                         |  BLACKBOX                     |
                         |   - Complaint intake          |
                         |   - Fulfillment verifier      |
                         |   - Replay engine             |
                         |   - Attribution (Shapley)     |
                         |   - Remedy router             |
                         |   - Evidence pack builder     |
                         |                               |
                         |  FLIGHT RECORDER (hash chain) |
                         +---+-----------+-----------+---+
                             |           |           |
              +--------------v--+  +-----v------+  +-v------------------+
              | Merchant sims   |  | Postgres   |  | PayPal Sandbox     |
              | (3 honest-ish + |  | recorder,  |  | Orders v2, Auth,   |
              |  1 attacker)    |  | ledger     |  | Refunds, Payouts,  |
              | signed manifests|  |            |  | Disputes, Webhooks |
              +-----------------+  +------------+  +--------------------+
```

Plus an **Ops Console** (inside the same Next.js app, `/console`) for the Recourse queue, ledger, and evaluation dashboards, built with **AG Grid**.

---

## 4. Core concepts (implement these exactly)

### 4.1 Intent Mandate (user side of trust)

- User types a natural-language request ("Buy me black trail running shoes, US size 10, under $120, deliver home").
- The **Mandate Extractor** (LLM, sees only the user's own text) proposes a structured mandate:
  `{ category, required_attributes{}, forbidden_attributes{}, max_unit_price, max_total, currency, quantity, merchant_allowlist|null, ship_to_ref, expires_at, autonomy_mode }`
- The UI shows the mandate as editable fields. **The user confirms or edits.** Ambiguities the extractor detects (e.g. size system) must be surfaced as explicit questions, never silently resolved.
- On confirm, the mandate is canonicalized (RFC 8785 JCS), hashed (SHA-256), and signed with the user's Ed25519 key (server-held per-user key for MVP; WebAuthn passkey signing is a stretch goal). Every field in the confirmed mandate carries the label `USER`.

### 4.2 Signed Merchant Manifest (merchant side of trust)

- Each merchant publishes `/.well-known/provenant-manifest.json` containing: merchant identity, PayPal `merchant_id` / payee email, product catalog (sku, title, attributes, price, currency, stock), and a **machine-readable policy** (return window, refund caps by fault type, restocking fee, whether partial refunds are pre-authorized).
- The manifest is JCS-canonicalized and signed with the merchant's Ed25519 key. Public keys are registered at onboarding.
- Values read from a verified manifest get the label `MERCHANT_SIGNED(merchant_id, manifest_hash)`.
- Signatures make merchant claims **non-repudiable**. If a merchant signs a false attribute, Blackbox can prove the merchant claimed it.
- Product **pages** (HTML, reviews, descriptions, promo banners) are separate from the manifest and are always `UNTRUSTED(url, content_hash)`.

### 4.3 Labels and propagation

Label lattice (most to least trusted): `USER` > `MERCHANT_SIGNED` > `DERIVED` > `UNTRUSTED`.
- Every runtime value is a `Labeled[T]` carrying `{value, label, sources[]}`.
- Any operation over labeled values produces a value whose label is the **join** (least trusted) of its inputs and whose `sources` is the union.
- Labels can never be upgraded by the LLM. Only signature verification (to `MERCHANT_SIGNED`) or user confirmation (to `USER`) can raise trust.

### 4.4 Field contracts (the heart of Lineage)

Before any PayPal order is created, the **Contract Checker** evaluates the proposed checkout. Each authority-bearing field has a contract:

| Order field | Allowed provenance | Additional deterministic check |
|---|---|---|
| `payee` | `MERCHANT_SIGNED` from the selected merchant's verified manifest | merchant in mandate allowlist if set |
| `item.sku` | `MERCHANT_SIGNED` | sku attributes satisfy mandate `required_attributes`, violate no `forbidden_attributes` |
| `unit_price` | `MERCHANT_SIGNED` | `<= max_unit_price` |
| `quantity` | `USER` | equals mandate quantity |
| `amount.total` | `DERIVED` only from `MERCHANT_SIGNED` price x `USER` quantity (+ signed shipping/tax) | `<= max_total` |
| `shipping_address` | `USER` (from user vault) | matches `ship_to_ref` |
| `currency` | `USER` or `MERCHANT_SIGNED` | must agree |

Any violation: **block** and record the violation with the full provenance path that caused it. Any value that is `UNTRUSTED` in an authority-bearing slot is a hard block regardless of its content.

**Residual risk (document it honestly in the README):** untrusted content (e.g. reviews) may still influence *which* mandate-compliant item is chosen. Lineage guarantees the purchase satisfies the mandate; it does not guarantee it is the best choice. That residual risk is exactly what Blackbox exists to handle.

### 4.5 Plan language and interpreter (CaMeL-style)

- The **P-LLM (planner)** sees ONLY the confirmed mandate and the tool signatures. It never sees web content. It emits a plan in a small JSON AST DSL (not free Python). Define the DSL in `lineage/dsl/schema.json` with node types: `call`, `let`, `if`, `for_each`, `select_best`, `propose_checkout`.
- The **interpreter** executes the plan, wraps every tool result in `Labeled`, and enforces that control-flow decisions (`if` conditions) depending on `UNTRUSTED` values can only choose among options that already passed the contract precheck.
- The **Q-LLM (quarantined extractor)** is called by the interpreter to turn untrusted text into schema-validated JSON. It has no tools, cannot see the mandate's authority fields, and its outputs are always `UNTRUSTED`.
- Tools available to plans: `list_merchants()`, `fetch_manifest(merchant)`, `search_manifest(merchant, filters)`, `fetch_page(url)`, `extract(schema, labeled_text)`, `rank(candidates, criteria)`, `propose_checkout(selection)`.

### 4.6 Flight Recorder

Append-only table `recorder_events` with a hash chain (`prev_hash`, `event_hash`). Each purchase session records:
- mandate (signed), manifests fetched (hashes + signatures), raw page snapshots (stored as blobs, referenced by content hash), every LLM call (model id from config, full prompt, response, temperature, seed if supported, latency), the plan AST, every labeled value, contract results, PayPal request/response pairs (secrets redacted), webhooks received, user approvals.
- The session's final hash goes into the PayPal order as `purchase_units[].custom_id = "pv:" + first 32 hex chars of the session hash` (custom_id max is 127 chars). This binds the money to the decision trace.
- Recorder must support **deterministic re-hydration**: given a session id, rebuild the exact inputs to every step.

### 4.7 Recourse: attribution and remedy

When a user files a complaint ("I got navy, I wanted black", optional photo of what arrived):

**Step A: Fact establishment (no LLM judgement on money).**
- *Fulfillment check:* compare what was delivered (user photo + merchant shipment record) against the signed manifest sku the order was for. A vision model produces structured observations; these are labeled `UNTRUSTED(user_evidence)` and shown to the user to confirm. If delivered item does not match the ordered sku, that is a **fulfillment fault** owned 100 percent by the merchant.
- *Intent check:* ask the user to confirm their *true* intent in structured form (the "clarified mandate"). Compare the purchased sku against the clarified mandate.
- *Claim check:* if delivered item matches the sku but the sku's signed attributes were false (merchant signed "waterproof", item is not), that is **misrepresentation**, provable via signature.

**Step B: Counterfactual replay (only if the purchase *decision* was wrong).**
Three players: `U` (user's original wording), `M` (merchant-supplied content: signed claims plus untrusted pages), `A` (the agent's planner/policy). For each player define an intervention that replaces its contribution with a corrected reference:
- `do(U)`: replace the original mandate with the clarified mandate.
- `do(M)`: replace merchant content with corrected facts (false signed claims fixed from established facts, injected/misleading untrusted content removed).
- `do(A)`: replace the agent's planner with a reference policy (configurable: stronger model and stricter selection rules).

Outcome function `bad(run) = 1` if the replayed purchase violates the clarified mandate, else `0`. Replays re-execute the full Lineage pipeline from recorded inputs with the intervention applied, at temperature > 0, `k` samples per coalition (default `k = 8`, from config).

With 3 players there are 8 coalitions, so compute **exact Shapley values** (no Monte Carlo needed) over `v(S) = P(bad | do(S))` estimated by sample means. Report each player's share with **bootstrap 95 percent confidence intervals**. Normalize positive contributions into fault shares `phi_U, phi_M, phi_A` summing to 1. If the CI is too wide (configurable threshold), escalate to human review instead of auto-remedying.

**Step C: Remedy routing (deterministic).**
Compute remedy amount `R` from the merchant's signed policy and the harm type (full refund if returnable, partial if kept, etc.). Then:

| State | Action |
|---|---|
| Authorization not yet captured | **Void** the authorization, re-run purchase with corrected mandate if user wants |
| Fulfillment fault or misrepresentation | Merchant refund (full or partial) via merchant credentials, capped by the merchant's signed policy |
| Decision fault, captured | Split `R`: merchant share `phi_M * R` as partial refund (within signed caps), agent share `phi_A * R` as **Payout** from the Provenant liability pool to the buyer, user share `phi_U * R` absorbed with explanation |
| Merchant share exceeds signed policy caps | Generate **dispute evidence pack** (PDF: mandate, signed manifest, signature proofs, recorder excerpts, attribution report) and open the dispute path (Section 5.4) |

Every money movement requires a one-click human confirmation in the Ops Console by default (`AUTO_REMEDY=false` in config). Every remedy is written back to the Flight Recorder.

**Step D: Explanation.** An LLM writes the plain-language verdict **from the computed numbers only** (pass it the structured attribution result, not raw evidence). It may not change any number. Show the verdict, the shares with CIs, and the counterfactual table.

---

## 5. PayPal integration map

Use the official PayPal Server SDK for Python if it covers the endpoint cleanly; otherwise call REST directly through one thin client module `paypal/client.py` with OAuth token caching, `PayPal-Request-Id` idempotency keys on every POST, retries with backoff on 5xx, and structured error mapping.

### 5.1 Purchase (Lineage)
1. `POST /v2/checkout/orders` with `intent=AUTHORIZE`, merchant credentials, `purchase_units[].custom_id` = session hash prefix, `invoice_id` unique per session, item breakdown that exactly matches contract-checked values.
2. Buyer approval:
   - *Human-present mode:* PayPal JS SDK v6 button in the Buyer App (or the `approve` HATEOAS link).
   - *Autonomous mode (stretch, gated by spike S4):* vaulted PayPal payment method created via "save PayPal without purchase", then orders created with the vault token.
3. `POST /v2/checkout/orders/{id}/authorize`.
4. On merchant fulfillment: `POST /v2/checkout/orders/{id}/track` (add tracking), then `POST /v2/payments/authorizations/{id}/capture`.
5. Authorization window logic: honor period 3 days, valid up to 29 days. Use `reauthorize` if needed. Store these as config, not literals.

### 5.2 Recourse (Blackbox)
- Void: `POST /v2/payments/authorizations/{id}/void`
- Refund (full/partial): `POST /v2/payments/captures/{id}/refund` with merchant credentials and `PayPal-Request-Id`
- Liability payout: `POST /v1/payments/payouts` from the Provenant operator account; poll `GET /v1/payments/payouts/{batch_id}` and consume payout webhooks
- Order verification: `GET /v2/checkout/orders/{id}` before and after every state change, cross-checked against the recorder

### 5.3 Webhooks
- Register per app via `POST /v1/notifications/webhooks`. Subscribe at minimum to: `CHECKOUT.ORDER.APPROVED`, `PAYMENT.AUTHORIZATION.CREATED`, `PAYMENT.AUTHORIZATION.VOIDED`, `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.REFUNDED`, `PAYMENT.PAYOUTSBATCH.SUCCESS`, `PAYMENT.PAYOUTSBATCH.DENIED`, `CUSTOMER.DISPUTE.CREATED`, `CUSTOMER.DISPUTE.UPDATED`, `CUSTOMER.DISPUTE.RESOLVED`.
- **Always** verify with `POST /v1/notifications/verify-webhook-signature` before trusting an event.
- Webhook handler must be idempotent (dedupe on event id) and order-independent (out-of-order events must not corrupt state).
- Use `POST /v1/notifications/simulate-event` in integration tests.

### 5.4 Disputes
- Merchant-side lifecycle via Disputes API: list, show details, provide evidence (upload the evidence pack), make offer, accept claim.
- Sandbox-only endpoints (`settle`, `require-evidence` / update status) are allowed **only in the demo/test harness**, clearly isolated in `paypal/sandbox_only.py`.
- Buyer-side dispute creation: verify in spike S6 whether any API path exists. If not, the demo files the dispute manually through the sandbox buyer account (documented step), and a stretch goal automates that step with a browser agent (KERNEL sponsor tool).

### 5.5 Negative testing
In the integration test suite, use the `PayPal-Mock-Response` header (e.g. `{"mock_application_codes":"INSTRUMENT_DECLINED"}`, `TRANSACTION_REFUSED`, `DUPLICATE_INVOICE_ID`) to prove Provenant handles failures without corrupting the recorder or ledger.

---

## 6. Merchant simulators and the Adversarial Lab

`merchants/` is a separate FastAPI service hosting 4 storefronts from config profiles in `config/merchants/*.yaml`:

- **Northwind Outfitters** (honest): accurate manifest, ships correctly.
- **Bayline Goods** (sloppy): accurate manifest, ships wrong variant with configurable probability.
- **Kestrel Supply** (misrepresenting): signs at least one false attribute on some skus.
- **Attacker shop**: tries to hijack agents (payee swap, price inflation, item swap, quantity bump, address exfiltration) via poisoned pages and reviews.

Catalogs are **generated** by `scripts/seed_catalog.py` (LLM-assisted generation from a category spec, then frozen to a dated seed file and signed). Pages render from the manifest plus a "content layer" that can include injected payloads.

**Adversarial Lab** (`lab/`): an attack generator that produces injection variants across a taxonomy (`config/attacks/taxonomy.yaml`: goal x surface x technique), places them in pages/reviews of any merchant, and records them as a versioned dataset in `lab/datasets/`. Generation uses an LLM at build time with a fixed seed and the result is committed, so evaluations are reproducible.

---

## 7. Baseline agent (for the "before" picture)

Implement `baseline/` as a conventional tool-calling shopping agent built on **PayPal's Agent Toolkit** (`@paypal/agent-toolkit` or its Python equivalent; check which is current) that reads the same pages and creates orders directly. It is the honest "what most teams build" comparison. It must run against the same merchants and the same attack dataset.

---

## 8. Evaluation harness (this is what makes the pitch credible)

`eval/` produces numbers for the README and the video. All runs are reproducible from a config file and write results to `eval/results/<timestamp>/`.

1. **Security:** attack success rate (ASR) per attack class, baseline vs Provenant, with 95 percent CIs. Report "money redirected to attacker" in sandbox USD.
2. **Utility:** benign task success rate on a set of ~50 generated shopping tasks, baseline vs Provenant. We must show security did not destroy usefulness.
3. **Attribution accuracy:** generate scenarios with **planted ground-truth faults** (pure user ambiguity, pure merchant misrepresentation, pure agent error, and mixed). Report how often Blackbox's majority-fault player matches the planted cause, plus mean absolute error of shares on mixed cases.
4. **Cost and latency:** LLM tokens and wall time per purchase and per recourse case.

Charts render in the Ops Console. Keep run sizes configurable so we can run small during development and larger before submission.

---

## 9. User interface

**Buyer App** (`web/`, Next.js + TypeScript + Tailwind):
- Chat-style request box, then the **Mandate Card** (editable, ambiguity questions highlighted, Confirm & Sign button).
- Live agent run view: steps streaming in, each tool result with its label chip (`USER`, `SIGNED`, `DERIVED`, `UNTRUSTED`).
- **Provenance Graph** (React Flow): the final order's fields on the right, every source on the left, edges colored by label. Blocked fields glow red with the violating path highlighted. This is the money shot of the demo.
- PayPal approval (JS SDK v6).
- Orders list with status from PayPal, and a **Report a problem** flow (text + optional photo + clarified-intent form).
- **Verdict view**: fault shares with CIs as a bar, the counterfactual table ("if the listing had been accurate, a wrong purchase happens 6% of the time instead of 81%"), and the money movements with PayPal transaction ids.

**Ops Console** (`/console`, AG Grid Enterprise trial or Community as licensing allows):
- Recourse queue grid with pending remedies, approve/reject buttons.
- Ledger grid joining recorder sessions with PayPal order, authorization, capture, refund, payout and dispute ids. Server-side row model, filters, grouping by merchant and fault type.
- Eval dashboards.

Design: clean, calm, finance-grade. Read the frontend-design guidance available to you before building UI. Mobile-responsive buyer app.

---

## 10. Tech stack

- **Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy + Alembic, Postgres (SQLite allowed only for local quick runs), `cryptography` for Ed25519, a JCS canonicalization library, httpx.
- **LLMs:** provider-agnostic adapter in `llm/`. Planner, extractor, mandate extractor, vision verifier, narrator, and reference-policy models are each set via env (`LLM_PLANNER_MODEL`, `LLM_EXTRACTOR_MODEL`, etc.). Default to Anthropic models; the baseline should also be runnable on at least one other provider for a fair comparison.
- **Frontend:** Next.js (App Router), TypeScript, Tailwind, React Flow, AG Grid, PayPal JS SDK v6.
- **Infra:** Docker Compose for local; deploy API, merchants, and web on **Render** (public HTTPS URLs are required for webhooks). Optional: Elastic for full-text search over recorder events if time permits.
- **Quality:** pytest (unit + sandbox integration markers), ruff, mypy on the security core, Playwright smoke test for the demo path. GitHub Actions running unit tests and lint on every push.

---

## 11. Repository layout

```
provenant/
  CLAUDE.md
  README.md                 # judges read this first
  LICENSE                   # Apache-2.0
  .env.example
  docker-compose.yml
  config/                   # app.yaml, merchants/*.yaml, attacks/taxonomy.yaml, eval/*.yaml
  api/                      # FastAPI app, routers
  lineage/
    mandate/                # extraction, canonicalization, signing
    manifest/               # fetching, signature verification
    labels/                 # Labeled[T], lattice, propagation
    dsl/                    # schema.json, parser, validator
    interpreter/            # executes plans, enforces label rules
    contracts/              # field contracts and checker
    checkout/               # PayPal order builder
  blackbox/
    recorder/               # hash-chained event store, rehydration
    intake/                 # complaints, evidence, clarified mandate
    facts/                  # fulfillment, intent, claim checks
    replay/                 # interventions, re-execution
    attribution/            # exact Shapley, bootstrap CIs
    remedy/                 # routing, money movement, evidence pack (PDF)
    narrate/                # LLM verdict from numbers only
  paypal/                   # client, orders, payments, payouts, disputes, webhooks, sandbox_only.py
  llm/                      # provider adapters, prompt templates (versioned files)
  merchants/                # storefront simulator service
  baseline/                 # Agent Toolkit baseline agent
  lab/                      # attack generator, datasets
  eval/                     # harness, results
  web/                      # Next.js buyer app + console
  scripts/                  # setup_sandbox.md, register_merchants.py, seed_catalog.py
  spikes/                   # Phase 0 PayPal behavior proofs
  tests/
  docs/                     # architecture.md, threat-model.md, progress.md, demo-script.md
```

Prompts are versioned text files under `llm/prompts/`, never inline strings scattered through code.

---

## 12. Build phases (ask me before moving to the next one)

**Phase 0: Spikes and skeleton (target: Oct 5 to Oct 11, light work)**
Repo scaffold, Docker Compose, CI, `.env.example`, sandbox setup doc, all spikes in Section 13 run and reported.
*Accept:* every spike has a pass/fail note in `docs/progress.md` with the actual PayPal responses (redacted).

**Phase 1: Lineage core (target: Oct 19 to Oct 24)**
Mandate extract/confirm/sign, manifests and signing, label system, DSL + interpreter, Q-LLM extractor, contract checker, PayPal authorize flow end to end with one honest merchant, Flight Recorder writing every step.
*Accept:* a benign purchase completes in sandbox with the session hash in `custom_id`; an attacker payee-swap is blocked with a recorded violation path; unit tests cover label propagation and every contract.

**Phase 2: Blackbox (target: Oct 25 to Oct 30)**
Complaint intake, fact checks, replay engine, exact Shapley with bootstrap CIs, remedy router (void, refund, payout), evidence pack PDF, webhook processing.
*Accept:* three planted scenarios (pure user, pure merchant, pure agent) each produce the correct majority fault and the correct PayPal money movement visible in the sandbox dashboard.

**Phase 3: UI, baseline, lab, eval (target: Oct 31 to Nov 5)**
Buyer App with Provenance Graph and Verdict view, Ops Console on AG Grid, baseline agent, attack dataset, full eval run.
*Accept:* eval produces ASR, utility, and attribution accuracy tables; the full demo path runs from a clean browser.

**Phase 4: Harden, deploy, submit (target: Nov 6 to Nov 11)**
Render deploy with webhooks live, negative-testing suite green, Playwright smoke test, README, threat model, demo script, architecture diagram. Freeze features on Nov 9.
*Accept:* a judge can either use the hosted URL or follow README setup and run it in under 15 minutes. Submit by Nov 11 (deadline is Nov 12 noon PST, which is Nov 13 01:30 IST).

---

## 13. Phase 0 spikes (run these first, report results)

| ID | Question to prove in sandbox | If it fails |
|---|---|---|
| S1 | OAuth + create/authorize/capture an order with merchant credentials, `custom_id` and `invoice_id` set and readable back | Blocker, report immediately |
| S2 | Void an authorization; partial refund of a capture with `PayPal-Request-Id` idempotency (repeat the call, confirm single refund) | Blocker |
| S3 | Payouts from operator account to a buyer sandbox email, batch status and webhook received | Fall back to operator-issued invoice credit, report |
| S4 | Save PayPal without purchase (vault), then create an order with the vault token and no buyer redirect | Autonomous mode becomes stretch; human-present only |
| S5 | Webhook registration on a public URL, delivery, and signature verification; simulate-event works | Use polling fallback, report |
| S6 | Full dispute lifecycle in sandbox: how a dispute gets created on a sandbox order, then list, provide evidence (file upload), and sandbox settle via API | Document the manual step for the demo |
| S7 | Negative testing headers on authorize, capture, refund | Report which codes work |
| S8 | JS SDK v6 button approves a server-created AUTHORIZE order | Use approve link redirect |

Write each spike as a standalone script that reads `.env`, prints the request summary and response, and exits nonzero on failure.

---

## 14. Threat model (write `docs/threat-model.md`)

Cover at least: indirect prompt injection in pages and reviews, manifest tampering (signature), replayed or expired mandates (nonce + expiry), malicious merchant signing false claims (non-repudiation then Blackbox), webhook spoofing (signature verification), duplicate/out-of-order webhooks (idempotency), recorder tampering (hash chain), LLM narrator hallucinating numbers (numbers-only input + post-check that every number in the text appears in the structured result), ranking manipulation among compliant items (residual risk, handled by recourse). State clearly what Provenant does **not** protect against.

---

## 15. Submission checklist (Devpost requirements)

- Public GitHub repo, Apache-2.0 LICENSE visible in About, all source and run instructions.
- Hosted demo URL (Render) plus complete local setup instructions.
- README sections: problem, what it does, architecture diagram, how PayPal is used (endpoint by endpoint), how AI is used (each model role), eval results with charts, threat model summary, limitations, setup, tools used (PayPal Agent Toolkit, AI-Toolkit plugin, Claude, AG Grid, Render, any others) and how each was used.
- Demo video under 3 minutes, public on YouTube, no copyrighted music. Follow `docs/demo-script.md`:
  1. (0:00 to 0:20) The problem: agents get hijacked, and nobody knows who pays when they buy wrong.
  2. (0:20 to 0:55) Baseline agent gets hijacked live by the attacker shop, money goes to the wrong payee in sandbox.
  3. (0:55 to 1:35) Same request through Provenant: Provenance Graph shows the poisoned field blocked; legit purchase proceeds, PayPal authorization created.
  4. (1:35 to 2:25) Something goes wrong anyway: complaint filed, replay runs, fault split shown with CIs, partial refund + liability payout land in sandbox with real transaction ids.
  5. (2:25 to 2:55) Eval numbers: ASR baseline vs Provenant, utility retained, attribution accuracy. Close.

---

## 16. How to work with me

- Start each session by reading this file and `docs/progress.md`.
- For anything non-trivial, show a short plan first, then implement.
- Use subagents for parallelizable work (e.g. merchant simulator while I review Lineage), but keep the security core in one consistent style.
- When a PayPal call fails, run `/paypal:explain-error`, paste the redacted response into `docs/progress.md`, and propose a fix.
- Never mark a task done without running its tests. Never claim a sandbox behavior works without a spike or integration test that proves it.
- If you think part of this brief is wrong or infeasible, say so directly with evidence. Do not quietly substitute something easier.

**First task:** read this file fully, set up the repo scaffold from Section 11, then implement and run spikes S1 and S2. Report results before doing anything else.
