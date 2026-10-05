"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { MandateCard } from "@/components/mandate-card";
import { Button, Card } from "@/components/ui";
import { api, ApiError, type Draft, type Meta } from "@/lib/api";

export default function NewPurchase() {
  const router = useRouter();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [user, setUser] = useState("");
  const [text, setText] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .meta()
      .then((m) => {
        setMeta(m);
        setUser(m.users[0]?.id ?? "");
      })
      .catch(() => setError("The Provenant API is not reachable. Start it with: uvicorn api.app:app --port 8700"));
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setDraft(await api.draft(user, text));
    } catch (err) {
      setError(err instanceof ApiError ? String(err.message) : "Drafting the mandate failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
      <p className="text-xs uppercase tracking-[0.2em] text-muted">Step one</p>
      <h1 className="display mt-2 text-4xl sm:text-5xl">Tell the agent what to buy.</h1>
      <p className="mt-3 text-muted">
        Your words are turned into a mandate you review and sign. The agent can only buy what the
        signed mandate allows.
      </p>

      <Card className="mt-8">
        <form onSubmit={submit} className="space-y-4">
          <label className="block">
            <span className="text-sm font-medium">Your request</span>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={3}
              placeholder="Buy me black trail running shoes, US size 10, mesh, at most 120 dollars total. Ship home."
              className="mt-2 w-full rounded-lg border border-line bg-bg px-3 py-2 text-base outline-none focus:border-accent"
              disabled={busy || draft !== null}
            />
          </label>
          <div className="flex flex-wrap items-end justify-between gap-4">
            <label className="text-sm">
              <span className="mr-2 text-muted">Shopping as</span>
              <select
                value={user}
                onChange={(e) => setUser(e.target.value)}
                className="rounded-md border border-line bg-bg px-2 py-1"
                disabled={busy || draft !== null}
              >
                {meta?.users.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.name}
                  </option>
                ))}
              </select>
            </label>
            {draft === null && (
              <Button type="submit" disabled={busy || !text.trim() || !user}>
                {busy ? "Drafting the mandate..." : "Draft my mandate"}
              </Button>
            )}
          </div>
        </form>
        {error && <p className="mt-4 rounded-lg bg-crimson/10 px-3 py-2 text-sm text-crimson">{error}</p>}
      </Card>

      {draft && meta && (
        <MandateCard
          draft={draft}
          meta={meta}
          user={user}
          onSigned={(id) => router.push(`/purchase/${id}`)}
          onRestart={() => setDraft(null)}
        />
      )}
    </div>
  );
}
