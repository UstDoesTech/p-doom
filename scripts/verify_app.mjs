// Loads the deployed app in full Chromium, plays one short Doom session as rule_bot and prints one
// JSON line. Usage:
//   node verify_app.mjs <app url> <bearer token> [player] [engine] [time cap, game s] [stop after ms]
// Defaults: rule_bot on the Doom WASM engine with a 15 s cap, played to the end. With a stop time,
// the Stop button is pressed after that many milliseconds.
import { chromium } from "playwright";

const [url, token, player = "rule_bot", engine = "wasm", cap = "15", stopAfter = ""] =
  process.argv.slice(2);
const report = { console: [], pageErrors: [], failedRequests: [] };
const trace = [];
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
  // Record what the bot saw and chose, from the decide calls, since the server may not keep them.
  page.on("response", async (r) => {
    if (!r.url().endsWith("/decide") || r.request().method() !== "POST") return;
    try {
      const st = JSON.parse(r.request().postData() || "{}").state;
      const d = await r.json();
      trace.push([st.tic, Math.round(st.player.x), Math.round(st.player.y), Math.round(st.player.angle),
        d.intent, d.raw_output?.rule, st.route ? [st.route.via, Math.round(st.route.bearing), Math.round(st.route.distance), Math.round(st.route.remaining)] : null,
        st.walls?.ahead, st.player.health,
        (st.enemies || []).slice(0, 2).map((e) => [e.type, Math.round(e.distance), Math.round(e.bearing), e.line_of_sight ? 1 : 0]),
        st.use_target ? st.use_target.kind : null]);
    } catch {}
  });
  page.on("response", (r) => {
    if (r.status() >= 400) report.failedRequests.push(`${r.status()} ${r.url().slice(-60)}`);
  });
  await page.goto(url, { waitUntil: "load" });
  await page.waitForSelector("select", { timeout: 30000 });
  await page.waitForTimeout(3000);
  const wasmOption = await page.locator("select").nth(2).locator("option[value=wasm]");
  report.wasmDisabled = engine === "wasm" ? await wasmOption.isDisabled() : false;
  report.player = player;
  report.engine = engine;
  if (!report.wasmDisabled) {
    await page.locator("select").nth(0).selectOption(player);
    await page.locator("select").nth(1).selectOption("paused");
    await page.locator("select").nth(2).selectOption(engine);
    await page.locator("input[type=number]").nth(2).fill(cap);
    await page.getByRole("button", { name: "Start" }).click();
    if (stopAfter) {
      await page.waitForTimeout(Number(stopAfter));
      await page.getByRole("button", { name: "Stop" }).click();
    }
    // Done when a result shows, or an error shows and the game is no longer running. An error
    // message alone can appear mid-game (a slow decision, say).
    await page.waitForFunction(
      () =>
        document.querySelector(".result") ||
        (document.querySelector(".error") && !document.querySelector("button.stop")),
      null,
      { timeout: 200000 },
    );
    const done = page.locator(".result, .error").first();
    report.result = (await done.innerText()).slice(0, 300);
    report.error = await page.locator(".error").innerText().catch(() => "");
    report.hud = await page.locator("dl").first().innerText().catch(() => "");
    report.log = await page.locator(".log").innerText().catch(() => "");
  }
} catch (e) {
  report.exception = String(e).slice(0, 400);
} finally {
  clearTimeout(timer);
  // Every 8th decision plus the last 30, to keep the line short.
  report.trace = trace.filter((_, i) => i % 8 === 0 || i >= trace.length - 30);
  console.log(JSON.stringify(report));
  await browser.close();
}
