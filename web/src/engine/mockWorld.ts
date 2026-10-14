// A small top-down arena that speaks the same state JSON as the real engine.
// It exists to test the players, telemetry and Genie pipeline end to end before the WASM
// build is ready. Mock sessions are tagged engine = "mock" and must never feed the deck.

import type { EngineStatus, GameKey, GameState } from "../types";

export const TIC_RATE = 35;

const TURN_RATE = 120; // degrees per second
const MOVE_SPEED = 200; // units per second
const STRAFE_SPEED = 160;
const EXIT_DISTANCE = 3000;
const EXIT_RADIUS = 80;
const ENEMY_COUNT = 8;
const VISIBLE_RANGE = 900;
const WAKE_RANGE = 900;
const ENEMY_SPEED = 60;
const ENEMY_STOP = 250;
const ATTACK_RANGE = 600;
const FIRE_RANGE = 800;
const FIRE_CONE = 10;

export function mulberry32(seed: number): () => number {
  let a = seed | 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const wrap180 = (deg: number) => ((((deg + 180) % 360) + 360) % 360) - 180;
const round1 = (n: number) => Math.round(n * 10) / 10;

export interface MockEnemy {
  x: number;
  y: number;
  hp: number;
  cooldown: number;
}

export class MockWorld {
  tic = 0;
  health = 100;
  kills = 0;
  ammo = 200;
  x = 0;
  y = 0;
  angle: number;
  enemies: MockEnemy[] = [];
  readonly exit = { x: 0, y: EXIT_DISTANCE };
  private rand: () => number;
  private fireCooldown = 0;

  constructor(
    readonly seed: number,
    readonly level = "E1M1",
  ) {
    this.rand = mulberry32(seed);
    this.angle = this.rand() * 360;
    for (let i = 0; i < ENEMY_COUNT; i++) {
      const along = ((i + 1) / (ENEMY_COUNT + 1)) * EXIT_DISTANCE * 0.9;
      const across = this.rand() * 600 - 300;
      this.enemies.push({ x: across, y: along, hp: 20, cooldown: 0 });
    }
  }

  status(): EngineStatus {
    if (this.health <= 0) return "died";
    if (Math.hypot(this.exit.x - this.x, this.exit.y - this.y) < EXIT_RADIUS) return "cleared";
    return "running";
  }

  bearingTo(tx: number, ty: number): number {
    return wrap180(this.angle - (Math.atan2(ty - this.y, tx - this.x) * 180) / Math.PI);
  }

  step(keys: ReadonlySet<GameKey>, dt = 1 / TIC_RATE): void {
    if (this.status() !== "running") return;
    this.tic++;

    if (keys.has("left")) this.angle += TURN_RATE * dt;
    if (keys.has("right")) this.angle -= TURN_RATE * dt;
    this.angle = ((this.angle % 360) + 360) % 360;

    const rad = (this.angle * Math.PI) / 180;
    const fx = Math.cos(rad);
    const fy = Math.sin(rad);
    const rx = Math.sin(rad); // facing rotated 90 degrees clockwise
    const ry = -Math.cos(rad);
    let mx = 0;
    let my = 0;
    if (keys.has("forward")) (mx += fx * MOVE_SPEED), (my += fy * MOVE_SPEED);
    if (keys.has("back")) (mx -= fx * MOVE_SPEED), (my -= fy * MOVE_SPEED);
    if (keys.has("strafe_right")) (mx += rx * STRAFE_SPEED), (my += ry * STRAFE_SPEED);
    if (keys.has("strafe_left")) (mx -= rx * STRAFE_SPEED), (my -= ry * STRAFE_SPEED);
    this.x += mx * dt;
    this.y += my * dt;

    this.fireCooldown = Math.max(0, this.fireCooldown - dt);
    if (keys.has("fire") && this.fireCooldown === 0 && this.ammo > 0) {
      this.ammo--;
      this.fireCooldown = 0.3;
      const target = this.nearestInCone();
      if (target && this.rand() < 0.7) {
        target.hp -= 10;
        if (target.hp <= 0) this.kills++;
      }
    }

    for (const e of this.enemies) {
      if (e.hp <= 0) continue;
      const dx = this.x - e.x;
      const dy = this.y - e.y;
      const dist = Math.hypot(dx, dy);
      if (dist >= WAKE_RANGE) continue;
      if (dist > ENEMY_STOP) {
        e.x += (dx / dist) * ENEMY_SPEED * dt;
        e.y += (dy / dist) * ENEMY_SPEED * dt;
      }
      e.cooldown = Math.max(0, e.cooldown - dt);
      if (dist < ATTACK_RANGE && e.cooldown === 0) {
        e.cooldown = 1;
        if (this.rand() < 0.4) this.health -= 5 + Math.floor(this.rand() * 6);
      }
    }
    if (this.health < 0) this.health = 0;
  }

  private nearestInCone(): MockEnemy | undefined {
    let best: MockEnemy | undefined;
    let bestDist = Infinity;
    for (const e of this.enemies) {
      if (e.hp <= 0) continue;
      const dist = Math.hypot(e.x - this.x, e.y - this.y);
      if (dist > FIRE_RANGE || Math.abs(this.bearingTo(e.x, e.y)) > FIRE_CONE) continue;
      if (dist < bestDist) (best = e), (bestDist = dist);
    }
    return best;
  }

  state(): GameState {
    const enemies = this.enemies
      .filter((e) => e.hp > 0)
      .map((e) => ({ e, dist: Math.hypot(e.x - this.x, e.y - this.y) }))
      .filter(({ dist }) => dist <= VISIBLE_RANGE)
      .sort((a, b) => a.dist - b.dist)
      .slice(0, 5)
      .map(({ e, dist }) => ({
        type: "zombieman",
        distance: Math.round(dist),
        bearing: round1(this.bearingTo(e.x, e.y)),
        line_of_sight: true,
      }));
    return {
      tic: this.tic,
      level: this.level,
      seed: this.seed,
      player: {
        health: this.health,
        armor: 0,
        weapon: "pistol",
        ammo: { bullets: this.ammo },
        x: Math.round(this.x),
        y: Math.round(this.y),
        angle: round1(this.angle),
      },
      enemies,
      pickups: [],
      exit_bearing: round1(this.bearingTo(this.exit.x, this.exit.y)),
    };
  }
}
