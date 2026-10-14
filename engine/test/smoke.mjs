// Boots the built engine in headless Chromium and checks the state shim. Prints one JSON line.
//   node engine/test/smoke.mjs <dir with websockets-doom.js, doom1.wad, default.cfg>
//
// Each experiment runs in a fresh page with its own time limit, so one wedged scenario cannot hide
// the results of the others.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { join, extname } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const dist = process.argv[2];
const here = fileURLToPath(new URL(".", import.meta.url));
const types = { ".js": "text/javascript", ".wasm": "application/wasm", ".html": "text/html" };

const server = createServer(async (req, res) => {
  const path = req.url.split("?")[0];
  const name = path === "/" ? "/index.html" : path;
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

const report = { experiments: {} };
setTimeout(() => {
  report.watchdog = "smoke test exceeded 400 s";
  console.log(JSON.stringify(report));
  process.exit(2);
}, 400000).unref();

let launchOpts = { args: ["--use-gl=swiftshader", "--enable-unsafe-swiftshader"] };
const launch = () => chromium.launch(launchOpts);
let browser = await launch();
// A frozen page can make close() hang, so kill the browser process outright.
const killBrowser = async () => {
  try { browser.process()?.kill("SIGKILL"); } catch {}
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const slim = (s) =>
  s && s.player
    ? {
        lvl: s.level, demo: s.demo, gs: s.gs, st: s.status, tic: s.tic,
        hp: s.player.health, x: s.player.x, y: s.player.y, a: s.player.angle,
        en: s.enemies.length, pk: s.pickups.length, ex: s.exit_bearing,
      }
    : s;

async function experiment(name, body, query = "") {
  const out = { samples: [], errors: [] };
  report.experiments[name] = out;
  const page = await browser.newPage();
  page.on("pageerror", (e) => out.errors.push(String(e).slice(0, 160)));
  const consoleLines = [];
  page.on("console", (m) => {
    const t = m.text();
    if (/GL Driver|ScriptProcessor/.test(t)) return;
    consoleLines.push(t.slice(0, 70));
    if (consoleLines.length > 2) consoleLines.shift();
  });
  out.console = consoleLines;
  const ev = (fn, arg, ms = 4000) =>
    Promise.race([
      page.evaluate(fn, arg),
      new Promise((_, rej) => setTimeout(() => rej(new Error("hung")), ms)),
    ]);
  const state = async (mode = 7) =>
    slim(await ev((m) => JSON.parse(Module.ccall("pdoom_get_state_ex", "string", ["number"], [m])), mode));
  const sample = async (label, mode = 7) => {
    const t = Date.now();
    try {
      out.samples.push({ label, ...(await state(mode)) });
    } catch (e) {
      out.samples.push({ label, error: String(e).slice(0, 60), ms: Date.now() - t });
      throw e;
    }
  };
  const key = (type) =>
    ev((t) => document.dispatchEvent(new KeyboardEvent(t, {
      key: "ArrowUp", code: "ArrowUp", keyCode: 38, which: 38, bubbles: true })), type);
  const call = (fn, args = []) =>
    ev(([f, a]) => Module.ccall(f, null, a.map(() => "number"), a), [fn, args]);
  let limitTimer;
  const limit = new Promise((_, rej) => {
    limitTimer = setTimeout(() => rej(new Error("experiment timed out")), 50000);
  });
  limit.catch(() => {});
  try {
    await page.goto(`http://localhost:${port}/${query}`);
    await page.waitForFunction(() => window.engineReady, null, { timeout: 40000 });
    await sleep(3000);
    await Promise.race([body({ sample, key, call, sleep }), limit]);
  } catch (e) {
    out.errors.push(String(e).slice(0, 160));
  } finally {
    clearTimeout(limitTimer);
    await Promise.race([page.close(), sleep(3000)]).catch(() => {});
  }
}

// A real game start freezes the page under the original browser flags. Try other GL set-ups.
const setups = {
  default_headless: { args: [] },
  angle_swiftshader: { args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] },
  angle_swiftshader_novsync: {
    args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--disable-gpu-vsync"],
  },
  chromium_channel: { channel: "chromium", args: ["--enable-unsafe-swiftshader"] },
  no_webgl_accel: { args: ["--disable-gpu", "--disable-accelerated-2d-canvas"] },
};
for (const [name, opts] of Object.entries(setups)) {
  await killBrowser();
  launchOpts = opts;
  try {
    browser = await launch();
  } catch (e) {
    report.experiments[name] = { errors: ["launch failed: " + String(e).slice(0, 120)], samples: [] };
    continue;
  }
  await experiment(name, async ({ sample, sleep }) => {
    await sleep(5000);
    for (let i = 0; i < 3; i++) {
      await sample("t" + i, 0);
      await sleep(1000);
    }
  });
}

await killBrowser();
server.close();
console.log(JSON.stringify(report));
process.exit(0);
