import type { EngineStatus, GameKey, GameState } from "../types";
import type { Engine } from "./Engine";
import { MockWorld, TIC_RATE } from "./mockWorld";

const SCALE = 0.12; // canvas pixels per world unit

export class MockEngine implements Engine {
  readonly kind = "mock" as const;
  private world = new MockWorld(0);
  private keys = new Set<GameKey>();
  private canvas?: HTMLCanvasElement;
  private timer?: ReturnType<typeof setInterval>;
  private paused = false;

  async start(canvas: HTMLCanvasElement, opts: { level: string; seed: number }): Promise<void> {
    this.stop();
    this.canvas = canvas;
    this.world = new MockWorld(opts.seed, opts.level);
    this.keys.clear();
    this.paused = false;
    this.timer = setInterval(() => {
      if (!this.paused) this.world.step(this.keys);
      this.draw();
    }, 1000 / TIC_RATE);
  }

  getState(): GameState {
    return this.world.state();
  }
  status(): EngineStatus {
    return this.world.status();
  }
  kills(): number {
    return this.world.kills;
  }
  setKey(key: GameKey, down: boolean): void {
    if (down) this.keys.add(key);
    else this.keys.delete(key);
  }
  canPause(): boolean {
    return true;
  }
  pause(): void {
    this.paused = true;
  }
  resume(): void {
    this.paused = false;
  }
  screenshot(): string | null {
    return this.canvas ? this.canvas.toDataURL("image/jpeg", 0.6).split(",")[1] : null;
  }
  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
  }

  private draw(): void {
    const canvas = this.canvas;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const w = this.world;
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    const px = (x: number) => cx + (x - w.x) * SCALE;
    const py = (y: number) => cy - (y - w.y) * SCALE; // world y points up the screen

    ctx.fillStyle = "#1E1916";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    ctx.fillStyle = "#4CAF7A";
    ctx.beginPath();
    ctx.arc(px(w.exit.x), py(w.exit.y), 12, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = "#E5483A";
    for (const e of w.enemies) {
      if (e.hp <= 0) continue;
      ctx.beginPath();
      ctx.arc(px(e.x), py(e.y), 6, 0, Math.PI * 2);
      ctx.fill();
    }

    const rad = (w.angle * Math.PI) / 180;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(-rad);
    ctx.fillStyle = "#E8B04B";
    ctx.beginPath();
    ctx.moveTo(12, 0);
    ctx.lineTo(-8, 7);
    ctx.lineTo(-8, -7);
    ctx.closePath();
    ctx.fill();
    ctx.restore();

    ctx.fillStyle = "#F3EEE6";
    ctx.font = "14px 'JetBrains Mono', monospace";
    ctx.fillText(`MOCK ARENA  hp ${w.health}  kills ${w.kills}  tic ${w.tic}`, 12, 22);
  }
}
