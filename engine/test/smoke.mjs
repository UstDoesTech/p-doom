// Boots the built engine in headless Chromium and checks the state shim. Prints one JSON line.
//   node engine/test/smoke.mjs <dir with websockets-doom.js, doom1.wad, default.cfg>
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { join, extname } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const dist = process.argv[2];
const here = fileURLToPath(new URL(".", import.meta.url));
const types = { ".js": "text/javascript", ".wasm": "application/wasm", ".html": "text/html" };

const server = createServer(async (req, res) => {
  const name = req.url === "/" ? "/index.html" : req.url.split("?")[0];
  const file = name === "/index.html" ? join(here, "index.html") : join(dist, name);
  try {
    const body = await readFile(file);
    res.writeHead(200, { "content-type": types[extname(file)] ?? "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404).end();
  }
}).listen(0);
const port = server.address().port;

const report = { checks: {}, errors: [], stage: "start", console: [] };
// A wedged page must not hang CI: report what we have and stop.
setTimeout(() => {
  report.errors.push("watchdog: smoke test exceeded 200 s");
  console.log(JSON.stringify(report));
  process.exit(2);
}, 200000).unref();
const browser = await chromium.launch({ args: ["--use-gl=swiftshader", "--enable-unsafe-swiftshader"] });
try {
  var page = await browser.newPage();
  page.on("pageerror", (e) => report.errors.push(String(e).slice(0, 300)));
  page.on("console", (m) => {
    report.console.push(m.text().slice(0, 140));
    if (report.console.length > 25) report.console.shift();
  });
  // Every page call gets its own timeout so a wedged page names the call that hung.
  const ev = (label, fn, arg) =>
    Promise.race([
      page.evaluate(fn, arg),
      new Promise((_, rej) => setTimeout(() => rej(new Error(`page call hung: ${label}`)), 15000)),
    ]);
  report.stage = "goto";
  await page.goto(`http://localhost:${port}/`);
  report.stage = "wait runtime";
  await page.waitForFunction(() => window.engineReady, null, { timeout: 60000 });

  const state = () =>
    ev("get_state", () => JSON.parse(Module.ccall("pdoom_get_state", "string", [], [])));
  const waitPlayer = async () => {
    for (let i = 0; i < 120; i++) {
      const s = await state();
      if (s.player) return s;
      await page.waitForTimeout(250);
    }
    throw new Error("player never appeared");
  };

  report.stage = "first player";
  const s0 = await waitPlayer();
  report.checks.level = s0.level;
  report.checks.health = s0.player.health;
  report.checks.status = s0.status;
  report.checks.enemies = s0.enemies.length;
  report.checks.pickups = s0.pickups.length;
  report.checks.exit_bearing = s0.exit_bearing;
  report.checks.start = [s0.player.x, s0.player.y, s0.player.angle];
  report.checks.first_tic = s0.tic;

  report.stage = "movement";
  const press = (type) =>
    ev("key " + type, (t) => document.dispatchEvent(new KeyboardEvent(t, {
      key: "ArrowUp", code: "ArrowUp", keyCode: 38, which: 38, bubbles: true })), type);
  await press("keydown");
  await page.waitForTimeout(1500);
  await press("keyup");
  const s1 = await state();
  report.checks.moved = Math.hypot(s1.player.x - s0.player.x, s1.player.y - s0.player.y);
  report.checks.tic_advanced = s1.tic - s0.tic;

  report.stage = "pause";
  await ev("pause", () => Module.ccall("pdoom_pause", null, [], []));
  const a = (await state()).tic;
  await page.waitForTimeout(800);
  const b = (await state()).tic;
  report.checks.paused_frozen = a === b;
  await ev("resume", () => Module.ccall("pdoom_resume", null, [], []));
  await page.waitForTimeout(800);
  report.checks.resumed = (await state()).tic > b;

  report.stage = "new game";
  await ev("new_game", () => Module.ccall("pdoom_new_game", null, ["number", "number", "number"], [1, 1, 42]));
  report.stage = "after new game";
  report.checks.probes = [];
  for (let i = 0; i < 8; i++) {
    await page.waitForTimeout(1000);
    const t0 = Date.now();
    try {
      const alive = await ev("probe " + i, () => 1 + 1);
      const st = await ev("probe state " + i, () => JSON.parse(Module.ccall("pdoom_get_state", "string", [], [])));
      report.checks.probes.push({ i, alive, ms: Date.now() - t0, tic: st.tic, health: st.player && st.player.health, status: st.status });
    } catch (e) {
      report.checks.probes.push({ i, hung: String(e).slice(0, 80), ms: Date.now() - t0 });
      break;
    }
  }
  report.stage = "done";
  report.log_tail = (await page.evaluate(() => window.engineLog.slice(-6))).map((l) => l.slice(0, 120));
} catch (e) {
  report.errors.push(String(e).slice(0, 400));
  try { report.log_tail = (await page.evaluate(() => window.engineLog.slice(-8))).map((l) => l.slice(0, 160)); } catch {}
} finally {
  await browser.close();
  server.close();
}
console.log(JSON.stringify(report));
process.exit(report.errors.length ? 1 : 0);
