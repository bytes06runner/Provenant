"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type CaseRow } from "@/lib/api";

const STATUS: Record<string, string> = {
  remedied: "Remedy carried out",
  awaiting_approval: "Awaiting approval",
  human_review: "With a human reviewer",
  no_remedy: "No remedy owed",
  in_progress: "Under review",
  running: "Replaying",
};
const pct = (x?: string) => (x == null ? "" : `${Math.round(Number(x) * 100)}%`);

export default function Cases() {
  const [rows, setRows] = useState<CaseRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .cases()
      .then((r) => setRows(r.cases))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  return (
    <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
      <h1 className="display text-4xl sm:text-5xl">Recourse</h1>
      <p className="mt-3 text-muted">Every complaint becomes a case with its own hash-chained record and a ruling.</p>
      {error && <p className="mt-4 rounded-lg bg-crimson/10 px-3 py-2 text-sm text-crimson">{error}</p>}
      <ul className="mt-6 space-y-3">
        {rows?.map((c) => (
          <li key={c.case_id}>
            <Link href={`/cases/${c.case_id}`} className="block rounded-xl border border-line px-5 py-4 hover:bg-surface">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-medium">
                  {c.purchase ? `${c.purchase.merchant_id} ${c.purchase.sku}` : c.case_id}
                </span>
                <span className="text-sm text-muted">{STATUS[c.status] ?? c.status}</span>
              </div>
              {c.complaint && <p className="mt-1 text-sm italic text-muted">&ldquo;{c.complaint}&rdquo;</p>}
              {c.shares && (
                <p className="num mt-2 text-xs text-muted">
                  user {pct(c.shares.U)} · merchant {pct(c.shares.M)} · agent {pct(c.shares.A)}
                </p>
              )}
            </Link>
          </li>
        ))}
        {rows?.length === 0 && <li className="text-muted">No complaints yet.</li>}
      </ul>
    </div>
  );
}
