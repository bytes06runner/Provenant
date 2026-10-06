# Deploying Provenant on Render

Three web services and one Postgres database, created and updated by `scripts/render_deploy.py`
from the public GitHub repository.

| Service | Runtime | Start | Health |
|---|---|---|---|
| `provenant-api-<env>` | Python 3.12 | `alembic upgrade head && uvicorn api.app:app` | `/api/health` |
| `provenant-merchants-<env>` | Python 3.12 | `uvicorn merchants.app:app` | `/healthz` |
| `provenant-web-<env>` | Node 20 (`web/`) | `npm run start` | `/` |
| `provenant-<env>-db` | Postgres 16 | schema by Alembic (`migrations/`) | |

Staging (2026-10-06): `https://provenant-web-staging.onrender.com`,
`https://provenant-api-staging.onrender.com`, `https://provenant-merchants-staging.onrender.com`.

## Secrets: where each one lives

| Secret | API | Simulator | Buyer app |
|---|---|---|---|
| PayPal client ids and secrets (merchants, operator) | env | none | none |
| Groq and Gemini keys, model roles | env | none | none |
| Buyer signing key (`buyer_a.ed25519.pem`) | secret file | none | none |
| Merchant signing keys (`<merchant>.ed25519.pem`) | none (it only needs public keys) | secret files | none |
| Merchant registry (public keys, payee ids, storefront URLs) | secret file | secret file | none |
| Database URL | env (internal URL) | none | none |
| API URL | | | `NEXT_PUBLIC_API_URL` at build |

Secret files are mounted at `/etc/secrets`. `PROVENANT_KEYS_READONLY=1`: a deployed service
never creates a key; a missing key is an error. Local-only values (`RENDER_API_KEY`, the staging
database's external URL, a second Groq key) are never sent.

## Deploy

```bash
.venv/bin/python scripts/render_deploy.py              # create or update, then redeploy
.venv/bin/python scripts/render_deploy.py --webhooks   # also register PayPal webhooks
```

The database is created once (dashboard or API, plan of your choice). The deploy script reads
its internal connection string. Migrations run at every API start (`alembic upgrade head` is a
no-op when current). To migrate from this machine, the database's IP allow list must include this
machine's address; the services themselves use the internal URL.

## Webhooks

Each PayPal REST app (four merchants, the operator) gets one webhook pointing at
`<api>/api/webhooks/paypal/<app>`. Its id is stored as `PAYPAL_WEBHOOK_ID_<APP>`. A delivery is
verified with PayPal (`verify-webhook-signature`), stored once by event id, and then only makes the
reconciliation poller GET the resource sooner. GET is always the source of truth.

## Free-plan caveats

- Free Postgres expires 30 days after creation (staging: 2026-11-05). Production needs a paid
  database, or a fresh free one created after Oct 13 so it outlives the Nov 12 deadline.
- Free web services sleep after 15 minutes without traffic; the first request takes up to a
  minute. PayPal retries webhooks, and reconciliation catches up on wake.
