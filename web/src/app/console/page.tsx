"use client";

import type { ColDef, ICellRendererParams } from "ag-grid-community";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Grid } from "@/components/grid";
import { Button } from "@/components/ui";
import { api, type CaseRow, type EvalRow, type EvalSummary, type OrderRow } from "@/lib/api";

type Tab = "queue" | "ledger" | "eval";

type LedgerRow = OrderRow & {
  authorization?: string;
  capture?: string;
  refunds?: string;
  case_id?: string;
  case_status?: string;
  payouts?: string;
};

const STATUS: Record<string, string> = {
  remedied: "Remedied",
  awaiting_approval: "Awaiting approval",
  human_review: "Human review",
  no_remedy: "No remedy",
  in_progress: "In progress",
  running: "Replaying",
};

const pct = (x?: string | number | null) => (x == null ? "" : `${Math.round(Number(x) * 100)}%`);

export default function Console() {
  const [tab, setTab] = useState<Tab>("queue");
  const [cases, setCases] = useState<CaseRow[]>([]);
  const [orders, setOrders] = useState<OrderRow[]>([]);
  const [evalData, setEvalData] = useState<{ summary: EvalSummary; rows: EvalRow[] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [c, o, e] = await Promise.allSettled([api.cases(), api.orders(), api.evalAttribution()]);
    if (c.status === "fulfilled") setCases(c.value.cases);
    if (o.status === "fulfilled") setOrders(o.value.orders);
    if (e.status === "fulfilled") setEvalData(e.value);
  }, []);

  useEffect(() => {
    const t = setTimeout(load, 0); // first load after mount; reloads follow approvals
    return () => clearTimeout(t);
  }, [load]);

  const approve = useCallback(
    async (caseId: string) => {
      setBusy(caseId);
      setMessage(null);
      try {
        const r = await api.approve(caseId, "operator");
        setMessage(`Approved ${caseId}: ${r.executed.map((x) => `${x.kind} ${x.resource_id} ${x.status}`).join(", ")}`);
        await load();
      } catch (e) {
        setMessage(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  const queueCols = useMemo<ColDef<CaseRow>[]>(
    () => [
      {
        field: "case_id",
        headerName: "Case",
        minWidth: 170,
        cellRenderer: (p: ICellRendererParams<CaseRow>) => (
          <Link href={`/cases/${p.value}`} className="num text-accent hover:underline">
            {p.value}
          </Link>
        ),
      },
      { field: "status", headerName: "Status", valueFormatter: (p) => STATUS[p.value] ?? p.value, minWidth: 140 },
      { headerName: "Purchase", valueGetter: (p) => (p.data?.purchase ? `${p.data.purchase.merchant_id} ${p.data.purchase.sku}` : ""), minWidth: 150 },
      { headerName: "User", valueGetter: (p) => pct(p.data?.shares?.U), maxWidth: 90 },
      { headerName: "Merchant", valueGetter: (p) => pct(p.data?.shares?.M), maxWidth: 100 },
      { headerName: "Agent", valueGetter: (p) => pct(p.data?.shares?.A), maxWidth: 90 },
      {
        headerName: "Proposed",
        minWidth: 180,
        valueGetter: (p) =>
          p.data?.remedy?.actions.map((a) => `${a.kind}${a.amount ? " " + a.amount : ""}`).join(", ") ||
          (p.data?.remedy?.status === "needs_human_review" ? "human review" : "none"),
      },
      { headerName: "PayPal ids", minWidth: 200, valueGetter: (p) => p.data?.executed.map((x) => x.resource_id).join(", ") },
      {
        headerName: "",
        minWidth: 130,
        sortable: false,
        filter: false,
        cellRenderer: (p: ICellRendererParams<CaseRow>) =>
          p.data?.status === "awaiting_approval" ? (
            <Button className="px-3 py-1 text-xs" disabled={busy !== null} onClick={() => approve(p.data!.case_id)}>
              {busy === p.data.case_id ? "Approving..." : "Approve"}
            </Button>
          ) : null,
      },
    ],
    [approve, busy],
  );

  const ledger = useMemo<LedgerRow[]>(() => {
    const byOrder = new Map(cases.filter((c) => c.order_id).map((c) => [c.order_id!, c]));
    return orders.map((o) => {
      const c = byOrder.get(o.order_id);
      const ids = (k: string) => (o.payments?.[k] ?? []).map((x) => `${x.id} ${x.status}`).join(", ");
      return {
        ...o,
        authorization: ids("authorizations"),
        capture: ids("captures"),
        refunds: ids("refunds"),
        case_id: c?.case_id,
        case_status: c ? STATUS[c.status] ?? c.status : "",
        payouts: c?.executed.filter((x) => x.kind === "payout").map((x) => `${x.resource_id} ${x.status}`).join(", "),
      };
    });
  }, [cases, orders]);

  const ledgerCols = useMemo<ColDef<LedgerRow>[]>(
    () => [
      { field: "created_at", headerName: "Created", valueFormatter: (p) => (p.value ? new Date(p.value).toLocaleString() : ""), minWidth: 170, sort: "desc" },
      { field: "merchant_id", headerName: "Merchant", minWidth: 110 },
      { field: "order_id", headerName: "Order", minWidth: 170, cellClass: "num" },
      { field: "amount", headerName: "Amount", type: "rightAligned", maxWidth: 110 },
      { field: "status", headerName: "Order status", minWidth: 130 },
      { field: "authorization", headerName: "Authorization", minWidth: 220 },
      { field: "capture", headerName: "Capture", minWidth: 220 },
      { field: "refunds", headerName: "Refunds", minWidth: 220 },
      { field: "payouts", headerName: "Payouts", minWidth: 200 },
      { field: "case_status", headerName: "Case", minWidth: 140 },
      { field: "custom_id", headerName: "custom_id", minWidth: 260, cellClass: "num" },
      { field: "session", headerName: "Recorder session", minWidth: 150, cellClass: "num" },
    ],
    [],
  );

  const evalCols = useMemo<ColDef<EvalRow>[]>(
    () => [
      { field: "at", headerName: "When", valueFormatter: (p) => (p.value ? new Date(p.value).toLocaleString() : ""), minWidth: 170, sort: "desc" },
      { field: "instance", headerName: "Instance", minWidth: 180, cellClass: "num" },
      { field: "kind", headerName: "Planted fault", minWidth: 130 },
      { field: "valid", headerName: "Valid", maxWidth: 90 },
      { field: "correct", headerName: "Correct", maxWidth: 100 },
      { field: "mae", headerName: "MAE", maxWidth: 90 },
      { headerName: "Shares U / M / A", minWidth: 160, valueGetter: (p) => (p.data?.shares ? `${pct(p.data.shares.U)} / ${pct(p.data.shares.M)} / ${pct(p.data.shares.A)}` : "") },
      { field: "budget_tokens", headerName: "Tokens", maxWidth: 110, type: "rightAligned" },
      { field: "invalid", headerName: "Why invalid", minWidth: 220 },
    ],
    [],
  );

  const kinds = ["pure_user", "pure_merchant", "pure_agent", "mixed"];

  return (
    <div className="mx-auto max-w-7xl px-4 py-10 sm:px-6">
      <h1 className="display text-4xl sm:text-5xl">Operations</h1>
      <p className="mt-3 text-muted">Recourse queue, the PayPal ledger joined with the Flight Recorder, and the evaluation.</p>

      <div className="mt-6 flex gap-1 border-b border-line" role="tablist">
        {(
          [
            ["queue", "Recourse queue"],
            ["ledger", "Ledger"],
            ["eval", "Evaluation"],
          ] as [Tab, string][]
        ).map(([t, label]) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-4 py-2 text-sm ${tab === t ? "border-accent text-text" : "border-transparent text-muted hover:text-text"}`}
          >
            {label}
          </button>
        ))}
      </div>

      {message && <p className="mt-4 rounded-lg border border-line bg-surface px-3 py-2 text-sm">{message}</p>}

      <div className="mt-6">
        {tab === "queue" && (
          <>
            <p className="mb-3 text-sm text-muted">
              Money moves only when an operator approves a proposed remedy (AUTO_REMEDY is off). Approving twice never
              moves money twice.
            </p>
            <Grid rows={cases} columns={queueCols} />
          </>
        )}
        {tab === "ledger" && <Grid rows={ledger} columns={ledgerCols} height={480} />}
        {tab === "eval" && (
          <div className="space-y-6">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {kinds.map((k) => {
                const s = evalData?.summary[k];
                return (
                  <div key={k} className="rounded-xl border border-line p-4">
                    <div className="text-xs uppercase tracking-wide text-muted">{k.replace("_", " ")}</div>
                    <div className="display mt-2 text-4xl">{s?.accuracy != null ? pct(s.accuracy) : "n/a"}</div>
                    <div className="mt-1 text-xs text-muted">
                      majority correct, {s?.valid ?? 0} valid of {s?.instances ?? 0}
                    </div>
                    {s?.mean_abs_error != null && <div className="mt-1 text-xs text-muted">mean abs. error {s.mean_abs_error}</div>}
                    {s?.interval_coverage != null && <div className="text-xs text-muted">interval coverage {pct(s.interval_coverage)}</div>}
                  </div>
                );
              })}
            </div>
            <p className="text-sm text-muted">
              Planted-fault scenarios run nightly within the daily model budget (config/eval/attribution.yaml). Each row is a
              real purchase through Lineage and a real Blackbox replay.
            </p>
            <Grid rows={evalData?.rows ?? []} columns={evalCols} height={380} />
          </div>
        )}
      </div>
    </div>
  );
}
