// Devpost image gallery: 15 images, 1800x1200 (3:2), PNG. Designed cards come from cards.html;
// app screens come from the real running app (docs/screenshots and a fresh capture).
// Run from web/ (Playwright lives there), with the app on :3000 and the API on :8700:
//   node ../Submission/source/make_gallery.mjs
import { copyFileSync, mkdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
// Playwright is a dev dependency of the buyer app.
const { chromium } = await import(pathToFileURL(join(root, "web", "node_modules", "playwright", "index.mjs")).href);
const OUT = join(here, "..", "gallery");
const SHOTS = join(root, "docs", "screenshots");
const BASELINE_RUN = process.env.BASELINE_RUN ?? "b-52cf96dfa972";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1800, height: 1200 } });

async function card(id, name) {
  await page.goto(pathToFileURL(join(here, "cards.html")).href + `#${id}`, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  await page.locator(`#${id}`).screenshot({ path: join(OUT, name) });
}

const plan = [
  ["card", "title", "01-provenant-title.png"],
  ["card", "problem", "02-the-problem.png"],
  ["card", "architecture", "03-how-it-works.png"],
  ["shot", "02-mandate-card-light.png", "04-signed-mandate-card.png"],
  ["shot", "04-agent-run-blocked-hijacks-dark.png", "05-live-hijacks-blocked.png"],
  ["shot", "05-provenance-graph-light.png", "06-provenance-graph.png"],
  ["shot", "07-paypal-approval-dark.png", "07-paypal-approval-js-sdk-v6.png"],
  ["live", `/baseline/${BASELINE_RUN}`, "08-baseline-agent-toolkit-before.png"],
  ["shot", "11-ruling-kestrel-light.png", "09-ruling-shared-fault.png"],
  ["shot", "12-ruling-kestrel-counterfactuals-dark.png", "10-counterfactual-replay.png"],
  ["shot", "13-ruling-kestrel-remedy-light.png", "11-remedy-paypal-refund.png"],
  ["shot", "14-ruling-wrong-variant-dark.png", "12-ruling-wrong-item-photo.png"],
  ["shot", "16-console-recourse-queue-light.png", "13-ops-console-recourse-queue.png"],
  ["card", "results", "14-results-on-paypal-sandbox.png"],
  ["card", "close", "15-provenant-closing.png"],
];

for (const [kind, src, name] of plan) {
  if (kind === "card") await card(src, name);
  else if (kind === "shot") copyFileSync(join(SHOTS, src), join(OUT, name));
  else {
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto(`http://localhost:3000${src}`, { waitUntil: "networkidle" });
    await page.getByText("What PayPal holds").waitFor({ timeout: 60_000 });
    await page.waitForTimeout(800);
    await page.screenshot({ path: join(OUT, name) });
  }
  const kb = Math.round(statSync(join(OUT, name)).size / 1024);
  console.log(`${name} ${kb} KB`);
}
await browser.close();
