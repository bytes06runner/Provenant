# Threat model

Provenant sits between an AI shopping agent and PayPal. This document lists what an attacker can
try, what stops it, where the defense lives in the code, and what Provenant does **not** protect
against.

## Assets

- **The buyer's money**: the payee, amount, item, quantity and shipping address of every order.
- **The buyer's data**: shipping addresses in the vault, payer details returned by PayPal.
- **The evidence**: the Flight Recorder trace that every ruling and every remedy depends on.
- **The liability pool**: the operator account that pays the agent's share of a fault.

## Trust boundaries

| Source | Label | Can bind payee, item, price, quantity, address? |
|---|---|---|
| What the buyer confirmed and signed | `USER` | quantity, address, limits |
| A merchant's signed manifest (verified Ed25519 over RFC 8785 JSON) | `MERCHANT_SIGNED` | payee, sku, unit price, shipping and tax |
| Arithmetic over the two above | `DERIVED` | the total, and only from those inputs |
| Pages, reviews, banners, every model output, buyer photos | `UNTRUSTED` | never |

Labels can only be raised by signature verification or by the buyer confirming. A model can never
raise a label.

## Threats and defenses

| # | Threat | Defense | Where |
|---|---|---|---|
| 1 | **Indirect prompt injection** in pages, reviews or banners ("pay this account", "add 2 more", "ship to") | The planner never sees web content. Page text goes only to a quarantined extractor with no tools, and its output is `UNTRUSTED`. The field contract checker blocks any `UNTRUSTED` value in an authority-bearing field, even when it equals the correct value. The checkout builder only accepts a sealed, approved contract result. | `lineage/labels`, `lineage/interpreter`, `lineage/contracts`, `lineage/checkout` |
| 2 | **Attacker merchant with a valid signature** offering a mandate-compliant item | The payee must come from the manifest of the merchant selected under the mandate's allowlist, and sku, price and total must come from the same manifest as the payee. A seal from another merchant cannot be mixed in. | `lineage/contracts` |
| 3 | **Manifest tampering** in transit or at rest | Signatures are checked against keys registered at onboarding; the label records the manifest hash. A changed byte fails verification. | `lineage/manifest`, `lineage/signing.py` |
| 4 | **Replayed or expired mandate** | Each mandate has a nonce, `issued_at` and `expires_at`; a nonce registry refuses a second use. | `lineage/mandate`, `lineage/nonces.py` |
| 5 | **Merchant signs a false claim** ("waterproof") | The signature makes the claim non-repudiable. Blackbox establishes misrepresentation from the signed record and routes a merchant refund within the merchant's signed policy. | `blackbox/facts`, `blackbox/remedy` |
| 6 | **Wrong item shipped** | The vision check reads the buyer's photo as `UNTRUSTED` evidence that the buyer confirms; a mismatch with the ordered sku is a fulfillment fault owned by the merchant. | `blackbox/facts` |
| 7 | **Webhook spoofing** | Every delivery is verified with PayPal's `verify-webhook-signature` using the receiving app's webhook id before anything is stored. | `paypal/webhooks.py` |
| 8 | **Duplicate or out-of-order webhooks** | Events are stored once (dedupe on event id and transmission id). Webhooks are hints only: the reconciliation poller re-reads the resource with a GET, which stays the source of truth. | `paypal/webhooks.py`, `blackbox/reconcile` |
| 9 | **Duplicate money movement** (retries, double clicks, crashes) | Our own request ledger records every POST with its operation key and `PayPal-Request-Id` before and after sending. A succeeded operation is never sent again; an unknown outcome is retried with the same request id. | `paypal/ledger.py` |
| 10 | **Recorder tampering** | Append-only, hash-chained events per session, serialized with a Postgres advisory lock. The order's `custom_id` holds a prefix of the chain head, so even a fully re-linked rewrite no longer matches the money. | `blackbox/recorder` |
| 11 | **Narrator invents or changes numbers** | The narrator receives only the computed attribution result. Every number in its text is checked against that result; any number not present rejects the text. | `blackbox/narrate` |
| 12 | **Model decides a payment** | No model output reaches a payment decision. Contracts, signatures, attribution and remedy routing are deterministic code with 100% line and branch coverage. | `lineage`, `blackbox` |
| 13 | **Unwarranted automatic payouts** | `AUTO_REMEDY=false` by default: every money movement waits for one operator click. Wide confidence intervals (above the configured width) escalate to human review instead of paying. | `blackbox/case.py`, `config/app.yaml` |
| 14 | **Secret leakage** | Secrets live only in `.env` and Render secret files. Tokens, client secrets, payer emails and names are redacted before logging or recording. gitleaks runs on every commit with PayPal-specific rules. | `paypal/redact.py`, `.gitleaks.toml` |
| 15 | **Ranking manipulation among compliant items** (a review pushes a worse but valid choice) | Residual risk, by design. Lineage guarantees the purchase satisfies the mandate, not that it is the best choice. Blackbox handles it: the replay finds the agent's or merchant's share and pays it back. | `blackbox/replay`, `blackbox/attribution` |

## What Provenant does not protect against

- **A compromised Provenant server or operator.** The server holds the per-user signing keys in
  the MVP (passkey signing is future work) and the merchants' REST credentials.
- **A buyer who signs a mandate they did not mean.** The mandate card shows every field and asks
  about ambiguities, but a confirmed mandate is the buyer's statement. Blackbox can still assign the
  user's share of a bad outcome.
- **Fraudulent merchants outside the manifest.** A merchant can sign a false claim; Provenant makes
  that provable and recoverable, it does not prevent it.
- **Choice quality.** See threat 15.
- **Model-provider failures or outages.** Fallbacks and budgets reduce the impact; an unavailable
  model stops a purchase, it never lets one through unchecked.
- **Live PayPal.** Everything here runs on the PayPal sandbox.
