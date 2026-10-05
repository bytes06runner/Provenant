"use client";

import "@xyflow/react/dist/style.css";
import { Background, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { Card, LabelChip, LABEL_COLOR, WaxSeal } from "@/components/ui";
import { api, type Graph, type GraphField, type Label, type Source } from "@/lib/api";

type SourceData = { source: Source };
type FieldData = { field: GraphField; attack?: string };

function SourceNode({ data }: NodeProps<Node<SourceData>>) {
  const s = data.source;
  return (
    <div className="w-[300px] rounded-lg border border-line bg-bg px-3 py-2 text-xs shadow-sm">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-medium" title={`${s.ref}${s.path ? "#" + s.path : ""}`}>
          {s.text}
        </span>
        <LabelChip label={s.label} />
      </div>
      <Handle type="source" position={Position.Right} style={{ background: LABEL_COLOR[s.label] }} />
    </div>
  );
}

function FieldNode({ data }: NodeProps<Node<FieldData>>) {
  const f = data.field;
  return (
    <div
      className={`w-[250px] rounded-lg border px-3 py-2 shadow-sm ${f.blocked ? "border-crimson bg-crimson/10" : "border-line bg-surface"}`}
    >
      <Handle type="target" position={Position.Left} style={{ background: f.blocked ? "var(--crimson)" : "var(--line)" }} />
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] uppercase tracking-wide text-muted">{f.name}</span>
        <LabelChip label={f.label} />
      </div>
      <div className="num mt-1 truncate text-sm" title={f.value}>
        {f.value}
      </div>
      {f.blocked && <div className="mt-1 text-[11px] font-medium text-crimson">Blocked{data.attack ? `: ${data.attack}` : ""}</div>}
    </div>
  );
}

const nodeTypes = { source: SourceNode, field: FieldNode };

function layout(g: Graph): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const gapS = 64;
  g.sources.forEach((s, i) => nodes.push({ id: s.id, type: "source", position: { x: 0, y: i * gapS }, data: { source: s } }));
  let y = 0;
  const gapF = 76;
  g.fields.forEach((f) => {
    nodes.push({ id: f.id, type: "field", position: { x: 470, y }, data: { field: f } });
    y += gapF;
  });
  g.blocked.forEach((b) => {
    y += 24;
    b.fields.forEach((f) => {
      nodes.push({ id: f.id, type: "field", position: { x: 470, y }, data: { field: f, attack: b.attack } });
      y += gapF + 18; // blocked nodes carry an extra line
    });
  });
  const edges: Edge[] = g.edges.map((e, i) => ({
    id: `e${i}`,
    source: e.from,
    target: e.to,
    animated: Boolean(e.violating),
    style: {
      stroke: e.violating ? "var(--crimson)" : LABEL_COLOR[e.label as Label],
      strokeWidth: e.violating ? 2 : 1.5,
      strokeDasharray: e.violating ? "6 4" : undefined,
      opacity: e.violating ? 1 : 0.85,
    },
  }));
  return { nodes, edges };
}

export default function Provenance() {
  const { id } = useParams<{ id: string }>();
  const [g, setG] = useState<Graph | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.provenance(id).then(setG).catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [id]);

  const flow = useMemo(() => (g ? layout(g) : null), [g]);
  const height = flow ? Math.max(420, Math.max(...flow.nodes.map((n) => n.position.y)) + 120) : 420;

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      <Link href={`/purchase/${id}`} className="text-sm text-muted hover:text-text">
        Back to the run
      </Link>
      <h1 className="display mt-2 text-4xl sm:text-5xl">Where every field came from</h1>
      <p className="mt-3 max-w-2xl text-muted">
        On the right, the fields of the PayPal order. On the left, every source behind them. A field is only
        accepted if it traces to your signed mandate or the merchant&apos;s signed catalog.
      </p>
      {error && <p className="mt-4 rounded-lg bg-crimson/10 px-3 py-2 text-sm text-crimson">{error}</p>}

      {g && flow && (
        <>
          <div className="mt-6 flex flex-wrap items-center gap-6">
            <WaxSeal title="Your signed mandate" value={g.mandate_seal} />
            <WaxSeal title="Flight Recorder chain head" value={g.chain_head} tone="slate" />
            <div className="flex flex-wrap gap-2 text-xs text-muted">
              {(["USER", "MERCHANT_SIGNED", "DERIVED", "UNTRUSTED"] as Label[]).map((l) => (
                <LabelChip key={l} label={l} />
              ))}
            </div>
          </div>

          <div className="mt-6 overflow-hidden rounded-xl border border-line bg-surface/30" style={{ height }}>
            <ReactFlow
              nodes={flow.nodes}
              edges={flow.edges}
              nodeTypes={nodeTypes}
              fitView
              fitViewOptions={{ padding: 0.08 }}
              nodesDraggable={false}
              nodesConnectable={false}
              proOptions={{ hideAttribution: true }}
              minZoom={0.3}
              zoomOnScroll={false}
              preventScrolling={false}
              panOnDrag={false}
            >
              <Background color="var(--line)" gap={24} size={1} />
            </ReactFlow>
          </div>

          {g.blocked.length > 0 && (
            <Card className="mt-6">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">Blocked hijack attempts</h2>
              <ol className="mt-3 space-y-4">
                {g.blocked.map((b, i) => (
                  <li key={i} className="border-l-2 border-crimson pl-4">
                    <div className="font-medium">{b.attack}</div>
                    <ul className="mt-1 space-y-1 text-sm text-muted">
                      {b.violations.map((v, j) => (
                        <li key={j}>
                          <span className="num text-xs text-crimson">{v.field}</span> {v.detail}
                        </li>
                      ))}
                    </ul>
                    <div className="mt-1 text-xs text-muted">Order builder: {b.order_builder}</div>
                  </li>
                ))}
              </ol>
            </Card>
          )}

          {g.decision && (
            <Card className="mt-6">
              <div className="flex items-center justify-between gap-3">
                <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">Which compliant item won</h2>
                <LabelChip label={g.decision.label} />
              </div>
              <p className="mt-2 text-sm text-muted">
                {g.decision.label === "UNTRUSTED"
                  ? "Untrusted content (reviews, page text) influenced the choice among items that already passed every contract. This is the residual risk Lineage does not remove; Blackbox handles it if the choice turns out wrong."
                  : "The choice among compliant items used signed and derived data only."}
              </p>
              <ul className="mt-3 flex flex-wrap gap-2">
                {g.decision.sources.map((s, i) => (
                  <li key={i} className="rounded-full border border-line px-2.5 py-1 text-xs" style={{ color: LABEL_COLOR[s.label] }}>
                    {s.text}
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
