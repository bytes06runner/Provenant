"use client";

import { useState } from "react";

type Mode = "system" | "light" | "dark";

export function ThemeToggle() {
  // The head script has already applied any saved theme to <html data-theme>.
  const [mode, setMode] = useState<Mode>(() => {
    if (typeof document === "undefined") return "system";
    const t = document.documentElement.dataset.theme;
    return t === "light" || t === "dark" ? t : "system";
  });

  function choose(next: Mode) {
    setMode(next);
    const root = document.documentElement;
    if (next === "system") delete root.dataset.theme;
    else root.dataset.theme = next;
    try {
      if (next === "system") localStorage.removeItem("pv-theme");
      else localStorage.setItem("pv-theme", next);
    } catch {
      /* not persisted */
    }
  }

  const order: Mode[] = ["system", "light", "dark"];
  const next = order[(order.indexOf(mode) + 1) % order.length];
  return (
    <button
      type="button"
      onClick={() => choose(next)}
      className="rounded-md border border-line px-2.5 py-1 text-xs text-muted hover:bg-surface hover:text-text"
      aria-label={`Theme: ${mode}. Switch to ${next}.`}
      suppressHydrationWarning
    >
      {mode === "system" ? "Auto" : mode === "light" ? "Light" : "Dark"}
    </button>
  );
}
