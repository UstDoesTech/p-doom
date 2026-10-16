// Boots the built engine in headless Chromium and checks what the app depends on. Prints one JSON
// line with every check and exits non-zero if any fails.
//   node engine/test/smoke.mjs <dir with websockets-doom.js, doom1.wad, default.cfg>
//
// Needs full Chromium (playwright channel "chromium"). The headless-shell build freezes instead of
// reporting a wasm trap.
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
  const file = path === "/" ? join(here, "index.html") : join(dist, path);
  try {
    const body = await readFile(file);
    res.writeHead(200, { "content-type": types[extname(file)] ?? "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404).end();
  }
}).listen(0);
const port = server.address().port;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const report = { checks: {}, failed: [], errors: [] };
setTimeout(() => {
  report.failed.push("watchdog: exceeded 300 s");
  console.log(JSON.stringify(report));
  process.exit(2);
}, 300000).unref();

const browser = await chromium.launch({ channel: "chromium", args: ["--enable-unsafe-swiftshader"] });
const check = (name, ok, detail) => {
  report.checks[name] = detail === undefined ? ok : { ok, detail };
  if (!ok) report.failed.push(name);
};

try {
  const page = await browser.newPage();
  page.on("pageerror", (e) => report.errors.push(String(e.stack || e).replace(/\s+/g, " ").slice(0, 400)));
  await page.goto(`http://localhost:${port}/`);
  await page.waitForFunction(() => window.engineReady, null, { timeout: 40000 });

  const ev = (fn, arg) =>
    Promise.race([page.evaluate(fn, arg), sleep(8000).then(() => Promise.reject(new Error("page call hung")))]);
  const state = () => ev(() => JSON.parse(Module.ccall("pdoom_get_state", "string", [], [])));
  const call = (name, args = []) =>
    ev(([n, a]) => Module.ccall(n, null, a.map(() => "number"), a), [name, args]);
  const waitLevel = async (minTic = 0) => {
    for (let i = 0; i < 120; i++) {
      const s = await state();
      if (s.player && s.tic >= minTic) return s;
      await sleep(250);
    }
    throw new Error("player never appeared");
  };
  const press = (target, type, key) =>
    ev(([t, ty, k]) => {
      const el = t === "document" ? document : t === "window" ? window : document.getElementById("canvas");
      el.dispatchEvent(new KeyboardEvent(ty, { ...k, which: k.keyCode, bubbles: true }));
    }, [target, type, key]);
  const UP = { key: "ArrowUp", code: "ArrowUp", keyCode: 38 };
  const CTRL = { key: "Control", code: "ControlLeft", keyCode: 17 };

  // 1. A live E1M1 with the right shape.
  const s0 = await waitLevel(1);
  check("level_is_E1M1", s0.level === "E1M1", s0.level);
  check("not_demo", s0.demo === 0);
  check("full_health", s0.player.health === 100, s0.player.health);
  check("start_position", s0.player.x === 1056 && s0.player.y === -3616, [s0.player.x, s0.player.y]);
  check("sees_pickups", s0.pickups.length > 0, s0.pickups.length);
  check("has_exit_bearing", typeof s0.exit_bearing === "number", s0.exit_bearing);
  check("bearing_in_range", Math.abs(s0.exit_bearing) <= 180);
  // Scenery: walls all round, a route to the exit, and nothing to use at the start.
  const W = ["ahead", "ahead_right", "right", "behind_right", "behind", "behind_left", "left", "ahead_left"];
  check(
    "walls_in_eight_directions",
    !!s0.walls && W.every((k) => typeof s0.walls[k] === "number" && s0.walls[k] > 0 && s0.walls[k] <= 512),
    s0.walls,
  );
  check("some_wall_is_close", !!s0.walls && W.some((k) => s0.walls[k] < 300), s0.walls);
  check(
    "has_route",
    !!s0.route && Math.abs(s0.route.bearing) <= 180 && s0.route.distance > 0 && s0.route.remaining > 0,
    s0.route,
  );
  check("use_target_is_null_at_start", s0.use_target === null, s0.use_target);
  check("interactables_is_a_list", Array.isArray(s0.interactables), s0.interactables?.length);
  check("keys_is_a_list", Array.isArray(s0.keys) && s0.keys.length === 0, s0.keys);
  check("has_exit_distance", typeof s0.exit_distance === "number" && s0.exit_distance > 0, s0.exit_distance);
  await sleep(2000);
  const sIdle = await state();
  report.checks.tics_per_second_idle = Math.round((sIdle.tic - s0.tic) / 2);
  check("tics_advance", sIdle.tic > s0.tic);

  // Bearing sign: the exit is to the right (positive), so turning right must shrink the bearing
  // and lower the facing angle (Doom angles run counter-clockwise).
  const RIGHT = { key: "ArrowRight", code: "ArrowRight", keyCode: 39 };
  const t0 = await state();
  await press("document", "keydown", RIGHT);
  await sleep(500);
  await press("document", "keyup", RIGHT);
  const t1 = await state();
  const turned = ((t0.player.angle - t1.player.angle + 540) % 360) - 180;
  check("turn_right_lowers_angle", turned > 5, Math.round(turned));
  check(
    "turn_right_shrinks_positive_bearing",
    t0.exit_bearing > 0 ? Math.abs(t1.exit_bearing) < Math.abs(t0.exit_bearing) : true,
    [t0.exit_bearing, t1.exit_bearing],
  );

  // 2. Keyboard: which dispatch target does SDL listen on?
  const moved = {};
  for (const target of ["document", "window", "canvas"]) {
    const before = await state();
    await press(target, "keydown", UP);
    await sleep(1500);
    await press(target, "keyup", UP);
    const after = await state();
    moved[target] = Math.round(Math.hypot(after.player.x - before.player.x, after.player.y - before.player.y));
    if (moved[target] > 20) break;
  }
  report.checks.moved_by_target = moved;
  check("keyboard_moves_player", Object.values(moved).some((d) => d > 20));

  // 3. Fire: Ctrl should spend ammo.
  const preAmmo = (await state()).player.ammo.total;
  for (const target of ["document", "window", "canvas"]) {
    await press(target, "keydown", CTRL);
    await sleep(900);
    await press(target, "keyup", CTRL);
    if ((await state()).player.ammo.total < preAmmo) break;
  }
  const postAmmo = (await state()).player.ammo.total;
  check("fire_spends_ammo", postAmmo < preAmmo, [preAmmo, postAmmo]);

  // 4. Pause freezes the world, resume restarts it.
  await call("pdoom_pause");
  await sleep(400);
  const a = (await state()).tic;
  await sleep(1000);
  const b = (await state()).tic;
  check("pause_freezes", a === b, [a, b]);
  await call("pdoom_resume");
  await sleep(1000);
  const c = (await state()).tic;
  check("resume_runs", c > b, [b, c]);

  // 5. Same seed, same start; tic counter restarts.
  await call("pdoom_new_game", [1, 1, 42]);
  await sleep(1500);
  const r1 = await waitLevel(1);
  await call("pdoom_new_game", [1, 1, 42]);
  await sleep(1500);
  const r2 = await waitLevel(1);
  check("restart_resets_tic", r2.tic < 200, [r1.tic, r2.tic]);
  check("same_seed_same_start", r1.player.x === r2.player.x && r1.player.y === r2.player.y);
  check("level_after_restart", r2.level === "E1M1" && r2.player.health === 100);


  // 5b. Every level has a route from its start, and asking for it is quick enough to do every
  // decision. A level the planner cannot solve reports null.
  const perLevel = {};
  for (let m = 1; m <= 9; m++) {
    await call("pdoom_new_game", [1, m, 1]);
    await sleep(1500);
    let s;
    for (let i = 0; i < 40; i++) {
      s = await state();
      if (s.player && s.level === `E1M${m}` && s.tic >= 1) break;
      await sleep(250);
    }
    const t0 = Date.now();
    const again = await state();
    perLevel[`E1M${m}`] = s.route
      ? { via: s.route.via, remaining: s.route.remaining, ms: Date.now() - t0, again: !!again.route }
      : null;
  }
  report.checks.route_per_level = perLevel;
  check("route_on_every_level", Object.values(perLevel).every((r) => r !== null), perLevel);

  // 6. Follow the route like a minimal bot: turn to the route bearing, walk, press use at a door
  // or the exit. Shows the route leads somewhere and that use does what use_target says.
  await call("pdoom_new_game", [1, 1, 7]);
  await sleep(1500);
  const n0 = await waitLevel(1);
  const held = new Set();
  const hold = async (key, down) => {
    if (down === held.has(key.key)) return;
    await press("document", down ? "keydown" : "keyup", key);
    down ? held.add(key.key) : held.delete(key.key);
  };
  const LEFT = { key: "ArrowLeft", code: "ArrowLeft", keyCode: 37 };
  const SPACE = { key: " ", code: "Space", keyCode: 32 };
  let best = n0.route ? n0.route.remaining : 1e9, bestDist = n0.exit_distance, usedDoor = 0, usedExit = 0;
  let last = n0, outcome = "running", sinceUse = 99;
  const navUntil = Date.now() + 100000;
  while (Date.now() < navUntil) {
    const s = await state();
    last = s;
    if (s.status !== "running") {
      outcome = s.status;
      break;
    }
    if (s.route) best = Math.min(best, s.route.remaining);
    if (s.exit_distance != null) bestDist = Math.min(bestDist, s.exit_distance);
    const b = s.route ? s.route.bearing : s.exit_bearing ?? 0;
    const wallAhead = s.walls ? s.walls.ahead : 999;
    sinceUse++;
    const t = s.use_target;
    const wantUse = t && !t.locked && (t.kind === "exit" || (t.kind === "door" && !t.open)) && sinceUse > 4;
    if (wantUse) {
      sinceUse = 0;
      t.kind === "exit" ? usedExit++ : usedDoor++;
      await hold(SPACE, true);
      await sleep(150);
      await hold(SPACE, false);
    }
    await hold(LEFT, b < -12);
    await hold(RIGHT, b > 12);
    await hold(UP, Math.abs(b) < 25 && (wallAhead > 40 || sinceUse < 4));
    await sleep(250);
  }
  for (const k of [LEFT, RIGHT, UP, SPACE]) await hold(k, false);
  report.checks.nav = { outcome, remaining_start: n0.route?.remaining, remaining_best: best, exit_dist_start: Math.round(n0.exit_distance), exit_dist_best: Math.round(bestDist), usedDoor, usedExit, tic: last.tic, health: last.player.health };
  // A naive driver may jam on a wall, so only ask that the remaining route length shrinks at all.
  check("route_makes_progress", n0.route != null && best < n0.route.remaining - 50, [n0.route?.remaining, best]);

  check("no_page_errors", report.errors.length === 0, report.errors.length);
} catch (e) {
  report.failed.push("exception");
  report.errors.push(String(e).slice(0, 300));
} finally {
  try {
    browser.process()?.kill("SIGKILL");
  } catch {}
  server.close();
}
console.log(JSON.stringify(report));
process.exit(report.failed.length ? 1 : 0);
