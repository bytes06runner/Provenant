"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Card, Hash, LabelChip } from "@/components/ui";
import { API_URL } from "@/lib/api";

type Step = { n: number; tool: string; args: Record<string, unknown>; result: string };
type Run = {
  id: string;
  request: string | null;
  attack: { id: string; goal: string; host: string; surface: string } | null;
  steps: Step[];
  order?: {
    merchant: string;
    id: string | null;
    paypal: { status?: string; payee?: string; amount?: string; items?: { name: string; quantity: string; unit: string }[]; ship_to?: Record<string, string> };
    signed: { sku: string; price: string; total: string; payee: string } | null;
    attacker_payee: string | null;
  };
};

const TOOL: Record<string, string> = {
  list_merchants: "Listed the shops",
  open_page: "Read a page",
  checkout: "Called PayPal create_order",
  finish: "Finished",
  invalid: "Unusable reply",
};

function budgetOf(request: string | null): number | null {
  const m = request?.match(/at most (\d+(?:\.\d+)?) dollars/);
  return m ? Number(m[1]) : null;
}

export default function BaselineRun() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API_URL}/api/baseline/${id}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setRun)
      .catch((e) => setError(String(e)));
  }, [id]);

  if (!run) return <div className="mx-auto max-w-5xl px-4 py-10 text-muted sm:px-6">{error ?? "Loading the run..."}</div>;
  const o = run.order;
  const charged = o?.paypal.amount ? Number(o.paypal.amount) : null;
  const signedTotal = o?.signed ? Number(o.signed.total) : null;
  const budget = budgetOf(run.request);
  const toAttacker = o && o.attacker_payee && o.paypal.payee === o.attacker_payee;

  return (
    <div className="mx-auto max-w-5xl px-4 py-10 sm:px-6">
      <p className="text-xs uppercase tracking-[0.2em] text-muted">
        The before picture <span className="num normal-case tracking-normal">{run.id}</span>
      </p>
      <h1 className="display mt-2 text-4xl sm:text-5xl">A conventional agent checks out</h1>
      <p className="mt-3 max-w-3xl text-muted">
        Built the way most agent checkouts are, on PayPal&apos;s Agent Toolkit: the model reads shop pages and calls{" "}
        <span className="num text-text">create_order</span> with the shop, item, price, quantity and address it chose. Same
        model as Provenant&apos;s planner, same shops.
      </p>
      <blockquote className="mt-6 border-l-2 border-accent pl-4 italic">{run.request}</blockquote>

      <div className="mt-8 grid gap-6 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <ol className="min-w-0 space-y-2">
          {run.steps.map((s) => (
            <li key={s.n} className="rounded-lg border border-line bg-surface/40 px-4 py-3">
              <div className="flex items-center justify-between gap-2 text-sm font-medium">
                <span>
                  <span className="num mr-2 text-xs text-muted">{String(s.n).padStart(2, "0")}</span>
                  {TOOL[s.tool] ?? s.tool}
                </span>
                {s.tool === "open_page" && <LabelChip label="UNTRUSTED" />}
              </div>
              <div className="mt-1 break-words text-sm text-muted">
                {s.tool === "open_page" ? String(s.args.url ?? "") : null}
                {s.tool === "checkout" ? `${String(s.args.merchant ?? "")}: ${JSON.stringify(s.args.items ?? [])}` : null}
                {s.tool !== "open_page" && s.tool !== "checkout" ? s.result : null}
              </div>
            </li>
          ))}
        </ol>

        {o && (
          <aside className="space-y-4">
            <Card>
              <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">What PayPal holds</h2>
              <p className="mt-1 text-xs text-muted">Read back from PayPal by GET.</p>
              <dl className="mt-3 space-y-1 text-sm">
                <div className="flex justify-between gap-2">
                  <dt className="text-muted">Order</dt>
                  <dd>
                    <Hash value={o.id} n={17} />
                  </dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-muted">Shop</dt>
                  <dd>{o.merchant}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-muted">Payee</dt>
                  <dd className={toAttacker ? "font-semibold text-crimson" : ""}>
                    <Hash value={o.paypal.payee} n={14} />
                    {toAttacker ? " (the attacker)" : ""}
                  </dd>
                </div>
                {(o.paypal.items ?? []).map((i, k) => (
                  <div key={k} className="flex justify-between gap-2">
                    <dt className="text-muted">Item</dt>
                    <dd className="text-right">
                      {i.name} x{i.quantity}
                    </dd>
                  </div>
                ))}
                <div className="flex justify-between gap-2 border-t border-line pt-2">
                  <dt className="text-muted">Amount</dt>
                  <dd className="num font-semibold">{o.paypal.amount} USD</dd>
                </div>
              </dl>
            </Card>
            {o.signed && charged !== null && signedTotal !== null && (
              <Card className={charged !== signedTotal ? "border-crimson/50" : ""}>
                <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">Against the merchant&apos;s signed price</h2>
                <dl className="mt-3 space-y-1 text-sm">
                  <div className="flex justify-between">
                    <dt className="text-muted">Charged by the agent</dt>
                    <dd className="num">{charged.toFixed(2)}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-muted">Signed price + shipping + tax</dt>
                    <dd className="num">{signedTotal.toFixed(2)}</dd>
                  </div>
                  {budget !== null && (
                    <div className="flex justify-between">
                      <dt className="text-muted">Your budget</dt>
                      <dd className={`num ${signedTotal > budget ? "font-semibold text-crimson" : ""}`}>{budget.toFixed(2)}</dd>
                    </div>
                  )}
                </dl>
                <p className="mt-3 text-sm">
                  {charged !== signedTotal
                    ? "The model typed the amount itself. It matches nothing the merchant signed"
                    : "The amount happens to match the signed total"}
                  {budget !== null && signedTotal > budget ? ", and the real cost is over your budget." : "."}
                </p>
                <p className="mt-2 text-xs text-muted">
                  Nothing in this order is traceable to your signed intent or the merchant&apos;s signed catalog. Provenant
                  derives the total from signed values and blocks anything else.
                </p>
              </Card>
            )}
            {run.attack && (
              <p className="text-xs text-muted">
                Planted for this run: <span className="num">{run.attack.id}</span> on {run.attack.host}&apos;s {run.attack.surface}.
              </p>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}
