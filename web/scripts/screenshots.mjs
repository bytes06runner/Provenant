// Devpost gallery: every screen of the real running app with real sandbox data, at 1800x1200,
// in light and dark. Run with the API (port 8700), the merchant simulator and `npm run dev`:
//   node scripts/screenshots.mjs [--base http://localhost:3000]
// Creates one fresh purchase (real model calls and a real, unapproved sandbox PayPal order) for
// the mandate, run, provenance and approval screens; the recourse screens use recorded cases.
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const OUT = join(here, "..", "..", "docs", "screenshots");
const BASE = process.argv.includes("--base") ? process.argv[process.argv.indexOf("--base") + 1] : "http://localhost:3000";
const REQUEST =
  "Buy me the cheapest black trail running shoes, US size 10, mesh, at most 120 dollars total including shipping. Ship to my home.";
const CASES = {
  kestrel: "r-s-9315c6f5ddf0", // the first real case: shared fault, real refund
  merchant: "r-s-4799bb92e9da", // wrong variant shipped: photo, merchant refund
  agent: "r-s-5fca70b35942", // planted agent bug: payout from the liability pool
};
const MERCHANT_PURCHASE = "s-4799bb92e9da";

mkdirSync(OUT, { recursive: true });
let n = 0;

async function shot(page, name) {
  n += 1;
  const id = String(n).padStart(2, "0");
  for (const scheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.waitForTimeout(400);
    await page.screenshot({ path: join(OUT, `${id}-${name}-${scheme}.png`) });
  }
  console.log(`${id} ${name}`);
}

async function visit(page, path, ready) {
  await page.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
  if (ready) await page.getByText(ready, { exact: false }).first().waitFor({ timeout: 120_000 });
  await page.waitForTimeout(600);
}

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1800, height: 1200 }, deviceScaleFactor: 1 });
const page = await context.newPage();
page.setDefaultTimeout(120_000);

// 1. Landing
await visit(page, "/", "A tribunal for every purchase");
await shot(page, "landing");

// 2. Request and mandate card (a real draft from the mandate model)
await visit(page, "/purchase/new", "Tell the agent what to buy");
await page.getByPlaceholder(/Buy me black trail running shoes/).fill(REQUEST);
await page.getByRole("button", { name: "Draft my mandate" }).click();
await page.getByText("Your mandate").waitFor();
const perItem = page.locator("input[placeholder='0.00']").first();
if ((await perItem.inputValue()) === "") await perItem.fill("120.00");
await page.locator("input[placeholder='northwind/NOR-001']").fill("northwind/NOR-001");
await page.getByText("Your mandate").scrollIntoViewIfNeeded();
await page.mouse.wheel(0, -120);
await shot(page, "mandate-card");

// 3. Live run (real plan, contracts and live hijack probes)
await page.getByRole("button", { name: "Confirm and sign" }).click();
await page.waitForURL(/\/purchase\/s-[0-9a-f]+$/);
const session = page.url().split("/").pop();
await page.getByText("Checkout proposed").first().waitFor({ timeout: 180_000 });
await page.waitForTimeout(1500);
await shot(page, "agent-run");
await page.getByText("Hijack attempt blocked").first().scrollIntoViewIfNeeded();
await shot(page, "agent-run-blocked-hijacks");

// 4. Provenance graph
await visit(page, `/purchase/${session}/provenance`, "Where every field came from");
await page.locator(".react-flow__node").first().waitFor();
await page.waitForTimeout(1200);
await shot(page, "provenance-graph");
await page.getByText("Blocked hijack attempts").scrollIntoViewIfNeeded();
await shot(page, "provenance-blocked-and-decision");

// 5. PayPal approval (a real, unapproved sandbox order)
await visit(page, `/purchase/${session}`, "Checkout proposed");
await page.getByRole("button", { name: "Create PayPal order" }).click();
await page.getByText("custom_id").first().waitFor();
await page.waitForTimeout(6000); // the official PayPal button loads from PayPal's sandbox
await shot(page, "paypal-approval");

// 6. Orders with live PayPal status
await visit(page, "/orders", "Live status from PayPal");
await page.locator("table tbody tr").first().waitFor();
await shot(page, "orders");

// 7. Report a problem
await visit(page, `/purchase/${MERCHANT_PURCHASE}/report`, "Report a problem");
await page.locator("textarea").fill("The shoes I received are not the ones I ordered: they are red knit, I ordered black mesh.");
await shot(page, "report-a-problem");

// 8. Recourse list and rulings
await visit(page, "/cases", "Every complaint becomes a case");
await page.locator("ul li a").first().waitFor();
await shot(page, "recourse-cases");
await visit(page, `/cases/${CASES.kestrel}`, "Ruling on order");
await shot(page, "ruling-kestrel");
await page.getByText("What would have happened").scrollIntoViewIfNeeded();
await shot(page, "ruling-kestrel-counterfactuals");
await page.getByText("Remedy ordered").scrollIntoViewIfNeeded();
await shot(page, "ruling-kestrel-remedy");
await visit(page, `/cases/${CASES.merchant}`, "Ruling on order");
await shot(page, "ruling-wrong-variant");
await visit(page, `/cases/${CASES.agent}`, "Ruling on order");
await page.getByText("Fault").first().scrollIntoViewIfNeeded();
await shot(page, "ruling-agent-payout");

// 9. Ops Console
await visit(page, "/console", "Operations");
await page.locator(".ag-row").first().waitFor();
await shot(page, "console-recourse-queue");
await page.getByRole("tab", { name: "Ledger" }).click();
await page.locator(".ag-row").first().waitFor();
await page.waitForTimeout(800);
await shot(page, "console-ledger");
await page.getByRole("tab", { name: "Evaluation" }).click();
await page.waitForTimeout(800);
await shot(page, "console-evaluation");

await browser.close();
console.log(`purchase session ${session}; ${n} screens x 2 themes -> ${OUT}`);
