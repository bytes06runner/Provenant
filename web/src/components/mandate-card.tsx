"use client";

import { useMemo, useState } from "react";
import { Button, Card, LabelChip } from "@/components/ui";
import { api, ApiError, type Draft, type Fields, type Meta } from "@/lib/api";

const NAMES: Record<string, string> = {
  color: "Color",
  size_us: "US size",
  material: "Material",
  waterproof: "Waterproof",
};

function Row({ label, flagged, children, hint }: { label: string; flagged?: boolean; children: React.ReactNode; hint?: string }) {
  return (
    <div className={`grid gap-2 border-b border-line py-3 sm:grid-cols-[11rem_1fr] sm:items-center ${flagged ? "-mx-3 rounded-lg bg-accent/10 px-3" : ""}`}>
      <div className="text-sm">
        <span className="font-medium">{label}</span>
        {hint && <div className="text-xs text-muted">{hint}</div>}
      </div>
      <div className="flex flex-wrap items-center gap-2">{children}</div>
    </div>
  );
}

function Toggle({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      className={`rounded-full border px-3 py-1 text-xs ${on ? "border-accent bg-accent text-accent-ink" : "border-line hover:bg-surface"}`}
    >
      {children}
    </button>
  );
}

// Plain words for the extractor's problem messages, which name mandate fields.
const PLAIN: [string, string][] = [
  ["max_unit_price", "the most you will pay per item"],
  ["max_total", "the most you will pay in total"],
  ["ship_to_ref", "where to ship"],
  ["merchant_allowlist", "which merchants"],
  ["required_attributes", "a required value"],
  ["forbidden_attributes", "a value to avoid"],
];
function plain(p: string) {
  return PLAIN.reduce((acc, [k, v]) => acc.replaceAll(k, v), p);
}

const input = "rounded-md border border-line bg-bg px-2 py-1 text-sm outline-none focus:border-accent";

export function MandateCard({
  draft,
  meta,
  user,
  onSigned,
  onRestart,
}: {
  draft: Draft;
  meta: Meta;
  user: string;
  onSigned: (id: string) => void;
  onRestart: () => void;
}) {
  const [f, setF] = useState<Fields>(draft.fields);
  const [reviewSku, setReviewSku] = useState("");
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>(draft.problems);
  const [error, setError] = useState<string | null>(null);
  const addresses = meta.users.find((u) => u.id === user)?.addresses ?? [];
  const flagged = (key: string) => problems.some((p) => p.includes(key));
  const attrs = useMemo(() => Object.keys(meta.vocabulary), [meta]);

  function set<K extends keyof Fields>(key: K, value: Fields[K]) {
    setF((prev) => ({ ...prev, [key]: value }));
  }

  async function sign() {
    setBusy(true);
    setError(null);
    try {
      await api.confirm(draft.id, f);
      await api.run(draft.id, reviewSku.trim() || null);
      onSigned(draft.id);
    } catch (err) {
      if (err instanceof ApiError && err.body && typeof err.body === "object" && "problems" in err.body) {
        setProblems((err.body as { problems: string[] }).problems);
      } else {
        setError(err instanceof Error ? err.message : "Signing failed.");
      }
      setBusy(false);
    }
  }

  return (
    <Card className="mt-6">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="display text-3xl">Your mandate</h2>
        <span className="text-xs text-muted">
          Every field you confirm is labeled <LabelChip label="USER" className="ml-1" />
        </span>
      </div>
      <p className="mt-1 text-sm text-muted">Review and edit before you sign. Nothing outside these limits can be bought.</p>

      {(draft.questions.length > 0 || problems.length > 0) && (
        <div className="mt-4 space-y-2">
          {draft.questions.map((q) => (
            <p key={q} className="rounded-lg border border-accent/40 bg-accent/10 px-3 py-2 text-sm">
              <span className="font-medium">Question: </span>
              {q}
            </p>
          ))}
          {problems.map((p) => (
            <p key={p} className="rounded-lg border border-accent/40 bg-accent/10 px-3 py-2 text-sm">
              <span className="font-medium">Needs your answer: </span>
              {plain(p)}
            </p>
          ))}
        </div>
      )}

      <div className="mt-4">
        <Row label="Category">
          <span className="num text-sm">{f.category}</span>
        </Row>
        {attrs.map((a) => (
          <Row key={a} label={NAMES[a] ?? a} flagged={flagged(a)} hint="required value">
            <select
              value={f.required_attributes[a] ?? ""}
              onChange={(e) => {
                const next = { ...f.required_attributes };
                if (e.target.value) next[a] = e.target.value;
                else delete next[a];
                set("required_attributes", next);
              }}
              className={input}
            >
              <option value="">any</option>
              {meta.vocabulary[a].map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
            <span className="ml-2 text-xs text-muted">never:</span>
            {meta.vocabulary[a].map((v) => {
              const banned = f.forbidden_attributes[a] ?? [];
              const on = banned.includes(v);
              return (
                <Toggle
                  key={v}
                  on={on}
                  onClick={() => {
                    const nextList = on ? banned.filter((x) => x !== v) : [...banned, v];
                    const next = { ...f.forbidden_attributes };
                    if (nextList.length) next[a] = nextList;
                    else delete next[a];
                    set("forbidden_attributes", next);
                  }}
                >
                  {v}
                </Toggle>
              );
            })}
          </Row>
        ))}
        <Row label="Most per item" flagged={flagged("max_unit_price")}>
          <input
            className={`${input} num w-28`}
            inputMode="decimal"
            value={f.max_unit_price ?? ""}
            placeholder="0.00"
            onChange={(e) => set("max_unit_price", e.target.value || null)}
          />
          <span className="text-sm text-muted">{f.currency}</span>
        </Row>
        <Row label="Most in total" flagged={flagged("max_total")} hint="including shipping and tax">
          <input
            className={`${input} num w-28`}
            inputMode="decimal"
            value={f.max_total ?? ""}
            placeholder="0.00"
            onChange={(e) => set("max_total", e.target.value || null)}
          />
          <span className="text-sm text-muted">{f.currency}</span>
        </Row>
        <Row label="Quantity" flagged={flagged("quantity")}>
          <input
            className={`${input} num w-20`}
            type="number"
            min={1}
            value={f.quantity}
            onChange={(e) => set("quantity", Math.max(1, Number(e.target.value) || 1))}
          />
        </Row>
        <Row label="Choose by" flagged={flagged("preference")}>
          <select
            value={f.preference ?? ""}
            onChange={(e) => set("preference", (e.target.value || null) as Fields["preference"])}
            className={input}
          >
            <option value="">no preference</option>
            <option value="lowest_total">lowest total</option>
            <option value="best_reviewed">best reviewed</option>
          </select>
        </Row>
        <Row label="Merchants" flagged={flagged("merchant")} hint="none selected means any registered merchant">
          {meta.merchants.map((m) => {
            const list = f.merchant_allowlist ?? [];
            const on = list.includes(m);
            return (
              <Toggle
                key={m}
                on={on}
                onClick={() => {
                  const next = on ? list.filter((x) => x !== m) : [...list, m];
                  set("merchant_allowlist", next.length ? next : null);
                }}
              >
                {m}
              </Toggle>
            );
          })}
        </Row>
        <Row label="Ship to" flagged={flagged("ship_to") || flagged("address")}>
          <select value={f.ship_to_ref ?? ""} onChange={(e) => set("ship_to_ref", e.target.value || null)} className={input}>
            <option value="">choose a saved address</option>
            {addresses.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </Row>
        <Row label="Reviews to read" hint="optional, read as untrusted text">
          <input
            className={`${input} num w-56`}
            value={reviewSku}
            placeholder="northwind/NOR-001"
            onChange={(e) => setReviewSku(e.target.value)}
          />
        </Row>
      </div>

      {error && <p className="mt-4 rounded-lg bg-crimson/10 px-3 py-2 text-sm text-crimson">{error}</p>}
      <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
        <Button variant="ghost" type="button" onClick={onRestart} disabled={busy}>
          Start over
        </Button>
        <Button type="button" onClick={sign} disabled={busy}>
          {busy ? "Signing..." : "Confirm and sign"}
        </Button>
      </div>
    </Card>
  );
}
