// Plays one short human game in headless Chromium against a running human-only app and prints one
// JSON line. Exit 0 only if the game started, took keyboard input, ended, and the server accepted the
// end of the session. Usage: node verify_human.mjs <app url> [seconds to play, default 8]
import { chromium } from "playwright";

const [url, secs = "8"] = process.argv.slice(2);
const report = { rates: null, pageErrors: [], failed: [], ok: false, console: [] };
const browser = await chromium.launch({ channel: "chromium", args: ["--enable-unsafe-swiftshader", "--autoplay-policy=no-user-gesture-required"] });
const guard = setTimeout(() => { report.timeout = true; console.log(JSON.stringify(report)); process.exit(2); }, 280000);
let page;
let created = null;
let ended = null;
try {
  page = await (await browser.newContext()).newPage();
  page.on("console", (m) => { if (/\[pdoom\]/.test(m.text()) || (report.console.length < 3 && m.type() === 'error')) report.console.push(m.type() + ": " + m.text().slice(0, 120)); });
  page.on("pageerror", (e) => report.pageErrors.push(String(e).slice(0, 300)));
  page.on("response", (r) => { if (r.status() >= 400 && !r.url().endsWith("favicon.ico")) report.failed.push(`${r.status()} ${r.url().slice(-60)}`); });
  page.on("response", async (r) => {
    if (r.request().method() !== "POST") return;
    if (r.url().endsWith("/api/sessions") && r.ok()) {
      try { created = JSON.parse(r.request().postData() || "{}"); } catch {}
    }
    if (r.url().endsWith("/end")) ended = { status: r.status(), body: JSON.parse(r.request().postData() || "{}") };
  });
  const health = await (await page.request.get(`${url}/api/health`)).json();
  report.health = health;
  if (!health.human_only) throw new Error("this is not a human-only deployment");

  await page.goto(url, { waitUntil: "load" });
  await page.getByRole("button", { name: /^Play$/ }).click();
  await page.getByText("Click the game to take control").or(page.getByText(/^Playing/)).first().waitFor({ timeout: 60000 });
  await page.waitForFunction(() => document.activeElement?.id === "canvas", null, { timeout: 30000 });
  for (let i = 0; i < Number(secs) * 4; i++) {
    await page.keyboard.down("ArrowUp");
    await page.waitForTimeout(150);
    await page.keyboard.up("ArrowUp");
    await page.keyboard.press(i % 8 < 4 ? "ArrowLeft" : "ArrowRight");
    await page.waitForTimeout(100);
  }
  // How responsive is the page while it plays? Count timer and frame callbacks over 3 s.
  report.rates = await page.evaluate(() => new Promise((res) => {
    let t = 0, f = 0;
    const iv = setInterval(() => t++, 30);
    const raf = () => { f++; requestAnimationFrame(raf); };
    requestAnimationFrame(raf);
    setTimeout(() => { clearInterval(iv); res({ timers30ms_per_3s: t, frames_per_3s: f }); }, 3000);
  }));
  await page.getByRole("button", { name: "Stop" }).click();
  const stoppedAt = Date.now();
  report.statusTrail = [];
  for (let i = 0; i < 6; i++) {
    if (await page.getByRole("status").count()) break;
    await page.waitForTimeout(5000);
    report.statusTrail.push(((Date.now() - stoppedAt) / 1000).toFixed(0) + "s " +
      (await page.locator(".actionbar").innerText().catch(() => "?")).replace(/\s+/g, " ") + " | " +
      (await page.locator("main").innerText().then((t) => (t.match(/Playing[^\n]*|Starting…|Ready/) || [""])[0]).catch(() => "?")));
  }
  await page.getByRole("status").first().waitFor({ timeout: 1000 });
  report.stopToCardS = (Date.now() - stoppedAt) / 1000;
  report.result = (await page.getByRole("status").first().innerText()).replace(/\s+/g, " ");
  await page.waitForTimeout(500);
  report.created = created;
  report.ended = ended;
  report.ok =
    !report.pageErrors.length &&
    created?.player_type === "human" && created?.engine === "wasm" &&
    ended?.status === 200 && !!ended?.body?.outcome;
} catch (e) {
  report.error = String(e).slice(0, 400);
  try { report.page = (await page.locator("main").innerText()).replace(/\s+/g, " ").slice(0, 500); } catch {}
  report.created = created;
  report.ended = ended;
} finally {
  clearTimeout(guard);
  await browser.close();
}
console.log(JSON.stringify(report));
process.exit(report.ok ? 0 : 1);
