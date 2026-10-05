"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Button, Card, LabelChip } from "@/components/ui";
import { api, type Meta, type SessionInfo } from "@/lib/api";

const NAMES: Record<string, string> = { color: "Color", size_us: "US size", material: "Material", waterproof: "Waterproof" };
const select = "rounded-md border border-line bg-bg px-2 py-1 text-sm outline-none focus:border-accent";

function readFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

export default function Report() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [info, setInfo] = useState<SessionInfo | null>(null);
  const [text, setText] = useState("");
  const [needed, setNeeded] = useState<Record<string, string>>({});
  const [avoid, setAvoid] = useState<Record<string, string>>({});
  const [arrived, setArrived] = useState<Record<string, string>>({});
  const [photo, setPhoto] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.meta().then(setMeta).catch(() => setError("The Provenant API is not reachable."));
    api.session(id).then(setInfo).catch(() => undefined);
  }, [id]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const clarify: Record<string, string> = {};
    for (const [k, v] of Object.entries(needed)) if (v) clarify[`required.${k}`] = v;
    for (const [k, v] of Object.entries(avoid)) if (v) clarify[`forbidden.${k}`] = v;
    const report = Object.fromEntries(Object.entries(arrived).filter(([, v]) => v));
    try {
      const { case_id } = await api.complain(id, { text, clarify, report, photo_base64: photo });
      router.push(`/cases/${case_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Filing the complaint failed.");
      setBusy(false);
    }
  }

  const attrs = meta ? Object.keys(meta.vocabulary) : [];
  const chosen = info?.chosen;

  return (
    <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
      <Link href={`/purchase/${id}`} className="text-sm text-muted hover:text-text">
        Back to the purchase
      </Link>
      <h1 className="display mt-2 text-4xl sm:text-5xl">Report a problem</h1>
      <p className="mt-3 text-muted">
        Tell us what went wrong. Blackbox checks the facts against the signed record, replays the decision with
        each party corrected, and proposes a remedy on PayPal rails.
      </p>
      {chosen && (
        <p className="mt-4 text-sm">
          Order for <span className="font-medium">{chosen.merchant_id}</span> <span className="num">{chosen.sku}</span>,{" "}
          <span className="num">{chosen.total} USD</span>
        </p>
      )}

      <form onSubmit={submit} className="mt-6 space-y-6">
        <Card>
          <label className="block">
            <span className="text-sm font-medium">What went wrong</span>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={3}
              required
              placeholder="The shoes leak in the rain. The listing said they were waterproof, and I needed waterproof shoes."
              className="mt-2 w-full rounded-lg border border-line bg-bg px-3 py-2 outline-none focus:border-accent"
            />
          </label>
        </Card>

        <Card>
          <div className="flex items-baseline justify-between gap-2">
            <h2 className="text-sm font-semibold">What you actually needed</h2>
            <LabelChip label="USER" />
          </div>
          <p className="mt-1 text-xs text-muted">
            Your clarified intent. It is signed like your original mandate and used to judge the purchase.
          </p>
          <div className="mt-3 space-y-2">
            {attrs.map((a) => (
              <div key={a} className="flex flex-wrap items-center gap-2 text-sm">
                <span className="w-28 text-muted">{NAMES[a] ?? a}</span>
                <select className={select} value={needed[a] ?? ""} onChange={(e) => setNeeded({ ...needed, [a]: e.target.value })}>
                  <option value="">as I asked</option>
                  {meta!.vocabulary[a].map((v) => (
                    <option key={v} value={v}>
                      must be {v}
                    </option>
                  ))}
                </select>
                <select className={select} value={avoid[a] ?? ""} onChange={(e) => setAvoid({ ...avoid, [a]: e.target.value })}>
                  <option value="">nothing to avoid</option>
                  {meta!.vocabulary[a].map((v) => (
                    <option key={v} value={v}>
                      never {v}
                    </option>
                  ))}
                </select>
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <h2 className="text-sm font-semibold">What arrived</h2>
          <p className="mt-1 text-xs text-muted">Only what you can confirm yourself. Leave the rest blank.</p>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            {attrs.map((a) => (
              <label key={a} className="flex items-center gap-2 text-sm">
                <span className="w-28 text-muted">{NAMES[a] ?? a}</span>
                <select className={select} value={arrived[a] ?? ""} onChange={(e) => setArrived({ ...arrived, [a]: e.target.value })}>
                  <option value="">not sure</option>
                  {meta!.vocabulary[a].map((v) => (
                    <option key={v} value={v}>
                      {v}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>
          <label className="mt-4 block text-sm">
            <span className="text-muted">Photo of what arrived (optional)</span>
            <input
              type="file"
              accept="image/png,image/jpeg"
              className="mt-2 block text-sm"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                setPhoto(f ? await readFile(f) : null);
              }}
            />
          </label>
          {photo && (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={photo} alt="What arrived" className="mt-3 h-32 rounded-lg border border-line object-contain" />
          )}
          <p className="mt-2 text-xs text-muted">
            A photo is read by a vision model as <LabelChip label="UNTRUSTED" className="mx-1" /> evidence. It can corroborate
            the shipment record, never overrule it on its own.
          </p>
        </Card>

        {error && <p className="rounded-lg bg-crimson/10 px-3 py-2 text-sm text-crimson">{error}</p>}
        <div className="flex justify-end">
          <Button type="submit" disabled={busy || !text.trim()}>
            {busy ? "Filing..." : "File the complaint"}
          </Button>
        </div>
      </form>
    </div>
  );
}
