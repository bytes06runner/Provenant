"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Card, Hash, Meander, WaxSeal } from "@/components/ui";
import { API_URL, api, type Attribution, type CaseView } from "@/lib/api";

const COALITION: Record<string, string> = {
  observed: "As it happened",
  "do(U)": "If your request had been clear",
  "do(M)": "If the merchant's content had been accurate",
  "do(A)": "If a stricter agent had chosen",
  "do(U,M)": "Clear request and accurate merchant",
  "do(U,A)": "Clear request and stricter agent",
  "do(M,A)": "Accurate merchant and stricter agent",
  "do(U,M,A)": "All three corrected",
};
const PARTY: Record<"U" | "M" | "A", string> = { U: "You (the request)", M: "The merchant", A: "The agent" };
const PARTY_COLOR: Record<"U" | "M" | "A", string> = { U: "var(--accent)", M: "var(--laurel)", A: "var(--slate)" };
const STATUS: Record<string, string> = {
  remedied: "Remedy carried out",
  awaiting_approval: "Remedy proposed, awaiting operator approval",
  human_review: "Sent to a human reviewer",
  no_remedy: "No remedy owed",
  in_progress: "Under review",
  running: "Replaying the decision",
};
const ORDER = Object.keys(COALITION);

function intent(clarified: Record<string, unknown>): string {
  const req = (clarified.required_attributes ?? {}) as Record<string, string>;
  const forb = (clarified.forbidden_attributes ?? {}) as Record<string, string[]>;
  const parts = Object.entries(req).map(([k, v]) => `${k} ${v}`);
  const never = Object.entries(forb).flatMap(([k, vs]) => vs.map((v) => `${k} ${v}`));
  return [parts.length ? `Required: ${parts.join(", ")}` : "", never.length ? `never: ${never.join(", ")}` : ""]
    .filter(Boolean)
    .join("; ");
}

const pct = (x: string | number) => `${(Number(x) * 100).toFixed(0)}%`;

function Shares({ a }: { a: Attribution }) {
  if (!a.shares) return <p className="text-sm text-muted">{a.reason}</p>;
  return (
    <div className="space-y-4">
      {(["U", "M", "A"] as const).map((p) => {
        const share = Number(a.shares![p]);
        const [lo, hi] = a.ci[p].map(Number);
        return (
          <div key={p}>
            <div className="flex items-baseline justify-between text-sm">
              <span>{PARTY[p]}</span>
              <span className="num">
                {pct(share)} <span className="text-xs text-muted">({pct(lo)} to {pct(hi)})</span>
              </span>
            </div>
            <div className="relative mt-1.5 h-3 rounded-full bg-surface-2">
              <div className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${share * 100}%`, background: PARTY_COLOR[p] }} />
              <div
                className="absolute top-1/2 h-4 -translate-y-1/2 border-x-2 border-text/60"
                style={{ left: `${lo * 100}%`, width: `${Math.max(0.5, (hi - lo) * 100)}%` }}
                title={`${Number(a.ci_level) * 100}% interval`}
              />
            </div>
          </div>
        );
      })}
      <p className="text-xs text-muted">
        Bars show each party&apos;s share of fault; brackets show the {pct(a.ci_level)} interval ({a.interval ?? "jeffreys"}{" "}
        posteriors, k = {a.k} replays per scenario). {a.escalate ? a.reason + "." : ""}
      </p>
    </div>
  );
}

export default function CasePage() {
  const { id } = useParams<{ id: string }>();
  const [c, setC] = useState<CaseView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    async function loop() {
      while (!stop) {
        try {
          const v = await api.case(id);
          setC(v);
          setError(null);
          if (!v.running && v.status !== "in_progress") break;
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
    loop();
    return () => {
      stop = true;
    };
  }, [id]);

  if (!c) {
    return <div className="mx-auto max-w-4xl px-4 py-10 text-muted sm:px-6">{error ?? "Opening the case..."}</div>;
  }
  const status = c.running === "running" ? "running" : c.status;
  const ruling = c.ruling?.ruling;
  const findings = ruling?.findings ?? [];
  const a = c.attribution;

  return (
    <article className="mx-auto max-w-4xl px-4 py-10 sm:px-6">
      <p className="text-xs uppercase tracking-[0.2em] text-muted">
        Case <span className="num normal-case tracking-normal">{c.case_id}</span>
      </p>
      <h1 className="display mt-2 text-4xl leading-tight sm:text-6xl">
        Ruling on order {c.order_id ? c.order_id.slice(0, 6) + "…" : "pending"}
      </h1>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <span className="rounded-full border border-line px-3 py-1 text-sm">{STATUS[status] ?? status}</span>
        {c.revised && <span className="text-xs text-muted">Revised from the recorded replays with the current interval method.</span>}
      </div>
      {c.running && c.running !== "running" && <p className="mt-3 rounded-lg bg-crimson/10 px-3 py-2 text-sm text-crimson">{c.running}</p>}

      {c.purchase && (
        <p className="mt-6 text-muted">
          Purchase of <span className="text-text">{c.purchase.merchant_id}</span> <span className="num text-text">{c.purchase.sku}</span> for{" "}
          <span className="num text-text">{c.purchase.total} USD</span>.
        </p>
      )}

      <Meander />

      <section>
        <h2 className="display text-2xl">I. The complaint</h2>
        <blockquote className="mt-3 border-l-2 border-accent pl-4 italic">{c.complaint.text}</blockquote>
        <div className="mt-3 flex flex-wrap gap-6 text-sm">
          {Object.keys(c.complaint.clarified ?? {}).length > 0 && (
            <div>
              <div className="text-xs uppercase tracking-wide text-muted">Clarified intent (signed)</div>
              <div className="mt-1">{intent(c.complaint.clarified)}</div>
            </div>
          )}
          {Object.keys(c.complaint.reported_attributes ?? {}).length > 0 && (
            <div>
              <div className="text-xs uppercase tracking-wide text-muted">What arrived (confirmed by you)</div>
              <div className="mt-1">
                {Object.entries(c.complaint.reported_attributes).map(([k, v]) => `${k}: ${v}`).join(", ")}
              </div>
            </div>
          )}
          {c.complaint.photo_blob && (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={`${API_URL}/api/cases/${c.case_id}/photo`} alt="Photo of what arrived" className="h-24 rounded-lg border border-line" />
          )}
        </div>
      </section>

      <section className="mt-10">
        <h2 className="display text-2xl">II. Findings</h2>
        {findings.length ? (
          <ol className="mt-3 list-decimal space-y-2 pl-6">
            {findings.map((f, i) => (
              <li key={i}>{f}</li>
            ))}
          </ol>
        ) : (
          <p className="mt-3 text-muted">The facts are still being established.</p>
        )}
      </section>

      {a && Object.keys(a.v).length > 0 && (
        <section className="mt-10">
          <h2 className="display text-2xl">III. What would have happened</h2>
          <p className="mt-2 text-sm text-muted">
            The purchase was replayed from the recorded inputs with each party corrected. Each row is the chance of a
            wrong purchase.
          </p>
          <div className="mt-4 overflow-x-auto rounded-xl border border-line">
            <table className="w-full min-w-[560px] text-sm">
              <thead className="bg-surface text-left text-xs uppercase tracking-wide text-muted">
                <tr>
                  <th className="px-4 py-2 font-medium">Scenario</th>
                  <th className="px-4 py-2 text-right font-medium">Wrong purchase</th>
                  <th className="px-4 py-2 text-right font-medium">Interval</th>
                  <th className="px-4 py-2 text-right font-medium">Replays</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(a.v)
                  .sort(([x], [y]) => ORDER.indexOf(x) - ORDER.indexOf(y))
                  .map(([k, v]) => (
                  <tr key={k} className="border-t border-line">
                    <td className="px-4 py-2">
                      {COALITION[k] ?? k} <span className="num ml-1 text-xs text-muted">{k}</span>
                    </td>
                    <td className="num px-4 py-2 text-right">{pct(v)}</td>
                    <td className="num px-4 py-2 text-right text-muted">
                      {a.v_ci?.[k] ? `${pct(a.v_ci[k][0])} to ${pct(a.v_ci[k][1])}` : "n/a"}
                    </td>
                    <td className="num px-4 py-2 text-right text-muted">
                      {c.replays[k] ? `${c.replays[k].bad}/${c.replays[k].k}` : "n/a"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="mt-10">
        <h2 className="display text-2xl">{a && Object.keys(a.v).length > 0 ? "IV." : "III."} Fault</h2>
        <div className="mt-4">{a ? <Shares a={a} /> : <p className="text-muted">{c.attribution_note ?? "Not yet attributed."}</p>}</div>
      </section>

      {ruling && (
        <section className="mt-10">
          <h2 className="display text-2xl">Holding</h2>
          <p className="mt-3 text-lg">{ruling.holding}</p>
          <p className="mt-2 text-xs text-muted">
            Written from the computed numbers only ({c.ruling?.source === "deterministic" ? "deterministic text" : c.ruling?.source});
            every number was checked against the computed result.
          </p>
        </section>
      )}

      <section className="mt-10">
        <h2 className="display text-2xl">Remedy ordered</h2>
        {c.remedy ? (
          <Card className="mt-3">
            {c.remedy.actions.length ? (
              <ul className="space-y-2">
                {c.remedy.actions.map((x, i) => (
                  <li key={i} className="flex flex-wrap justify-between gap-2">
                    <span>
                      <span className="font-medium capitalize">{x.kind}</span> {x.amount ? <span className="num">{x.amount} USD</span> : null}{" "}
                      <span className="text-muted">from the {x.party === "operator" ? "agent liability pool" : x.party}</span>
                    </span>
                    <span className="text-xs text-muted">{x.reason}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p>No money moves.</p>
            )}
            {Number(c.remedy.absorbed_by_user) > 0 && (
              <p className="mt-3 text-sm text-muted">
                Absorbed by you, for your share: <span className="num">{c.remedy.absorbed_by_user} USD</span>
              </p>
            )}
            {c.remedy.notes.map((n) => (
              <p key={n} className="mt-2 text-sm text-muted">
                {n}
              </p>
            ))}
          </Card>
        ) : (
          <p className="mt-3 text-muted">Pending.</p>
        )}

        {c.executed.length > 0 && (
          <div className="mt-4 space-y-2">
            {c.executed.map((x) => {
              const final = [...c.reconciliation].reverse().find((r) => r.resource_id === x.resource_id);
              return (
                <div key={x.resource_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-laurel/40 bg-laurel/10 px-4 py-2 text-sm">
                  <span>
                    PayPal {x.kind} <Hash value={x.resource_id} n={17} /> {x.amount ? <span className="num">{x.amount} USD</span> : null}
                  </span>
                  <span className="text-xs">{final ? final.to : x.status}</span>
                </div>
              );
            })}
          </div>
        )}
        {status === "awaiting_approval" && (
          <p className="mt-3 text-sm text-muted">
            Money moves only after an operator approves it in the <Link href="/console" className="text-accent hover:underline">console</Link>.
          </p>
        )}
      </section>

      <Meander />

      <footer className="flex flex-wrap items-center justify-between gap-6">
        <WaxSeal title="Case chain head" value={c.chain_head} tone="slate" />
        {c.custom_id && <WaxSeal title="PayPal custom_id" value={c.custom_id} />}
        {c.evidence_pack && (
          <a href={`${API_URL}/api/cases/${c.case_id}/evidence.pdf`} target="_blank" rel="noreferrer" className="rounded-lg border border-line px-4 py-2 text-sm hover:bg-surface">
            Evidence pack (PDF)
          </a>
        )}
      </footer>
    </article>
  );
}
