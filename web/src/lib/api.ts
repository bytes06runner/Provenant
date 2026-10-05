// Thin client for the Provenant API (api/app.py). The base URL comes from the environment.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8700";

export type Label = "USER" | "MERCHANT_SIGNED" | "DERIVED" | "UNTRUSTED";

export type Fields = {
  category: string;
  required_attributes: Record<string, string>;
  forbidden_attributes: Record<string, string[]>;
  max_unit_price: string | null;
  max_total: string | null;
  currency: string;
  quantity: number;
  merchant_allowlist: string[] | null;
  preference: "lowest_total" | "best_reviewed" | null;
  ship_to_ref: string | null;
};

export type Meta = {
  users: { id: string; name: string; addresses: string[] }[];
  merchants: string[];
  vocabulary: Record<string, string[]>;
  category: string;
  sdk_script_url: string;
  presentation_mode: string;
};

export type Draft = {
  id: string;
  fields: Fields;
  questions: string[];
  problems: string[];
  ready: boolean;
};

export type Source = { id?: string; label: Label; ref: string; path: string; kind: string; text: string };

export type Step = {
  seq: number;
  at: string;
  type: string;
  title: string;
  detail: string;
  label: Label | null;
  sources: Source[];
};

export type SessionInfo = {
  id: string;
  state: string;
  live?: boolean;
  error?: string | null;
  chain_head: string | null;
  mandate_seal?: string;
  manifest_seal?: string;
  decision_label?: Label;
  chosen?: { merchant_id: string; sku: string; unit_price: string; total: string; attributes: Record<string, string> };
  order?: { id: string; merchant_id: string; status: string; custom_id: string; approval_url: string | null };
};

export type GraphField = { id: string; field: string; name: string; value: string; label: Label; blocked: boolean };
export type Graph = {
  allowed: boolean;
  fields: GraphField[];
  sources: (Source & { id: string })[];
  edges: { from: string; to: string; label: Label; violating?: boolean }[];
  blocked: {
    attack: string;
    order_builder: string;
    violations: { field: string; rule: string; detail: string; provenance: string[] }[];
    fields: GraphField[];
  }[];
  decision: { label: Label; sources: Source[]; candidate: Record<string, unknown> } | null;
  mandate_seal: string | null;
  chain_head: string | null;
};

export type OrderRow = {
  session: string;
  merchant_id: string;
  order_id: string;
  created_at: string;
  custom_id: string | null;
  status: string;
  amount?: string;
  currency?: string;
  items?: string[];
  payments?: Record<string, { id: string; status: string }[]>;
  error?: string;
};

export type Shares = { U: string; M: string; A: string };
export type Attribution = {
  k: number;
  v: Record<string, string>;
  v_ci?: Record<string, [string, string]>;
  phi: Shares;
  shares: Shares | null;
  ci: Record<"U" | "M" | "A", [string, string]>;
  ci_level: string;
  interval?: string;
  escalate: boolean;
  reason: string;
  majority: string | null;
};
export type Remedy = {
  status: "proposed" | "needs_human_review" | "no_remedy";
  harm: string;
  r: string;
  actions: { kind: string; amount: string | null; party: string; reason: string }[];
  absorbed_by_user: string;
  over_cap: string;
  notes: string[];
};
export type Executed = { kind: string; resource_id: string; status: string; amount?: string; party?: string };
export type Ruling = { heading: string; findings: string[]; holding: string; remedy: string };
export type CaseView = {
  case_id: string;
  status: string;
  running?: string;
  purchase_session: string | null;
  order_id: string | null;
  custom_id: string | null;
  purchase: { merchant_id: string; sku: string; total: string; attributes: Record<string, string> } | null;
  complaint: { text: string; clarified: Record<string, unknown>; reported_attributes: Record<string, string>; photo_blob: string | null };
  facts: {
    ordered_sku: string;
    shipped_sku: string | null;
    shipped: boolean;
    fulfillment_fault: boolean;
    misrepresentations: Record<string, { signed: string; actual: string }>;
    clarified_violations: string[];
    conflicts: string[];
    delivery_check: Record<string, unknown> | null;
  } | null;
  outcome: { bad: number; why: string; best_total: string | null } | null;
  replays: Record<string, { k: number; bad: number }>;
  attribution: Attribution | null;
  attribution_note: string | null;
  revised: boolean;
  remedy: Remedy | null;
  ruling: { ruling: Ruling; source: string; rejected?: string } | null;
  evidence_pack: string | null;
  approvals: { by: string }[];
  executed: Executed[];
  reconciliation: { kind: string; resource_id: string; from: string | null; to: string; final: boolean }[];
  discrepancies: { order_id: string; unexplained_refunds: unknown[] }[];
  closed: Record<string, unknown> | null;
  chain_head: string | null;
};
export type CaseRow = {
  case_id: string;
  status: string;
  order_id: string | null;
  purchase: CaseView["purchase"];
  shares: Shares | null;
  escalate: boolean | null;
  remedy: Remedy | null;
  executed: Executed[];
  complaint: string | null;
};
export type EvalRow = {
  instance: string;
  kind: string;
  valid: boolean;
  invalid?: string | null;
  correct?: boolean | null;
  mae?: number | null;
  at: string;
  case?: string;
  budget_tokens?: number;
  seconds?: number;
  shares?: Shares | null;
  ci?: Record<string, [string, string]> | null;
};
export type EvalSummary = Record<
  string,
  { instances: number; valid: number; accuracy: number | null; mean_abs_error?: number | null; interval_coverage?: number | null; mean_budget_tokens?: number }
> & { generated_at?: string };

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(typeof body === "string" ? body : JSON.stringify(body));
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) throw new ApiError(res.status, body?.detail ?? body);
  return body as T;
}

export const api = {
  meta: () => call<Meta>("/api/meta"),
  draft: (user: string, request: string) =>
    call<Draft>("/api/sessions", { method: "POST", body: JSON.stringify({ user, request }) }),
  confirm: (id: string, fields: Fields) =>
    call<{ mandate: Record<string, unknown>; hash: string; key_id: string; seal: string }>(
      `/api/sessions/${id}/confirm`,
      { method: "POST", body: JSON.stringify({ fields }) },
    ),
  run: (id: string, reviewSku: string | null) =>
    call<{ state: string }>(`/api/sessions/${id}/run`, {
      method: "POST",
      body: JSON.stringify({ review_sku: reviewSku, probe_attacks: true }),
    }),
  session: (id: string) => call<SessionInfo>(`/api/sessions/${id}`),
  events: (id: string, after: number) =>
    call<{ state: string; error: string | null; steps: Step[] }>(`/api/sessions/${id}/events?after=${after}`),
  provenance: (id: string) => call<Graph>(`/api/sessions/${id}/provenance`),
  order: (id: string) =>
    call<{ order_id: string; merchant_id: string; approval_url: string | null; custom_id: string }>(
      `/api/sessions/${id}/order`,
      { method: "POST" },
    ),
  clientToken: (merchant: string) =>
    call<{ clientToken: string }>(`/api/paypal/client-token?merchant=${encodeURIComponent(merchant)}`),
  authorize: (id: string) =>
    call<{ authorization_id: string; status: string; custom_id: string; expiration_time: string }>(
      `/api/sessions/${id}/authorize`,
      { method: "POST" },
    ),
  orders: () => call<{ orders: OrderRow[] }>("/api/orders"),
  complain: (
    session: string,
    body: { text: string; clarify: Record<string, string>; report: Record<string, string>; photo_base64: string | null },
  ) => call<{ case_id: string }>(`/api/sessions/${session}/complaint`, { method: "POST", body: JSON.stringify(body) }),
  cases: () => call<{ cases: CaseRow[] }>("/api/cases"),
  case: (id: string) => call<CaseView>(`/api/cases/${id}`),
  approve: (id: string, by: string) =>
    call<{ executed: Executed[]; settled: boolean | null; discrepancies: unknown[] }>(`/api/cases/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ by }),
    }),
  evalAttribution: () => call<{ summary: EvalSummary; rows: EvalRow[] }>("/api/eval/attribution"),
};
