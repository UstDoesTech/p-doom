// Loads the deployed app in full Chromium, plays one short Doom session as rule_bot and prints one
// JSON line. Usage: node verify_app.mjs <app url> <bearer token>
import { chromium } from "playwright";

const [url, token] = process.argv.slice(2);
const report = { console: [], pageErrors: [], failedRequests: [] };
const browser = await chromium.launch({ channel: "chromium", args: ["--enable-unsafe-swiftshader"] });
const timer = setTimeout(() => {
  report.timeout = true;
  console.log(JSON.stringify(report));
  process.exit(2);
}, 280000);
try {
  const ctx = await browser.newContext();
  // Authorise requests to the app only; sending the token to other origins breaks their CORS.
  const origin = new URL(url).origin;
  await ctx.route("**/*", (route) => {
    const req = route.request();
    if (req.url().startsWith(origin)) {
      return route.continue({ headers: { ...req.headers(), authorization: `Bearer ${token}` } });
    }
    return route.continue();
  });
  const page = await ctx.newPage();
  page.on("console", (m) => {
    if (["error", "warning"].includes(m.type())) report.console.push(m.text().slice(0, 300));
  });
  page.on("pageerror", (e) => report.pageErrors.push(String(e).slice(0, 300)));
  page.on("requestfailed", (r) => report.failedRequests.push(`${r.url().slice(-60)} ${r.failure()?.errorText}`));
  page.on("response", (r) => {
    if (r.status() >= 400) report.failedRequests.push(`${r.status()} ${r.url().slice(-60)}`);
  });
  await page.goto(url, { waitUntil: "load" });
  await page.waitForSelector("select", { timeout: 30000 });
  await page.waitForTimeout(3000);
  const wasmOption = await page.locator("select").nth(2).locator("option[value=wasm]");
  report.wasmDisabled = await wasmOption.isDisabled();
  if (!report.wasmDisabled) {
    await page.locator("select").nth(0).selectOption("rule_bot");
    await page.locator("select").nth(1).selectOption("paused");
    await page.locator("select").nth(2).selectOption("wasm");
    await page.locator("input[type=number]").nth(2).fill("15");
    await page.getByRole("button", { name: "Start" }).click();
    const done = page.locator(".result, .error").first();
    await done.waitFor({ timeout: 200000 });
    report.result = (await done.innerText()).slice(0, 300);
    report.hud = await page.locator("dl").first().innerText().catch(() => "");
    report.log = await page.locator(".log").innerText().catch(() => "");
  }
} catch (e) {
  report.exception = String(e).slice(0, 400);
} finally {
  clearTimeout(timer);
  console.log(JSON.stringify(report));
  await browser.close();
}
