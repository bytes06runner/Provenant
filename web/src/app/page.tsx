import Link from "next/link";
import { LabelChip, Meander } from "@/components/ui";

const pillars = [
  {
    numeral: "I",
    title: "Lineage",
    body: "Every field of a PayPal order carries its origin. Payee, item, price and total must trace to your signed mandate or the merchant's signed catalog. Text from a web page can never bind them.",
  },
  {
    numeral: "II",
    title: "Blackbox",
    body: "When a purchase still goes wrong, the decision is replayed under corrections to you, the merchant and the agent. Fault is weighed with exact Shapley values and honest intervals.",
  },
  {
    numeral: "III",
    title: "Remedy",
    body: "The ruling moves money on PayPal rails: a void, a merchant refund within its signed policy, or a payout from the agent's liability pool. Every movement is recorded and reconciled.",
  },
];

export default function Home() {
  return (
    <div className="mx-auto max-w-6xl px-4 sm:px-6">
      <section className="grid gap-10 py-16 sm:py-24 lg:grid-cols-[1.3fr_1fr] lg:items-end">
        <div>
          <p className="mb-4 text-xs uppercase tracking-[0.2em] text-muted">Fiat iustitia</p>
          <h1 className="display text-5xl leading-[1.05] sm:text-7xl">
            A tribunal for every purchase your agent makes.
          </h1>
          <p className="mt-6 max-w-xl text-lg text-muted">
            Provenant makes agent checkout impossible to hijack through prompt injection, and when
            a purchase still goes wrong, it works out who caused it and moves the money
            accordingly.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link
              href="/purchase/new"
              className="rounded-lg bg-accent px-5 py-2.5 text-sm font-medium text-accent-ink hover:brightness-105"
            >
              Start a purchase
            </Link>
            <Link href="/orders" className="rounded-lg border border-line px-5 py-2.5 text-sm hover:bg-surface">
              See recorded orders
            </Link>
          </div>
        </div>
        <div className="rounded-xl border border-line bg-surface/60 p-6">
          <p className="mb-4 text-xs uppercase tracking-wide text-muted">Every value carries a label</p>
          <ul className="space-y-3 text-sm">
            <li className="flex items-center justify-between gap-4">
              <span>Your quantity, budget and address</span>
              <LabelChip label="USER" />
            </li>
            <li className="flex items-center justify-between gap-4">
              <span>The merchant&apos;s signed price and payee</span>
              <LabelChip label="MERCHANT_SIGNED" />
            </li>
            <li className="flex items-center justify-between gap-4">
              <span>A total computed from those</span>
              <LabelChip label="DERIVED" />
            </li>
            <li className="flex items-center justify-between gap-4">
              <span>Reviews, banners and page text</span>
              <LabelChip label="UNTRUSTED" />
            </li>
          </ul>
          <p className="mt-5 border-t border-line pt-4 text-xs text-muted">
            Untrusted content may inform which compliant item is chosen. It can never set who is
            paid, what is bought, how much, or where it ships.
          </p>
        </div>
      </section>

      <Meander />

      <section className="grid gap-6 pb-24 md:grid-cols-3">
        {pillars.map((p) => (
          <article key={p.title} className="rounded-xl border border-line p-6">
            <div className="display text-3xl text-accent">{p.numeral}</div>
            <h2 className="mt-2 text-lg font-semibold">{p.title}</h2>
            <p className="mt-2 text-sm leading-relaxed text-muted">{p.body}</p>
          </article>
        ))}
      </section>
    </div>
  );
}
