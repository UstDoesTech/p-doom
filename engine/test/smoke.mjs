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

const report = { experiments: {} };
setTimeout(() => {
  report.watchdog = "smoke test exceeded 280 s";
  console.log(JSON.stringify(report));
  process.exit(2);
}, 280000).unref();

const browser = await chromium.launch({ args: ["--use-gl=swiftshader", "--enable-unsafe-swiftshader"] });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const slim = (s) =>
  s && s.player
    ? {
        lvl: s.level, demo: s.demo, gs: s.gs, st: s.status, tic: s.tic,
        hp: s.player.health, x: s.player.x, y: s.player.y, a: s.player.angle,
        en: s.enemies.length, pk: s.pickups.length, ex: s.exit_bearing,
      }
    : s;

async function experiment(name, body) {
  const out = { samples: [], errors: [] };
  report.experiments[name] = out;
  const page = await browser.newPage();
  page.on("pageerror", (e) => out.errors.push(String(e).slice(0, 160)));
  const ev = (fn, arg, ms = 4000) =>
    Promise.race([
      page.evaluate(fn, arg),
      new Promise((_, rej) => setTimeout(() => rej(new Error("hung")), ms)),
    ]);
  const state = async () => slim(await ev(() => JSON.parse(Module.ccall("pdoom_get_state", "string", [], []))));
  const sample = async (label) => {
    const t = Date.now();
    try {
      out.samples.push({ label, ...(await state()) });
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
  const limit = new Promise((_, rej) => setTimeout(() => rej(new Error("experiment timed out")), 50000));
  try {
    await page.goto(`http://localhost:${port}/`);
    await page.waitForFunction(() => window.engineReady, null, { timeout: 40000 });
    await sleep(3000);
    await Promise.race([body({ sample, key, call, sleep }), limit]);
  } catch (e) {
    out.errors.push(String(e).slice(0, 160));
  } finally {
    out.log = await ev(() => window.engineLog.slice(-4), undefined, 1500).catch(() => "page unresponsive");
    await page.close().catch(() => {});
  }
}

await experiment("idle", async ({ sample, sleep }) => {
  for (let i = 0; i < 5; i++) {
    await sample("t" + i);
    await sleep(1000);
  }
});

await experiment("key_held", async ({ sample, key, sleep }) => {
  await sample("before");
  await key("keydown");
  for (let i = 0; i < 6; i++) {
    await sleep(500);
    await sample("held" + i);
  }
  await key("keyup");
  await sample("released");
});

await experiment("pause", async ({ sample, call, sleep }) => {
  await sample("before");
  await call("pdoom_pause");
  await sleep(400);
  await sample("paused+400ms");
  await sleep(1200);
  await sample("paused+1600ms");
  await call("pdoom_resume");
  await sleep(1000);
  await sample("resumed");
});

await experiment("new_game", async ({ sample, call, sleep }) => {
  await sample("before");
  await call("pdoom_new_game", [1, 1, 42]);
  for (let i = 0; i < 6; i++) {
    await sleep(1000);
    await sample("after" + i);
  }
});

await browser.close();
server.close();
console.log(JSON.stringify(report));
process.exit(0);
