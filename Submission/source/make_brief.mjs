// Project brief PDF (A4) from brief.html, rendered by Chromium. Run from web/:
//   node ../Submission/source/make_brief.mjs
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const { chromium } = await import(pathToFileURL(join(root, "web", "node_modules", "playwright", "index.mjs")).href);
const browser = await chromium.launch();
const page = await browser.newPage();
await page.goto(pathToFileURL(join(here, "brief.html")).href, { waitUntil: "networkidle" });
await page.evaluate(() => document.fonts.ready);
const out = join(here, "..", "Provenant-project-brief.pdf");
await page.pdf({
  path: out,
  format: "A4",
  printBackground: true,
  preferCSSPageSize: true,
  displayHeaderFooter: true,
  headerTemplate: "<span></span>",
  footerTemplate: `<div style="width:100%;font:7pt Inter,sans-serif;color:#5f584d;padding:0 16mm;display:flex;justify-content:space-between">
    <span>Provenant · PayPal AI Hackathon 2026 · github.com/bytes06runner/Provenant</span>
    <span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>`,
});
await browser.close();
console.log(out);
