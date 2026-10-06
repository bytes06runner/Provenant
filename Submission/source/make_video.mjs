// Demo video (under 2 minutes, 1920x1080) recorded from the real running app: a visible cursor,
// click ripples, smooth zooms and captions are injected into the page; frames are captured with
// the Chrome screencast (crisp text) and assembled with ffmpeg. While a model call is working,
// capture pauses, so the video has no dead air but every screen is real.
// Run from web/ with the app on :3000, the API on :8700 and the simulator on :8710:
//   node ../Submission/source/make_video.mjs
import { execFileSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const { chromium } = await import(pathToFileURL(join(root, "web", "node_modules", "playwright", "index.mjs")).href);
const FFMPEG = execFileSync(join(root, ".venv", "bin", "python"), ["-c", "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"]).toString().trim();
const OUT = join(here, "..");
const FRAMES = join(here, "frames");
const APP = "http://localhost:3000";
const W = 1920, H = 1080;
const BASELINE_RUN = "b-52cf96dfa972";
const KESTREL_CASE = "r-s-9315c6f5ddf0";
const REQUEST = "Buy me the cheapest black trail running shoes, US size 10, mesh, at most 120 dollars total including shipping. Ship to my home.";

rmSync(FRAMES, { recursive: true, force: true });
mkdirSync(FRAMES, { recursive: true });

// ---- overlays injected into every page -------------------------------------------------
const OVERLAY = `
(() => {
  if (window.__demo) return;
  const css = document.createElement('style');
  css.textContent = \`
    #__cursor { position: fixed; left: 0; top: 0; width: 34px; height: 34px; z-index: 2147483647; pointer-events: none;
                transform: translate(-4px, -2px); transition: left 0s, top 0s; filter: drop-shadow(0 2px 3px rgba(0,0,0,.35)); }
    .__ripple { position: fixed; z-index: 2147483646; pointer-events: none; width: 18px; height: 18px; border-radius: 50%;
                border: 3px solid #b55d34; transform: translate(-50%, -50%) scale(1); opacity: .95; animation: __rip 650ms ease-out forwards; }
    @keyframes __rip { to { transform: translate(-50%, -50%) scale(4.2); opacity: 0; } }
    #__cap { position: fixed; left: 50%; bottom: 54px; transform: translateX(-50%) translateY(16px); z-index: 2147483645; pointer-events: none;
             max-width: 1480px; padding: 18px 30px; border-radius: 16px; background: rgba(28,26,23,.9); color: #f4f1ea;
             font: 500 31px/1.35 Inter, system-ui, sans-serif; text-align: center; opacity: 0; transition: opacity .35s, transform .35s;
             box-shadow: 0 10px 30px rgba(0,0,0,.25); }
    #__cap.on { opacity: 1; transform: translateX(-50%) translateY(0); }
    #__cap b { color: #e9a07c; font-weight: 600; }
    #__chap { position: fixed; left: 40px; top: 96px; z-index: 2147483645; pointer-events: none; padding: 8px 16px; border-radius: 999px;
              background: rgba(181,93,52,.95); color: #fffaf3; font: 600 18px Inter, sans-serif; letter-spacing: .12em; text-transform: uppercase;
              opacity: 0; transition: opacity .35s; }
    #__chap.on { opacity: 1; }
    body.__zooming { transition: transform 1000ms cubic-bezier(.22,.7,.2,1); }
    ::-webkit-scrollbar { display: none; }\`;
  const add = () => {
    document.documentElement.appendChild(css);
    const c = document.createElement('div'); c.id = '__cursor';
    c.innerHTML = '<svg viewBox="0 0 24 24" width="34" height="34"><path d="M3 2 L3 19 L8 14.5 L11.5 22 L14.5 20.6 L11 13.3 L17.5 13.3 Z" fill="#1c1a17" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(c);
    const cap = document.createElement('div'); cap.id = '__cap'; document.documentElement.appendChild(cap);
    const chap = document.createElement('div'); chap.id = '__chap'; document.documentElement.appendChild(chap);
    const pos = JSON.parse(sessionStorage.getItem('__cursor') || '[960,540]');
    c.style.left = pos[0] + 'px'; c.style.top = pos[1] + 'px';
    document.addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px';
      sessionStorage.setItem('__cursor', JSON.stringify([e.clientX, e.clientY])); }, true);
    document.addEventListener('mousedown', e => { const r = document.createElement('div'); r.className = '__ripple';
      r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px'; document.documentElement.appendChild(r); setTimeout(() => r.remove(), 700); }, true);
  };
  if (document.body) add(); else document.addEventListener('DOMContentLoaded', add);
  window.__demo = {
    caption(html) { const el = document.getElementById('__cap'); if (!html) { el.classList.remove('on'); return; }
      el.innerHTML = html; el.classList.add('on'); },
    chapter(t) { const el = document.getElementById('__chap'); if (!t) { el.classList.remove('on'); return; } el.textContent = t; el.classList.add('on'); },
    zoom(x, y, s) { const b = document.body; b.classList.add('__zooming');
      b.style.transformOrigin = x + 'px ' + (y + window.scrollY) + 'px'; b.style.transform = 'scale(' + s + ')'; },
    unzoom() { document.body.style.transform = 'none'; },
  };
})();`;

// ---- capture -----------------------------------------------------------------------------
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1, colorScheme: "light" });
await context.addInitScript(OVERLAY);
const page = await context.newPage();
page.setDefaultTimeout(180_000);
const cdp = await context.newCDPSession(page);
const frames = []; // {file, t}
let paused = false, pausedAt = 0, pausedTotal = 0, n = 0;
cdp.on("Page.screencastFrame", async ({ data, metadata, sessionId }) => {
  cdp.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
  if (paused) return;
  const file = join(FRAMES, `f${String(n++).padStart(6, "0")}.jpg`);
  writeFileSync(file, Buffer.from(data, "base64"));
  frames.push({ file, t: metadata.timestamp - pausedTotal });
});
const now = () => Date.now() / 1000;
function pause() { paused = true; pausedAt = now(); }
function resume() { pausedTotal += now() - pausedAt; paused = false; }
// Long holds are trimmed by a quarter so the whole video stays under two minutes.
const wait = (ms) => page.waitForTimeout(ms >= 1500 ? Math.round(ms * 0.74) : ms);

const demo = (fn, ...args) => page.evaluate(({ fn, args }) => window.__demo[fn](...args), { fn, args });
const caption = (html) => demo("caption", html);
const chapter = (t) => demo("chapter", t);

async function centerOf(locator) {
  await locator.scrollIntoViewIfNeeded();
  const b = await locator.boundingBox();
  return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
}
async function moveTo(locator, steps = 28) {
  const { x, y } = await centerOf(locator);
  await page.mouse.move(x, y, { steps });
  return { x, y };
}
async function clickOn(locator) {
  await moveTo(locator);
  await wait(180);
  await locator.click();
}
async function zoomOn(locator, scale = 1.55) {
  const { x, y } = await centerOf(locator);
  await demo("zoom", x, y, scale);
  await wait(1100);
}
async function unzoom() { await demo("unzoom"); await wait(900); }
async function smoothScrollTo(locator, offset = 200) {
  await page.evaluate(({ y }) => window.scrollTo({ top: y, behavior: "smooth" }), {
    y: await locator.evaluate((el, off) => el.getBoundingClientRect().top + window.scrollY - off, offset),
  });
  await wait(900);
}
async function card(id, ms, zoom = 1.04) {
  await page.goto(pathToFileURL(join(here, "cards.html")).href + `#${id}`, { waitUntil: "networkidle" });
  await page.evaluate(({ id, W, H }) => {
    for (const s of document.querySelectorAll("section")) s.style.display = s.id === id ? "flex" : "none";
    const c = document.getElementById(id);
    document.body.style.cssText = `margin:0;width:${W}px;height:${H}px;position:relative;overflow:hidden;background:${getComputedStyle(c).backgroundColor}`;
    // Center the 1800x1200 card in the 16:9 frame, scaled around its own center.
    c.style.cssText += `;position:absolute;margin:0;left:${(W - 1800) / 2}px;top:${(H - 1200) / 2}px;transform-origin:50% 50%;transform:scale(${H / 1200})`;
  }, { id, W, H });
  await page.evaluate(() => document.fonts.ready);
  await wait(200);
  if (paused) resume();
  const sec = page.locator(`#${id}`);
  await sec.evaluate((el, z) => { el.style.transition = "transform 6s ease-out"; el.style.transform = `scale(${(1080 / 1200) * z})`; }, zoom);
  await wait(ms);
}

pause(); // nothing is captured until the first card is on screen
await cdp.send("Page.startScreencast", { format: "jpeg", quality: 88, maxWidth: W, maxHeight: H, everyNthFrame: 1 });

// 1. Title and problem
await card("title", 4200);
await card("problem", 7800);

// 2. The before picture: a conventional agent on PayPal's Agent Toolkit
await page.emulateMedia({ colorScheme: "light" });
await page.goto(`${APP}/baseline/${BASELINE_RUN}`, { waitUntil: "networkidle" });
await page.getByText("What PayPal holds").waitFor();
await chapter("Before: a conventional agent");
await caption("A typical agent on <b>PayPal's Agent Toolkit</b> reads shop pages and fills in the order itself.");
await wait(2600);
await moveTo(page.getByText("Called PayPal create_order").first());
await zoomOn(page.getByText("Called PayPal create_order").first().locator(".."), 1.45);
await caption("It typed the shop, item, price and quantity into <b>create_order</b>.");
await wait(2600);
await unzoom();
const against = page.getByText("Against the merchant").locator("..");
await moveTo(against);
await zoomOn(against, 1.6);
await caption("PayPal now holds <b>149.00</b>. The merchant's signed total is <b>160.88</b>, over the 150 budget. Nothing is traceable.");
await wait(4200);
await unzoom();
await caption(null); await chapter(null);

// 3. Provenant: the signed mandate
await page.goto(`${APP}/purchase/new`, { waitUntil: "networkidle" });
await chapter("Provenant: Lineage");
await caption("Same request through Provenant. Your words become a <b>mandate you sign</b>.");
const box = page.getByPlaceholder(/Buy me black trail running shoes/);
await clickOn(box);
await box.pressSequentially(REQUEST, { delay: 9 });
await clickOn(page.getByRole("button", { name: "Draft my mandate" }));
pause();
await page.getByText("Your mandate").waitFor();
await wait(500);
resume();
const needs = page.getByText("Needs your answer").first();
await smoothScrollTo(needs, 260);
await zoomOn(needs, 1.5);
await caption("Ambiguities are asked, never guessed. Every field you confirm is labeled <b>User</b>.");
await wait(2600);
await unzoom();
const perItem = page.locator("input[placeholder='0.00']").first();
await clickOn(perItem);
if ((await perItem.inputValue()) === "") await perItem.pressSequentially("120.00", { delay: 40 });
const reviews = page.locator("input[placeholder='northwind/NOR-001']");
await clickOn(reviews);
await reviews.pressSequentially("northwind/NOR-001", { delay: 25 });
await clickOn(page.getByRole("button", { name: "Confirm and sign" }));
pause();
await page.waitForURL(/\/purchase\/s-[0-9a-f]+$/);
const session = page.url().split("/").pop();
await page.getByText("Checkout proposed").first().waitFor();
await wait(1200);
resume();

// 4. The live run: labels and blocked hijacks
await caption("Every tool result carries a label. Reviews are read as <b>Untrusted</b> text.");
await wait(2400);
const blocked = page.getByText("Hijack attempt blocked").first().locator("..").locator("..");
await smoothScrollTo(blocked, 380);
await moveTo(blocked);
await zoomOn(blocked, 1.5);
await caption("Live hijack attempts: a payee injected by a page, and the attacker's own signed payee. <b>Both blocked.</b>");
await wait(3600);
await unzoom();

// 5. Provenance graph
await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
await wait(700);
await clickOn(page.getByText("See where every field came from"));
await page.locator(".react-flow__node").first().waitFor();
await wait(1200);
await caption("Every field of the PayPal order traces to <b>your signature</b> or the <b>merchant's seal</b>.");
await wait(2600);
const flow = page.locator(".react-flow");
await smoothScrollTo(flow, 120);
const badPayee = page.getByText("Blocked: payee from injected page text").first();
await moveTo(badPayee);
await zoomOn(badPayee, 1.7);
await caption("Text from a web page can never bind <b>who is paid</b>, what is bought, how much, or where it ships.");
await wait(3600);
await unzoom();

// 6. PayPal approval
await page.goto(`${APP}/purchase/${session}`, { waitUntil: "networkidle" });
await page.getByText("Checkout proposed").first().waitFor();
await clickOn(page.getByRole("button", { name: "Create PayPal order" }));
pause();
await page.getByText("custom_id").first().waitFor();
await wait(5000);
resume();
const paypalCard = page.getByText("custom_id").first().locator("..").locator("..").locator("..");
await moveTo(paypalCard);
await zoomOn(paypalCard, 1.45);
await caption("A real PayPal order: its <b>custom_id</b> is the hash of the decision trace. You approve with the official PayPal button.");
await wait(3800);
await unzoom();
await caption(null); await chapter(null);

// 7. Recourse: the ruling
await page.emulateMedia({ colorScheme: "dark" });
await page.goto(`${APP}/cases/${KESTREL_CASE}`, { waitUntil: "networkidle" });
await page.getByText("Ruling on order").waitFor();
await chapter("Blackbox: when it still goes wrong");
await caption("The shoes leak. The merchant had <b>signed</b> 'waterproof: yes'. The buyer files a complaint.");
await wait(3200);
const table = page.getByText("What would have happened").first();
await smoothScrollTo(table, 120);
await caption("Blackbox replays the purchase with each party corrected: <b>64 replays</b>, from the recorded inputs.");
await wait(1200);
const zeroRow = page.getByText("Clear request and accurate merchant").first().locator("..");
await moveTo(zeroRow);
await zoomOn(zeroRow, 1.5);
await caption("Only when <b>both</b> the request and the listing are corrected does the wrong purchase stop.");
await wait(3600);
await unzoom();
const fault = page.getByText("Fault", { exact: false }).locator("xpath=ancestor::section[1]").last();
await smoothScrollTo(fault, 160);
await caption("Fault, by exact Shapley values: <b>you 50%, merchant 50%, agent 0%</b>, with Jeffreys intervals.");
await wait(3600);
const remedy = page.getByText("PayPal refund").first().locator("..");
await smoothScrollTo(remedy, 360);
await moveTo(remedy);
await zoomOn(remedy, 1.6);
await caption("The merchant's share is refunded on PayPal: <b>45.26</b>, reconciled until COMPLETED.");
await wait(3800);
await unzoom();

// 8. Ops console
await page.emulateMedia({ colorScheme: "light" });
await page.goto(`${APP}/console`, { waitUntil: "networkidle" });
await page.locator(".ag-row").first().waitFor();
await chapter("Operations");
await caption("Every remedy needs one operator click. <b>Void, refund and payout</b>, all on the PayPal sandbox.");
await wait(1500);
await zoomOn(page.locator(".ag-root-wrapper").first(), 1.35);
await wait(2800);
await unzoom();
await caption(null); await chapter(null);

// 9. Results and close
await card("results", 7200, 1.03);
await card("close", 4800, 1.03);

await cdp.send("Page.stopScreencast");
await browser.close();

// ---- assemble ----------------------------------------------------------------------------
const lines = ["ffconcat version 1.0"];
for (let i = 0; i < frames.length; i++) {
  const d = i + 1 < frames.length ? Math.max(0.001, frames[i + 1].t - frames[i].t) : 1.5;
  lines.push(`file '${frames[i].file}'`, `duration ${d.toFixed(4)}`);
}
lines.push(`file '${frames[frames.length - 1].file}'`);
const list = join(here, "frames.txt");
writeFileSync(list, lines.join("\n") + "\n");
const total = frames[frames.length - 1].t - frames[0].t + 1.5;
const mp4 = join(OUT, "Provenant-demo.mp4");
execFileSync(FFMPEG, ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list,
  "-vf", "fps=30,scale=in_range=pc:out_range=tv,format=yuv420p", "-color_range", "tv", "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-movflags", "+faststart", mp4]);
rmSync(FRAMES, { recursive: true, force: true });
rmSync(list, { force: true });
console.log(`session ${session}; ${frames.length} frames; about ${total.toFixed(1)} s -> ${mp4}`);
