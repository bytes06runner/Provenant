"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Hash } from "@/components/ui";
import { api, type OrderRow } from "@/lib/api";

const KIND: Record<string, string> = {
  authorizations: "Authorization",
  captures: "Capture",
  refunds: "Refund",
};

export default function Orders() {
  const [rows, setRows] = useState<OrderRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .orders()
      .then((r) => setRows(r.orders))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      <h1 className="display text-4xl sm:text-5xl">Orders</h1>
      <p className="mt-3 text-muted">Live status from PayPal for every order Provenant created. Newest first.</p>
      {error && <p className="mt-4 rounded-lg bg-crimson/10 px-3 py-2 text-sm text-crimson">{error}</p>}
      {rows === null && !error && <p className="mt-8 text-sm text-muted">Asking PayPal...</p>}
      {rows && (
        <div className="mt-6 overflow-x-auto rounded-xl border border-line">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead className="bg-surface text-xs uppercase tracking-wide text-muted">
              <tr>
                <th className="px-4 py-3 font-medium">Order</th>
                <th className="px-4 py-3 font-medium">Merchant</th>
                <th className="px-4 py-3 font-medium">Item</th>
                <th className="px-4 py-3 text-right font-medium">Amount</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Payments</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.order_id} className="border-t border-line align-top">
                  <td className="px-4 py-3">
                    <Hash value={r.order_id} n={17} />
                    <div className="mt-1">
                      <Link href={`/purchase/${r.session}/provenance`} className="text-xs text-accent hover:underline">
                        provenance
                      </Link>
                    </div>
                  </td>
                  <td className="px-4 py-3">{r.merchant_id}</td>
                  <td className="px-4 py-3 text-muted">{r.items?.join(", ") || "n/a"}</td>
                  <td className="num px-4 py-3 text-right">
                    {r.amount ?? "n/a"} {r.currency ?? ""}
                  </td>
                  <td className="px-4 py-3">
                    <span className="rounded-full border border-line px-2 py-0.5 text-xs">{r.status}</span>
                  </td>
                  <td className="px-4 py-3 text-xs">
                    {Object.entries(r.payments ?? {}).flatMap(([k, list]) =>
                      list.map((p) => (
                        <div key={p.id} className="flex items-center gap-1">
                          <span className="text-muted">{KIND[k] ?? k}</span>
                          <Hash value={p.id} n={17} />
                          <span className="text-muted">{p.status}</span>
                        </div>
                      )),
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
