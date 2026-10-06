"use client";

import { useState } from "react";
import type { Label } from "@/lib/api";

const CHIP: Record<Label, { text: string; cls: string }> = {
  USER: { text: "User", cls: "text-accent border-accent/50 bg-accent/10" },
  MERCHANT_SIGNED: { text: "Sealed", cls: "text-laurel border-laurel/50 bg-laurel/10" },
  DERIVED: { text: "Derived", cls: "text-slate border-slate/50 bg-slate/10" },
  UNTRUSTED: { text: "Untrusted", cls: "text-crimson border-crimson/50 bg-crimson/10" },
};

export const LABEL_COLOR: Record<Label, string> = {
  USER: "var(--accent)",
  MERCHANT_SIGNED: "var(--laurel)",
  DERIVED: "var(--slate)",
  UNTRUSTED: "var(--crimson)",
};

export function LabelChip({ label, className = "" }: { label: Label | null | undefined; className?: string }) {
  if (!label) return null;
  const c = CHIP[label];
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide ${c.cls} ${className}`}
      title={label}
    >
      {c.text}
    </span>
  );
}

/** A hash shown short, full value on hover, click to copy. */
export function Hash({ value, n = 10, className = "" }: { value: string | null | undefined; n?: number; className?: string }) {
  const [copied, setCopied] = useState(false);
  if (!value) return <span className="num text-muted">none</span>;
  return (
    <button
      type="button"
      title={copied ? "Copied" : value}
      onClick={() => {
        navigator.clipboard?.writeText(value).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}
      className={`num cursor-copy rounded px-1 text-[0.95em] hover:bg-surface ${className}`}
    >
      {value.slice(0, n)}
      {copied ? " ✓" : ""}
    </button>
  );
}

/** Wax-seal badge for a signature: mandate, manifest seal, recorder chain head. */
export function WaxSeal({ title, value, tone = "accent" }: { title: string; value: string | null | undefined; tone?: "accent" | "laurel" | "slate" }) {
  const color = tone === "laurel" ? "var(--laurel)" : tone === "slate" ? "var(--slate)" : "var(--accent)";
  return (
    <div className="flex items-center gap-3">
      <svg width="44" height="44" viewBox="0 0 44 44" aria-hidden="true">
        <path
          d="M22 2c3 0 4 3 7 3.5s5.5-1.5 7.5 1 0 5.5 1.5 8 4.5 3.5 4 6.5-3.5 4-4 7 1.5 5.5-1 7.5-5.5 0-8 1.5-3.5 4.5-6.5 4-4-3.5-7-4-5.5 1.5-7.5-1 0-5.5-1.5-8S2 25 2 22s3.5-4 4-7-1.5-5.5 1-7.5 5.5 0 8-1.5S19 2 22 2z"
          fill={color}
          opacity="0.9"
        />
        <circle cx="22" cy="22" r="11" fill="none" stroke="var(--bg)" strokeOpacity="0.55" strokeWidth="1.2" />
        <text x="22" y="26" textAnchor="middle" fontSize="11" fill="var(--bg)" fontFamily="var(--font-display)" fontStyle="italic">
          P
        </text>
      </svg>
      <div className="leading-tight">
        <div className="text-[11px] uppercase tracking-wide text-muted">{title}</div>
        <Hash value={value} n={12} className="-ml-1 text-sm" />
      </div>
    </div>
  );
}

/** Greek-key (meander) divider, used sparingly at section breaks. */
export function Meander({ className = "" }: { className?: string }) {
  return (
    <div className={`my-8 text-line ${className}`} aria-hidden="true">
      <svg width="100%" height="14" preserveAspectRatio="none">
        <defs>
          <pattern id="meander" width="28" height="14" patternUnits="userSpaceOnUse">
            <path d="M0 13 H28 M22 13 V1 H6 V9 H16 V5 H11" fill="none" stroke="currentColor" strokeWidth="1.4" />
          </pattern>
        </defs>
        <rect width="100%" height="14" fill="url(#meander)" />
      </svg>
    </div>
  );
}

export function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <section className={`rounded-xl border border-line bg-surface/60 p-5 sm:p-6 ${className}`}>{children}</section>;
}

export function Button({
  children,
  variant = "primary",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" }) {
  const base = "inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50";
  const tone =
    variant === "primary"
      ? "bg-accent text-accent-ink hover:brightness-105"
      : "border border-line text-text hover:bg-surface";
  return (
    <button className={`${base} ${tone} ${className}`} {...props}>
      {children}
    </button>
  );
}

export function money(v: string | null | undefined, currency = "USD") {
  if (v == null) return "n/a";
  return `${v} ${currency}`;
}
