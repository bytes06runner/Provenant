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
};
