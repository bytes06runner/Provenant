# Provenant architecture

This document describes how the system is put together and the decisions that shape it. The
product brief is [`CLAUDE.md`](../CLAUDE.md); proven PayPal behavior and the decision log are in
[`progress.md`](progress.md).

## Overview

```
                      +------------------------------------------+
  user request -----> |  Buyer App (Next.js)                     |
                      |  mandate card, run view, provenance      |
                      |  graph, PayPal approval, complaints      |
                      +--------------------+---------------------+
                                           |
                      +--------------------v---------------------+
                      |  Provenant API (FastAPI)                 |
                      |                                          |
                      |  LINEAGE (prevent)                       |
                      |    mandate -> planner -> interpreter     |
                      |    labels, Q-LLM extractor, contracts    |
                      |    checkout builder                      |
                      |                                          |
                      |  BLACKBOX (recover)                      |
                      |    reconciliation poller   <------------+-------- webhooks
                      |    complaint intake, fact checks        |          (hints only)
                      |    replay, Shapley attribution          |
                      |    remedy router, evidence pack         |
                      |                                          |
                      |  FLIGHT RECORDER (hash-chained log)      |
                      |  REQUEST LEDGER (PayPal idempotency)     |
                      +----+--------------+---------------+------+
                           |              |               |
                    merchant sims     Postgres       PayPal sandbox
```

## Trust model (Lineage)

Every runtime value is a `Labeled[T]` carrying a label and its sources.

| Label | Raised by | Example |
|---|---|---|
| `USER` | User confirmation only (signed mandate, confirmed vault entry) | quantity, max_total, ship_to address |
| `MERCHANT_SIGNED` | Signature verification of a registered merchant's manifest | payee, sku, price, shipping, tax |
| `DERIVED` | Any computation; never higher than this | order total |
| `UNTRUSTED` | Everything else: pages, reviews, LLM output | product descriptions |

Rules:
- A computed value is `join(DERIVED, *inputs)`: capped at `DERIVED`, and `UNTRUSTED` if any input
  is untrusted. Sources are the union of the inputs' sources.
- Field contracts (`lineage/contracts`) gate every order. Any `UNTRUSTED` provenance in an
  authority-bearing field is a hard block regardless of value.
- Cross-field binding: every `MERCHANT_SIGNED` source behind `item.sku`, `unit_price` and
  `amount.total` must be the same manifest (ref and hash) the payee came from.
- Signatures: Ed25519 over `purpose || NUL || JCS(payload)` (RFC 8785), so a manifest signature can
  never verify as a mandate.

## PayPal integration principles

1. **GETs are the source of truth.** State is read from the PayPal resource, never inferred from
   an HTTP status code or a webhook body. Any 2xx is success; nothing branches on 200 vs 201.
2. **Idempotency lives in our request ledger** (`paypal/ledger.py`). Every POST is persisted with
   an operation key, its `PayPal-Request-Id` and the resulting resource id, before and after
   sending. A succeeded operation is never re-sent; an unknown outcome is retried with the same
   request id.
3. **Every POST asks for `Prefer: return=representation`**, and every state change is followed by
   a verification GET.
4. **Sandbox-only endpoints** (dispute `adjudicate`, `require-evidence`) live in
   `paypal/sandbox_only.py` and refuse to run against any non-sandbox host.

## Reconciliation poller (Blackbox, first-class component)

Decision (2026-10-05): **GETs are the source of truth; webhooks only trigger an early poll.**

Why: in the sandbox, a real `PAYMENT.AUTHORIZATION.VOIDED` was never generated for a confirmed
void, while refund, payout and dispute events were generated and delivered (S5, S6). Webhook
delivery is also asynchronous, can be duplicated, and can arrive out of order. Money decisions
cannot depend on it.

Design:

```
  tracked resources (from the request ledger and the Flight Recorder)
      order, authorization, capture, refund, payout batch, dispute
                 |
                 v
  +-----------------------------+      verified webhook (any type)
  |  reconciliation poller      | <--- "resource X may have changed": schedule X now
  |                             |
  |  for each due resource:     |
  |    GET from PayPal          |
  |    diff against last seen   |
  |    on change: append a      |
  |    recorder event, update   |
  |    the ledger view, wake    |
  |    any waiting remedy       |
  +-----------------------------+
                 |
                 v
        Flight Recorder (hash chain)
```

- **Schedule.** Each tracked resource has a next-poll time from a per-type backoff in config (fast
  while a transition is expected, for example a payout batch in `PENDING`; slow once terminal;
  stop when final). Authorizations also get polls around the honor-period and expiry times.
- **Webhooks are hints.** After signature verification and dedupe (`paypal/webhooks.py`), a
  webhook only moves its resource's next poll to now. Its body is stored for audit but never
  applied to state. A spoofed or replayed webhook can at most cause an extra GET.
- **Order independence.** Because state always comes from a fresh GET, out-of-order or missing
  webhooks cannot corrupt state. The poller records each observed transition once (keyed on
  resource id and observed status), so repeated polls are idempotent.
- **Remedies wait on observed state.** The remedy router never assumes a refund, payout or void
  has landed until the poller has observed it with a GET.
- **Reconciliation report.** The Ops Console shows resources whose PayPal state disagrees with
  what the recorder expects (for example a refund the ledger marks succeeded but PayPal does not
  list), for a human to resolve.

## LLM roles and model choice

Lineage's guarantees do not depend on any model: authority-bearing fields are bound by labels
and contracts, and a plan can only propose prechecked candidates. Model choice therefore
optimizes for speed and cost, except where it changes what Blackbox measures.

| Role | Model (env) | Why |
|---|---|---|
| Planner | Gemini Flash-Lite, Groq gpt-oss-120b fallback | Fast (about 4 s per plan vs about 12 s and an 8,000 tokens/minute cap on Groq); 20/20 valid plans with prompt v2 |
| Mandate extractor | gpt-oss-120b, Flash-Lite fallback | Sees only the user's own words; output is confirmed by the user |
| Q-LLM extractor and ranker | Groq Qwen, Flash-Lite fallback | Deterministic at temperature 0 in S9; no tools; output always UNTRUSTED |
| Reference policy, do(agent) | gpt-oss-120b, stricter selection rules, no fallback | See below |
| Baseline agent | Same model as the planner | Isolates architecture in the comparison |
| Vision verifier | Flash-Lite, escalating to Gemini Flash | Escalates only on low confidence or disagreement with the shipment record, to protect Gemini Flash's 20 requests/day |
| Narrator | Flash-Lite | Writes from computed numbers only |

### Why the reference policy is a different, stronger model

Attribution asks how often the purchase goes wrong when the agent's contribution is replaced by
a reference agent: `do(A)`. If the reference policy ran on the same model as the planner, the
intervention would change almost nothing. Replays with and without `do(A)` would sample from
nearly the same distribution, `P(bad | do(A))` would stay close to `P(bad)`, and the agent's
Shapley share would be pushed toward zero. Agent fault would be systematically underestimated.

So `do(A)` swaps in both a different model (gpt-oss-120b, larger than the planner's Flash-Lite)
and stricter selection rules (for example: never let untrusted review content outweigh a signed
attribute match, prefer the candidate whose signed attributes match the mandate most
specifically). The difference between planner and reference is then a real counterfactual: what
a careful, stronger agent would have done with the same mandate and the same merchant content.

The reference policy has no fallback model. A fallback to a weaker or same-family model would
silently change the counterfactual mid-run; Blackbox waits for gpt-oss-120b instead.

### Replay sampling: common random numbers (decided 2026-10-06)

The planner sees only the mandate and the review URL, never merchant content. So for any
coalition S, do(S) and do(S + M) give the planner byte-identical input, and the only difference
between the two replays is the content the interpreter serves. With
`attribution.common_random_numbers: true` (config/app.yaml), sample j of every coalition with
identical planner input shares one plan; plans are keyed by exactly what the planner sees
(role, prompt, context, sample index), so nothing is shared that could differ.

- Each coalition still gets k samples from the same distribution, so v(S) is unchanged in
  expectation. The M comparison becomes paired, which removes planner noise from it.
- The bootstrap resamples sample indices jointly across coalitions (paired bootstrap), which is
  valid for paired and for independent samples.
- It halves planner and reference-policy calls: 4 distinct planner inputs per sample instead of
  8. A k=8 case needs 16 reference-policy calls (about 50k tokens on gpt-oss-120b) instead of 32.
- A planning failure is shared as well (the same input fails the same way), and every
  `replay.run` event records `plan_reused`.

Set the flag to false to sample every coalition independently.

## Components and status

| Component | Path | Status |
|---|---|---|
| PayPal client, request ledger, redaction | `paypal/` | built |
| Webhook intake (verify, dedupe) | `paypal/webhooks.py` | built |
| Labels, signing, mandate, manifest, vault, contracts | `lineage/` | built |
| DSL, interpreter, Q-LLM extractor, checkout builder | `lineage/` | Phase 1 |
| Flight Recorder | `blackbox/recorder` | Phase 1 |
| Reconciliation poller | `blackbox/reconcile` | Phase 2 |
| Intake, facts, replay, attribution, remedy, evidence pack | `blackbox/` | Phase 2 |
| Buyer App and Ops Console | `web/` | Phase 3 |
