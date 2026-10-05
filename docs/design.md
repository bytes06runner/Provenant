# Provenant design direction (Phase 3)

Status: agreed 2026-10-05. Not built yet; the frontend lands in Phase 3.

## Concept

**A Roman tribunal for agent purchases.** Elegant and editorial, finance-grade, never a costume
theme. Inspired by classical Roman law and ledgers: the product records, weighs evidence and
issues rulings, and the interface should feel like a well-set legal document and a careful
ledger, not like a toga party.

No third-party logos or brand names in the UI, except the official PayPal button.

## Typography

All from Google Fonts, each with proper fallbacks.

| Use | Face | Fallback stack |
|---|---|---|
| Display headings | **Instrument Serif**, italic, large and confident | `"Instrument Serif", "Iowan Old Style", "Palatino Linotype", Georgia, serif` |
| UI and body | **Inter** | `Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif` |
| IDs, hashes, amounts | **JetBrains Mono** | `"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace` |

Amounts use tabular figures. Hashes are shown short (first 8 to 12 characters) with the full value
on hover and copy.

## Color

Defined as tokens; values are targets ("around"), tuned for contrast (WCAG AA for text).

| Token | Light | Dark |
|---|---|---|
| `--bg` | ivory `#F4F1EA` | warm near-black charcoal `#1A1816` |
| `--surface` | parchment `#EAE4D8` | a step lighter than `--bg` |
| `--text` | charcoal `#1C1A17` | ivory |
| `--accent` | clay `#C4693F` | clay, lifted for contrast |
| `--laurel` | laurel green `#7D8B6A` | laurel, lifted for contrast |
| `--display` | charcoal | lavender `#B9A6D3` (italic display headings) |

### Label chips (and provenance graph edges, same colors)

| Label | Chip text | Color |
|---|---|---|
| `USER` | User | clay |
| `MERCHANT_SIGNED` | **Sealed** | laurel |
| `DERIVED` | Derived | slate grey |
| `UNTRUSTED` | Untrusted | muted crimson |

## Ornament

- A subtle meander (Greek key) border, as an SVG divider, used sparingly (section breaks on the
  ruling page and landing page, not on every card).
- Signatures appear as **wax-seal style badges** showing the short hash (mandate signature, manifest
  seal, recorder chain head).

## Language

- The verdict page reads as a ruling: **"Ruling on order 6SV6…"**. Fault shares with confidence
  intervals and the counterfactual table are set like a legal document: numbered findings,
  a holding, the remedy ordered.
- Plain English throughout. A little Latin at most, and only in headings.

## Screens

1. Landing page
2. Request and mandate card (editable fields, ambiguity questions highlighted, Confirm and Sign)
3. Live agent run (steps streaming, label chip on every tool result)
4. Provenance graph (React Flow; order fields on the right, sources on the left, edges colored by
   label, blocked fields marked with the violating path)
5. PayPal approval (official PayPal button, JS SDK v6)
6. Orders (status from PayPal)
7. Report a problem (text, optional photo, clarified-intent form)
8. Verdict / ruling (fault shares with CIs, counterfactual table, remedy with PayPal transaction ids)
9. Ops Console (AG Grid ledger and recourse queue)
10. Eval dashboard

## Screenshots for the Devpost gallery

A Playwright script captures every screen above **from the real running app with real sandbox
data**, at **1800x1200 (3:2)**, in **light and dark**, into `docs/screenshots/`.
