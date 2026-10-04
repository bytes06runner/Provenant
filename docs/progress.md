# Progress

## 2026-10-04: Phase 0 kickoff

### Done
- Repo scaffold per CLAUDE.md Section 11 (package dirs, `config/`, `.env.example`, `.gitignore`,
  Apache-2.0 `LICENSE`, `docker-compose.yml` with Postgres, GitHub Actions CI).
- `paypal/client.py`: OAuth token cache per REST app, `PayPal-Request-Id` on every POST (stable
  across retries, pinnable by the caller), backoff on 5xx and transport errors only, one token
  refresh on 401, structured `PayPalError` with issue codes and `debug_id`.
- `paypal/redact.py`: masks tokens, secrets, payer emails and payer names before anything is
  printed or written.
- Spikes S1 and S2 written (`spikes/`). Redacted logs go to `spikes/out/` (gitignored).
- Unit tests: 9 passing (token caching, request-id reuse across retries, no retry on 4xx,
  retry cap, 401 refresh, redaction). `ruff` and `mypy --strict` on `paypal/` are clean.

### Spike results

| ID | Result | Notes |
|---|---|---|
| S0 | PASS | All 5 REST apps (4 merchants + operator) get OAuth tokens. See below. |
| S1 | PASS | 14/14 checks, merchant `northwind`, buyer A approved in browser. |
| S2 | PASS | Void 2/2, idempotent partial refund 5/5. Needed one fix and a resume, see below. |
| S3, S7 | In progress | |
| S4, S6 | Waiting on you | Vault enabled on merchant apps (S4); a sandbox dispute filed (S6). |
| S5 | Not started | Will use a cloudflared tunnel. |
| S8 | Not started | After S3 and S7. |

#### S0: OAuth and scopes (2026-10-04)
All apps: `refund`, `payments/payouts`, `disputes/read-seller`, `disputes/update-seller`,
`disputes/create`, `applications/webhooks`, `payment/authcapture`, `shipping/trackers/readwrite`.
Not present on any app: `vault/payment-tokens/readwrite`. Flag for S4 (autonomous mode): vaulting
may need to be enabled on the app in the dashboard.

#### S1: create, approve, authorize, capture (2026-10-04, 15:03 UTC)
Redacted responses (full log in `spikes/out/S1-20261004T150343Z.json`, gitignored):

| Step | HTTP | PayPal debug_id | Result |
|---|---|---|---|
| POST /v2/checkout/orders | 200 | f2176799fdc66 | order `61860079CP5350614`, `PAYER_ACTION_REQUIRED` |
| GET order (pre-approval) | 200 | f6697142497c6 | `custom_id=pv:621f54d9c25ef29791b1104938faf57a`, `invoice_id=pv-spike-170d...e49f` |
| (buyer approves in browser) | | | `APPROVED` detected by polling |
| POST .../authorize | 201 | f649200f7cc42 | order `COMPLETED`, authorization `5655675599407182X` `CREATED` |
| GET authorization | 200 | f937658487469 | `custom_id` and `invoice_id` echoed, `expiration_time=2026-11-02T15:03:36Z` |
| POST .../authorizations/{id}/capture | 201 | f937658f3826e | capture `3B162418P49418349` `COMPLETED`, `final_capture=true` |
| GET capture | 200 | f496675ad694a | `custom_id` and `invoice_id` echoed; sandbox fee 1.36 USD on 25.00 |
| GET order (final) | 200 | f69421680086d | `custom_id` still bound to the order |

Findings:
- `custom_id` propagates order -> authorization -> capture. The decision trace binding in brief
  Section 4.6 works as designed, and is readable from any of the three objects.
- Observation only: create order answered 200 (not 201) when `payment_source.paypal` was
  supplied. We do not depend on this; see "Decisions" below.
- Authorization `expiration_time` is exactly 29 days after authorization, matching
  `config/app.yaml: paypal.authorization.valid_days`.

#### S2: void, and idempotent partial refund (2026-10-04, 15:04 to 15:06 UTC)
Logs: `spikes/out/s2.console.txt` (first run), `spikes/out/S2b-20261004T150606Z.json` (resume).

First run: both orders approved by buyer A and authorized (201).
- (a) Void: `POST /v2/payments/authorizations/15T846825A7221243/void` returned **204** (empty
  body). `GET` shows `status=VOIDED`. **PASS.**
- (b) Capture of order `27X917119W351372Y` succeeded (201, capture `9RD264568L409791S`
  `COMPLETED`), then the spike crashed with `KeyError: 'amount'`. Cause: our bug, not PayPal's.
  Without `Prefer: return=representation`, capture (and void) responses are minimal: only `id`,
  `status`, `links`. Fix: read the amount back with `GET /v2/payments/captures/{id}`, which the brief
  requires around every state change anyway. Spikes now also log unexpected crashes instead of
  exiting without a log.
- Resumed part (b) on the same capture with `--capture-id`:

| Step | HTTP | debug_id | PayPal-Request-Id | Result |
|---|---|---|---|---|
| GET capture | 200 | f762156fa950b | | `COMPLETED`, 25.00 USD |
| POST .../captures/9RD264568L409791S/refund (10.00) | 201 | f7076271b91c8 | 883f88d6-... | refund `23764753N6120533F` |
| same POST, same request id | 200 | f90832721b725 | 883f88d6-... | same refund id |
| same POST, same request id | 200 | f41452063af8a | 883f88d6-... | same refund id |
| GET capture | 200 | f601096b3c2ea | | `PARTIALLY_REFUNDED` |
| GET order 27X917119W351372Y | 200 | f692589c564fd | | exactly 1 refund, total 10.00 |

Findings:
- PayPal-side idempotency works: replays with the same `PayPal-Request-Id` returned the original
  refund and created nothing new. Observation only: the first call answered 201 and replays 200.
  We do not build on that difference (see "Decisions").
- Void returns 204 with no body. Never parse a void response.
- State-changing POST responses are minimal by default (only `id`, `status`, `links`).

### Decisions (2026-10-04, after S1/S2 review)
1. **Success is any 2xx.** No code branches on 200 vs 201 (or 204). State is read from the
   resource body, and verified with a GET after every state change.
2. **Idempotency is enforced in our own ledger,** not inferred from PayPal status codes.
   `paypal/ledger.py` persists every POST: `operation_key`, `PayPal-Request-Id`, app, path,
   state (`PENDING`/`SUCCEEDED`/`FAILED`), resulting resource id and status, debug id.
   - The entry is written before the request is sent, so a crash leaves `PENDING` and the retry
     reuses the same request id.
   - A `SUCCEEDED` operation is never resent; the client raises `AlreadyCompleted` with the
     recorded resource id. PayPal's idempotency window is finite, so relying on it alone could
     move money twice after the window closes.
   - 4xx is recorded as `FAILED` with the issue code and no resource id.
3. **Every POST sends `Prefer: return=representation`,** so responses carry the full resource.
   The verification GET after each state change stays.
4. Spikes use a SQLite ledger under `spikes/out/` (gitignored). S2 deliberately runs without a
   ledger because it tests PayPal's own request-id handling.

### Design notes from writing S1/S2
- Orders are created with `payment_source.paypal.experience_context`, so PayPal returns a
  `payer-action` link (not `approve`). The spike accepts either.
- An `AUTHORIZE` order cannot be authorized until a buyer approves it. In human-present mode
  that is a real browser login, so S1/S2 print the link and poll `GET /v2/checkout/orders/{id}`
  until `APPROVED`. S2 creates both of its orders up front so they are approved in one sitting.
- S2 idempotency check: same `PayPal-Request-Id` sent 3 times; pass requires one refund id,
  one refund on the order, capture status `PARTIALLY_REFUNDED`, refunded total equal to a
  single partial refund.

### Tooling
- gitleaks runs as a pre-commit hook (`.pre-commit-config.yaml`), with extra rules for PayPal
  client ids, secrets and access tokens in `.gitleaks.toml`. Verified: a staged realistic secret
  is blocked; full history scan is clean.

### Open items
- PayPal AI-Toolkit plugin was not loaded in this session, so its `paypal-best-practices` skill
  was not consulted. Relaunch with `claude --plugin-dir <path-to-AI-Toolkit>` and re-review
  `paypal/client.py` and the spikes against it before Phase 1.
- Docker is not installed on the dev machine; Postgres via compose is untested locally.
