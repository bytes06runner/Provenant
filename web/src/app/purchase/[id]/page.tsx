"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { PayPalApprove } from "@/components/paypal-approve";
import { Button, Card, Hash, LabelChip, WaxSeal } from "@/components/ui";
import { api, type Meta, type SessionInfo, type Step } from "@/lib/api";

const TERMINAL = new Set(["proposed", "blocked", "failed", "ordered", "authorized", "archived"]);

const STATE_TEXT: Record<string, string> = {
  drafted: "Mandate drafted",
  signed: "Signed, starting",
  running: "Agent running",
  proposed: "Checkout proposed",
  blocked: "Blocked by contract",
  failed: "Run failed",
  ordered: "Waiting for your PayPal approval",
  authorized: "Payment authorized",
  archived: "Recorded session",
};

export default function PurchaseRun() {
  const { id } = useParams<{ id: string }>();
  const [steps, setSteps] = useState<Step[]>([]);
  const [info, setInfo] = useState<SessionInfo | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [showCalls, setShowCalls] = useState(false);
  const [ordering, setOrdering] = useState(false);
  const [auth, setAuth] = useState<{ authorization_id: string; status: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const last = useRef(-1);

  const refresh = useCallback(async () => {
    const ev = await api.events(id, last.current);
    if (ev.steps.length) {
      last.current = Math.max(last.current, ev.steps[ev.steps.length - 1].seq);
      // Merge by sequence number: overlapping polls (and dev double effects) never duplicate.
      setSteps((prev) => {
        const bySeq = new Map(prev.map((s) => [s.seq, s]));
        for (const s of ev.steps) bySeq.set(s.seq, s);
        return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
      });
    }
    const s = await api.session(id);
    setInfo(s);
    return s.state;
  }, [id]);

  useEffect(() => {
    api.meta().then(setMeta).catch(() => undefined);
    let stop = false;
    async function loop() {
      while (!stop) {
        try {
          const state = await refresh();
          if (TERMINAL.has(state)) break;
        } catch {
          setError("Lost contact with the Provenant API. Retrying...");
        }
        await new Promise((r) => setTimeout(r, 700));
      }
    }
    loop();
    return () => {
      stop = true;
    };
  }, [refresh]);

  const onAuthorized = useCallback(
    (r: { authorization_id: string; status: string }) => {
      setAuth(r);
      refresh();
    },
    [refresh],
  );

  async function createOrder() {
    setOrdering(true);
    setError(null);
    try {
      await api.order(id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Creating the PayPal order failed.");
    } finally {
      setOrdering(false);
    }
  }

  const visible = steps.filter((s) => showCalls || (s.type !== "llm.call" && !s.type.startsWith("llm.")));
  const state = info?.state ?? "running";
  const chosen = info?.chosen;

  return (
    <div className="mx-auto grid max-w-6xl gap-8 px-4 py-10 sm:px-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="min-w-0">
        <p className="text-xs uppercase tracking-[0.2em] text-muted">
          Purchase <span className="num normal-case tracking-normal">{id}</span>
        </p>
        <h1 className="display mt-2 text-4xl sm:text-5xl">{STATE_TEXT[state] ?? state}</h1>
        {info?.error && <p className="mt-3 rounded-lg bg-crimson/10 px-3 py-2 text-sm text-crimson">{info.error}</p>}
        {error && <p className="mt-3 rounded-lg bg-crimson/10 px-3 py-2 text-sm text-crimson">{error}</p>}

        <div className="mt-6 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">What the agent did</h2>
          <label className="flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={showCalls} onChange={(e) => setShowCalls(e.target.checked)} />
            Show model calls
          </label>
        </div>
        <ol className="mt-3 space-y-2" aria-live="polite">
          {visible.map((s) => (
            <li key={s.seq} className="rounded-lg border border-line bg-surface/40 px-4 py-3">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-sm font-medium">
                    <span className="num mr-2 text-xs text-muted">{String(s.seq).padStart(2, "0")}</span>
                    {s.title}
                  </div>
                  {s.detail && <div className="mt-1 break-words text-sm text-muted">{s.detail}</div>}
                </div>
                <LabelChip label={s.label} />
              </div>
              {s.type === "contract.blocked" && (
                <div className="mt-2 text-xs text-crimson">The order builder refused it. Nothing was sent to PayPal.</div>
              )}
            </li>
          ))}
          {!TERMINAL.has(state) && <li className="px-4 py-3 text-sm text-muted">Working...</li>}
        </ol>
      </div>

      <aside className="space-y-4 lg:sticky lg:top-6 lg:self-start">
        <Card>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">Seals</h2>
          <div className="mt-4 space-y-4">
            <WaxSeal title="Your signed mandate" value={info?.mandate_seal} />
            {info?.manifest_seal && <WaxSeal title="Merchant manifest seal" value={info.manifest_seal} tone="laurel" />}
            <WaxSeal title="Flight Recorder chain head" value={info?.chain_head} tone="slate" />
          </div>
        </Card>

        {chosen && (
          <Card>
            <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">Proposed checkout</h2>
            <div className="mt-3 text-lg font-semibold">
              {chosen.merchant_id} <span className="num text-base">{chosen.sku}</span>
            </div>
            <dl className="mt-2 space-y-1 text-sm">
              {Object.entries(chosen.attributes).map(([k, v]) => (
                <div key={k} className="flex justify-between">
                  <dt className="text-muted">{k}</dt>
                  <dd>{v}</dd>
                </div>
              ))}
              <div className="flex justify-between border-t border-line pt-2">
                <dt className="text-muted">Total</dt>
                <dd className="num font-semibold">{chosen.total} USD</dd>
              </div>
            </dl>
            {info?.decision_label === "UNTRUSTED" && (
              <p className="mt-3 text-xs text-muted">
                <LabelChip label="UNTRUSTED" className="mr-1" />
                Reviews influenced which compliant item was chosen. They could not touch the payee, item, price
                or address.
              </p>
            )}
            <Link href={`/purchase/${id}/provenance`} className="mt-4 block text-sm text-accent underline-offset-4 hover:underline">
              See where every field came from
            </Link>
            {state === "proposed" && info?.live && (
              <Button className="mt-4 w-full" onClick={createOrder} disabled={ordering}>
                {ordering ? "Creating the PayPal order..." : "Create PayPal order"}
              </Button>
            )}
          </Card>
        )}

        {info?.order && (
          <Card>
            <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">PayPal</h2>
            <dl className="mt-3 space-y-1 text-sm">
              <div className="flex justify-between gap-2">
                <dt className="text-muted">Order</dt>
                <dd>
                  <Hash value={info.order.id} n={17} />
                </dd>
              </div>
              <div className="flex justify-between gap-2">
                <dt className="text-muted">custom_id</dt>
                <dd>
                  <Hash value={info.order.custom_id} n={14} />
                </dd>
              </div>
            </dl>
            <div className="mt-4">
              {auth || state === "authorized" ? (
                <p className="text-sm">
                  Authorized{auth ? <> as <Hash value={auth.authorization_id} n={17} /></> : null}. The merchant
                  captures on shipment.
                </p>
              ) : (
                meta &&
                info.live && (
                  <PayPalApprove
                    sessionId={id}
                    orderId={info.order.id}
                    merchant={info.order.merchant_id}
                    approvalUrl={info.order.approval_url}
                    sdkUrl={meta.sdk_script_url}
                    presentationMode={meta.presentation_mode}
                    onAuthorized={onAuthorized}
                  />
                )
              )}
            </div>
          </Card>
        )}
      </aside>
    </div>
  );
}
