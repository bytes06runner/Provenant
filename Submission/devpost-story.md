# Provenant

**Tagline:** A tribunal for every purchase your agent makes.

## Inspiration

AI agents are starting to shop for people, and they read the open web to do it. A product review
or a hidden banner can tell an agent whom to pay, how much and where to ship. Published 2026
benchmarks put prompt injection success against shopping agents at roughly 40 to 70 percent. When
an agent buys the wrong thing, there is no rule for who pays: the person who asked, the merchant
whose listing misled the agent, or the company running the agent. We wanted agent payments that
cannot be hijacked, and a fair, provable answer to "who owes the money" when something still goes
wrong.

## What it does

Provenant is a trust layer between a shopping agent and PayPal.

- **Lineage (prevent).** The buyer's request becomes a mandate they review and sign. Merchants
  publish signed catalogs.
  - Every value the agent handles carries a label for where it came from: User, Sealed (merchant
    signed), Derived or Untrusted.
  - Field contracts let the PayPal order's payee, item and price come only from the merchant's
    seal. The quantity and address come only from the buyer, and the total only from arithmetic
    over those values.
  - Web text can never bind any of these fields. Every field shows in a provenance graph.
- **Blackbox (rule).** A complaint opens a case.
  - Provable facts come first: a wrong item shipped, or a signed claim that was false.
  - If the decision itself was wrong, the purchase is replayed from its recorded inputs with each
    party corrected.
  - Exact Shapley values split the fault among user, merchant and agent, with confidence
    intervals. Uncertain cases go to a human.
- **Remedy (pay).** The ruling moves money on PayPal.
  - A void before capture, or a merchant refund within its signed policy.
  - A payout of the agent's share from a liability pool, or a dispute evidence pack.
  - Each movement takes one operator click.
- **Flight Recorder.** A hash-chained log of every step. The PayPal order's `custom_id` is the
  hash of the decision trace, so the money is bound to its evidence.

## How we built it

- **Backend.** Python 3.12 with FastAPI, SQLAlchemy and Alembic on Postgres. Ed25519 signatures
  run over RFC 8785 canonical JSON.
- **Lineage.** A CaMeL-style design: the planner model sees only the signed mandate and writes a
  small JSON plan. A quarantined extractor with no tools reads page text, and a deterministic
  interpreter and contract checker decide what may be paid.
- **Blackbox.** Counterfactual replays at temperature above zero. Exact Shapley values over the
  8 coalitions, with Jeffreys posteriors propagated into the shares.
- **PayPal.** Orders v2 (AUTHORIZE) with the merchants' own credentials, plus authorize, capture,
  track, void, refund, Payouts and the Disputes API. Webhooks are verified with
  verify-webhook-signature. The buyer approves with the JS SDK v6 button. The baseline agent uses
  PayPal's Agent Toolkit.
- **Proof first.** Every PayPal behavior was first proven by a standalone spike against the
  sandbox. Idempotency runs through our own request ledger with `PayPal-Request-Id`.
- **Models.** Free tiers of Groq (gpt-oss-120b, Qwen) and Google AI Studio (Gemini Flash-Lite,
  Flash). Every role is configured by environment variable.
- **Frontend.** Next.js, Tailwind, React Flow for the provenance graph, and AG Grid for the
  operations console.
- **Deployment.** Render, with live PayPal webhooks.
- **Tooling.** Built with Claude Code and the PayPal AI-Toolkit plugin.

## Challenges we ran into

- **Keeping models out of the money decision.** Contracts, signatures, attribution and routing
  are deterministic code with 100% test coverage. The narrator may not even change a number: every
  number in its text is checked against the computed result.
- **Honest statistics at small sample sizes.** A bootstrap gave zero-width intervals at 4 of 4
  outcomes, so we switched to Jeffreys intervals propagated through the Shapley map.
- **Free-tier limits.** One account's per-minute and daily limits forced a budget tracker,
  provider fallbacks, response caching and common random numbers across replays.
- **Real money movement on the sandbox.** This took idempotent retries, reconciliation by GET
  after every change, and webhooks treated only as hints.

## Accomplishments that we're proud of

- 14 of 14 live hijack attempts blocked, including the attacker shop's own validly signed payee.
- 3 of 3 planted faults (user, merchant and agent) attributed correctly, each with the right
  PayPal movement:
  - Void `0XU54803RE449331J`
  - Refund `6VC997542C814874E`
  - Payout `ASF3PFT6573V2`
- A real shared-fault ruling (merchant falsely signed "waterproof") with a 45.26 refund,
  `3AA89058PW941291F`.
- 573 tests and a deployed staging stack with verified webhooks.

## What we learned

- Provenance is a better security boundary for agents than filtering text. It does not matter
  how clever an injection is if page text can never reach the payee field.
- Liability becomes tractable once every step is recorded: counterfactual replay turns "whose
  fault" into a measurable question.

## What's next for Provenant

- Passkey (WebAuthn) mandate signing.
- Vaulted autonomous checkout.
- Real merchant onboarding through PayPal partner flows.
- Larger attack and attribution evaluations.
- Standard dispute packs that a payment network could adopt for agent purchases.

## Built with

python, fastapi, postgresql, alembic, next.js, typescript, tailwind, react-flow, ag-grid,
paypal-orders-api, paypal-payouts, paypal-disputes-api, paypal-webhooks, paypal-js-sdk,
paypal-agent-toolkit, groq, gemini, render, playwright, claude-code
