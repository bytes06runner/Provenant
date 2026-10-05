# Provenant buyer app

Next.js (App Router), TypeScript, Tailwind and React Flow. The design direction is in
`../docs/design.md`.

The app talks to the Provenant API (`../api/app.py`). Start both:

```bash
.venv/bin/uvicorn api.app:app --port 8700        # from the repository root
npm --prefix web run dev -- --port 3000
```

Set `NEXT_PUBLIC_API_URL` if the API is not on `http://127.0.0.1:8700`.

Screens so far (Lineage first): landing, request and mandate card, live agent run with label chips,
provenance graph, PayPal approval (JS SDK v6) and orders.
