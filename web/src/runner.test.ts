import { describe, expect, it } from "vitest";
import type { Api } from "./api";
import type { Engine } from "./engine/Engine";
import { SessionRunner, type RunnerConfig } from "./runner";
import type { Decision, EngineStatus, GameKey, GameState } from "./types";

class FakeEngine implements Engine {
  readonly kind = "mock" as const;
  tic = 0;
  paused = false;
  constructor(
    private dieAtTic = Infinity,
    private pausable = true,
  ) {}
  async start() {
    this.tic = 0;
  }
  getState(): GameState {
    this.tic += 5; // each look at the game moves it on
    return {
      tic: this.tic,
      level: "E1M1",
      seed: 1,
      player: { health: 90, armor: 0, weapon: "pistol", ammo: { bullets: 20 }, x: 0, y: 0, angle: 0 },
      enemies: [],
      pickups: [],
      exit_bearing: 0,
    };
  }
  status(): EngineStatus {
    return this.tic >= this.dieAtTic ? "died" : "running";
  }
  kills() {
    return 2;
  }
  setKey(_k: GameKey, _d: boolean) {}
  canPause() {
    return this.pausable;
  }
  pause() {
    this.paused = true;
  }
  resume() {
    this.paused = false;
  }
  screenshot() {
    return null;
  }
  stop() {}
}

function fakeApi(engine: FakeEngine) {
  const log = {
    decidedWhilePaused: [] as boolean[],
    events: 0,
    ended: [] as Array<{ outcome: string; kills: number }>,
    created: 0,
  };
  const decision: Decision = {
    intent: "fire",
    confidence: null,
    threat_prob: null,
    danger_score: null,
    latency_ms: 1,
    error: null,
  };
  const api: Api = {
    players: async () => [],
    createSession: async () => ((log.created += 1), { session_id: "s1", model_version: "t" }),
    decide: async () => {
      log.decidedWhilePaused.push(engine.paused);
      return decision;
    },
    sendEvents: async (_id, events) => void (log.events += events.length),
    endSession: async (_id, body) => void log.ended.push({ outcome: body.outcome, kills: body.kills }),
    abandonSession: (_id, body) => void log.ended.push({ outcome: "abandoned", kills: body.kills }),
  };
  return { api, log };
}

const cfg = (over: Partial<RunnerConfig> = {}): RunnerConfig => ({
  playerType: "rule_bot",
  mode: "paused",
  engine: "mock",
  level: "E1M1",
  seed: 1,
  decisionIntervalMs: 0,
  eventEveryTics: 5,
  timeoutS: 1000,
  tickMs: 1,
  ...over,
});

const canvas = {} as HTMLCanvasElement;

describe("SessionRunner", () => {
  it("records a death, pauses the game around every decision and flushes events", async () => {
    const engine = new FakeEngine(100);
    const { api, log } = fakeApi(engine);
    const result = await new SessionRunner(engine, api, cfg()).run(canvas);

    expect(result.outcome).toBe("died");
    expect(log.ended).toEqual([{ outcome: "died", kills: 2 }]);
    expect(log.events).toBeGreaterThan(5);
    expect(log.decidedWhilePaused.length).toBeGreaterThan(0);
    expect(log.decidedWhilePaused.every(Boolean)).toBe(true);
  });

  it("ends as a timeout when the game-time cap passes", async () => {
    const engine = new FakeEngine();
    const { api, log } = fakeApi(engine);
    const result = await new SessionRunner(engine, api, cfg({ timeoutS: 2 })).run(canvas);
    expect(result.outcome).toBe("timeout");
    expect(result.durationS).toBeGreaterThanOrEqual(2);
    expect(log.ended[0].outcome).toBe("timeout");
  });

  it("records a stopped session as aborted, with its events", async () => {
    const engine = new FakeEngine();
    const { api, log } = fakeApi(engine);
    const runner = new SessionRunner(engine, api, cfg());
    const run = runner.run(canvas);
    setTimeout(() => runner.abort(), 20);
    const result = await run;
    expect(result.outcome).toBe("aborted");
    expect(log.ended).toEqual([{ outcome: "aborted", kills: 2 }]);
    expect(log.events).toBeGreaterThan(0);
  });

  it("records a closing page as abandoned, once, and does not also end normally", async () => {
    const handlers: Record<string, () => void> = {};
    const g = globalThis as unknown as Record<string, unknown>;
    g.addEventListener = (name: string, fn: () => void) => void (handlers[name] = fn);
    g.removeEventListener = () => {};
    try {
      const engine = new FakeEngine();
      const { api, log } = fakeApi(engine);
      const run = new SessionRunner(engine, api, cfg()).run(canvas);
      setTimeout(() => handlers.pagehide(), 20);
      await run.catch(() => {});
      expect(log.ended).toEqual([{ outcome: "abandoned", kills: 2 }]);
    } finally {
      delete g.addEventListener;
      delete g.removeEventListener;
    }
  });

  it("refuses paused mode on an engine that cannot pause", async () => {
    const engine = new FakeEngine(Infinity, false);
    const { api, log } = fakeApi(engine);
    await expect(new SessionRunner(engine, api, cfg()).run(canvas)).rejects.toThrow(/cannot pause/);
    expect(log.created).toBe(0);
  });

  it("keeps playing in realtime mode without pausing", async () => {
    const engine = new FakeEngine(60);
    const { api, log } = fakeApi(engine);
    await new SessionRunner(engine, api, cfg({ mode: "realtime" })).run(canvas);
    expect(log.decidedWhilePaused.some(Boolean)).toBe(false);
  });
});
