import type { Metadata } from "next";
import { Instrument_Serif, Inter, JetBrains_Mono } from "next/font/google";
import Link from "next/link";
import { ThemeToggle } from "@/components/theme-toggle";
import "./globals.css";

const instrument = Instrument_Serif({
  variable: "--font-instrument",
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
});
const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });
const jetbrains = JetBrains_Mono({ variable: "--font-jetbrains", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Provenant",
  description:
    "A trust layer for AI agents that spend money: provenance on every checkout field, and recourse when a purchase still goes wrong.",
};

const nav = [
  { href: "/purchase/new", label: "New purchase" },
  { href: "/orders", label: "Orders" },
  { href: "/cases", label: "Recourse" },
  { href: "/console", label: "Console" },
];

// Applied before paint so a saved theme never flashes the other one.
const themeScript = `try{var t=localStorage.getItem("pv-theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${instrument.variable} ${inter.variable} ${jetbrains.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-full flex flex-col">
        <header className="border-b border-line">
          <div className="mx-auto flex max-w-6xl items-center gap-6 px-4 py-4 sm:px-6">
            <Link href="/" className="display text-2xl leading-none text-text">
              Provenant
            </Link>
            <nav className="flex flex-1 items-center gap-1 text-sm">
              {nav.map((n) => (
                <Link
                  key={n.href}
                  href={n.href}
                  className="rounded-md px-3 py-1.5 text-muted hover:bg-surface hover:text-text"
                >
                  {n.label}
                </Link>
              ))}
            </nav>
            <ThemeToggle />
          </div>
        </header>
        <main className="flex-1">{children}</main>
        <footer className="border-t border-line">
          <div className="mx-auto max-w-6xl px-4 py-6 text-xs text-muted sm:px-6">
            Sandbox only. Every payment runs against the PayPal sandbox; every model call is real
            and recorded.
          </div>
        </footer>
      </body>
    </html>
  );
}
