import { describe, expect, it } from "vitest";
import { ApiTimeout, type Api } from "./api";
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

function fakeApi(engine: FakeEngine, failDecide = false, failCreate = false, delayMs = 0) {
  const log = {
    decidedWhilePaused: [] as boolean[],
    events: 0,
    ended: [] as Array<{ outcome: string; kills: number; timeouts: number }>,
    created: 0,
    modes: [] as string[],
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
    createSession: async (body) => {
      log.created += 1;
      log.modes.push(body.mode);
      if (failCreate) throw new Error("503 unavailable");
      return { session_id: "s1", model_version: "t" };
    },
    decide: async () => {
      log.decidedWhilePaused.push(engine.paused);
      if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
      if (failDecide) throw new ApiTimeout("/sessions/s1/decide", 25000);
      return decision;
    },
    sendEvents: async (_id, events) => void (log.events += events.length),
    endSession: async (_id, body) =>
      void log.ended.push({
        outcome: body.outcome,
        kills: body.kills,
        timeouts: body.decision_timeouts,
      }),
    abandonSession: (_id, body) =>
      void log.ended.push({
        outcome: "abandoned",
        kills: body.kills,
        timeouts: body.decision_timeouts,
      }),
  };
  return { api, log };
}

const cfg = (over: Partial<RunnerConfig> = {}): RunnerConfig => ({
  playerType: "rule_bot",
  mode: "realtime",
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
  it("records a death, sends every session as realtime and flushes events", async () => {
    const engine = new FakeEngine(100);
    const { api, log } = fakeApi(engine);
    const result = await new SessionRunner(engine, api, cfg()).run(canvas);

    expect(result.outcome).toBe("died");
    expect(log.ended).toEqual([{ outcome: "died", kills: 2, timeouts: 0 }]);
    expect(log.events).toBeGreaterThan(5);
    expect(log.decidedWhilePaused.length).toBeGreaterThan(0);
    expect(log.decidedWhilePaused.some(Boolean)).toBe(false);
    expect(log.modes).toEqual(["realtime"]);
  });

  it("ends as a timeout when the game-time cap passes", async () => {
    const engine = new FakeEngine();
    const { api, log } = fakeApi(engine);
    const result = await new SessionRunner(engine, api, cfg({ timeoutS: 2 })).run(canvas);
    expect(result.outcome).toBe("timeout");
    expect(result.durationS).toBeGreaterThanOrEqual(2);
    expect(log.ended[0].outcome).toBe("timeout");
  });

  it("has no cap when timeoutS is 0 and plays on until the game ends", async () => {
    const engine = new FakeEngine(400); // dies after 400 tics, far past a 2 s cap
    const { api } = fakeApi(engine);
    const result = await new SessionRunner(engine, api, cfg({ timeoutS: 0 })).run(canvas);
    expect(result.outcome).toBe("died");
    expect(result.durationS).toBeGreaterThan(2);
  });

  it("records a stopped session as aborted, with its events", async () => {
    const engine = new FakeEngine();
    const { api, log } = fakeApi(engine);
    const runner = new SessionRunner(engine, api, cfg());
    const run = runner.run(canvas);
    setTimeout(() => runner.abort(), 20);
    const result = await run;
    expect(result.outcome).toBe("aborted");
    expect(log.ended).toEqual([{ outcome: "aborted", kills: 2, timeouts: 0 }]);
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
      expect(log.ended).toEqual([{ outcome: "abandoned", kills: 2, timeouts: 0 }]);
    } finally {
      delete g.addEventListener;
      delete g.removeEventListener;
    }
  });

  it("pauses the game around every decision in paused mode and records it as paused", async () => {
    const engine = new FakeEngine(100);
    const { api, log } = fakeApi(engine);
    await new SessionRunner(engine, api, cfg({ mode: "paused" })).run(canvas);
    expect(log.decidedWhilePaused.length).toBeGreaterThan(0);
    expect(log.decidedWhilePaused.every(Boolean)).toBe(true);
    expect(log.modes).toEqual(["paused"]);
  });

  it("refuses paused mode on an engine that cannot pause", async () => {
    const engine = new FakeEngine(Infinity, false);
    const { api, log } = fakeApi(engine);
    await expect(new SessionRunner(engine, api, cfg({ mode: "paused" })).run(canvas)).rejects.toThrow(
      /cannot pause/,
    );
    expect(log.created).toBe(0);
  });

  it("asks one call at a time in paused mode, whatever maxInFlight says", async () => {
    const engine = new FakeEngine(800);
    const { api } = fakeApi(engine);
    let now = 0;
    let peak = 0;
    const inner = api.decide.bind(api);
    api.decide = async (...a) => {
      now += 1;
      peak = Math.max(peak, now);
      await new Promise((r) => setTimeout(r, 20));
      try {
        return await inner(...a);
      } finally {
        now -= 1;
      }
    };
    await new SessionRunner(engine, api, cfg({ mode: "paused", maxInFlight: 3 })).run(canvas);
    expect(peak).toBe(1);
  });

  it("keeps playing while a decision is pending", async () => {
    const engine = new FakeEngine(60);
    const { api, log } = fakeApi(engine);
    await new SessionRunner(engine, api, cfg()).run(canvas);
    expect(log.decidedWhilePaused.some(Boolean)).toBe(false);
  });

  it("stops a walking player while a slow decision is pending ", async () => {
    const presses: boolean[] = [];
    const engine = new FakeEngine(3000);
    engine.setKey = (k, d) => {
      if (k === "forward") presses.push(d);
    };
    const { api } = fakeApi(engine, false, false, 80);
    const walk = await api.decide.bind(api);
    api.decide = async (...a) => ({ ...(await walk(...a)), intent: "advance" });
    await new SessionRunner(
      engine,
      api,
      cfg({ stopAfterMs: 10 }),
    ).run(canvas);
    // Without the stop the same intent is held and forward is pressed once. With it, each slow
    // decision lets go of the key and the answer presses it again.
    expect(presses.filter(Boolean).length).toBeGreaterThanOrEqual(2);
  });

  describe("overlapping decision calls", () => {
    const overlapped = (over: Partial<RunnerConfig> = {}) =>
      cfg({ maxInFlight: 3, decisionIntervalMs: 5, maxStaleTics: 100000, ...over });

    it("asks again before the last answer is back, but never exceeds the limit", async () => {
      const engine = new FakeEngine(1500);
      const { api } = fakeApi(engine);
      let now = 0;
      let peak = 0;
      const inner = api.decide.bind(api);
      api.decide = async (...a) => {
        now += 1;
        peak = Math.max(peak, now);
        await new Promise((r) => setTimeout(r, 40));
        try {
          return await inner(...a);
        } finally {
          now -= 1;
        }
      };
      await new SessionRunner(engine, api, overlapped()).run(canvas);
      expect(peak).toBe(3);
    });

    it("keeps one call at a time by default", async () => {
      const engine = new FakeEngine(800);
      const { api } = fakeApi(engine);
      let now = 0;
      let peak = 0;
      const inner = api.decide.bind(api);
      api.decide = async (...a) => {
        now += 1;
        peak = Math.max(peak, now);
        await new Promise((r) => setTimeout(r, 20));
        try {
          return await inner(...a);
        } finally {
          now -= 1;
        }
      };
      await new SessionRunner(engine, api, cfg()).run(canvas);
      expect(peak).toBe(1);
    });

    it("drops an answer that lands after a newer one", async () => {
      const pressed: string[] = [];
      const engine = new FakeEngine(2500);
      engine.setKey = (k, d) => {
        if (d) pressed.push(k);
      };
      const { api } = fakeApi(engine);
      let n = 0;
      api.decide = async () => {
        n += 1;
        const first = n === 1;
        await new Promise((r) => setTimeout(r, first ? 120 : 5));
        return {
          intent: first ? "turn_left" : "advance",
          confidence: null,
          threat_prob: null,
          danger_score: null,
          latency_ms: 1,
          error: null,
        } as Decision;
      };
      await new SessionRunner(engine, api, overlapped()).run(canvas);
      expect(pressed).toContain("forward");
      expect(pressed).not.toContain("left"); // the late turn was not acted on
    });

    it("drops an answer to a state that is too old by the time it lands", async () => {
      const pressed: string[] = [];
      const engine = new FakeEngine(2500);
      engine.setKey = (k, d) => {
        if (d) pressed.push(k);
      };
      const { api } = fakeApi(engine, false, false, 60);
      const inner = api.decide.bind(api);
      api.decide = async (...a) => ({ ...(await inner(...a)), intent: "advance" });
      // The mock moves the game on 5 tics every look, so 60 ms is far more than 10 tics.
      await new SessionRunner(engine, api, overlapped({ maxStaleTics: 10 })).run(canvas);
      expect(pressed).not.toContain("forward");
    });

    it("does not stop the walk on a slow answer, since the next one is already on its way", async () => {
      const presses: boolean[] = [];
      const engine = new FakeEngine(3000);
      engine.setKey = (k, d) => {
        if (k === "forward") presses.push(d);
      };
      const { api } = fakeApi(engine, false, false, 80);
      const inner = api.decide.bind(api);
      api.decide = async (...a) => ({ ...(await inner(...a)), intent: "advance" });
      await new SessionRunner(engine, api, overlapped({ stopAfterMs: 10 })).run(canvas);
      expect(presses.filter(Boolean).length).toBe(1);
    });
  });

  it("counts decision timeouts on the session and keeps the game moving", async () => {
    const engine = new FakeEngine(100);
    const { api, log } = fakeApi(engine, true);
    const errors: string[] = [];
    const result = await new SessionRunner(engine, api, cfg(), {
      onError: (m) => errors.push(m),
    }).run(canvas);
    expect(result.outcome).toBe("died");
    expect(log.ended[0].timeouts).toBeGreaterThan(0);
    expect(errors.some((m) => m.includes("did not answer"))).toBe(true);
    expect(engine.paused).toBe(true); // never left running when it finished
  });

  it("ends a crashed game loop as an error instead of hanging", async () => {
    const engine = new FakeEngine();
    const { api, log } = fakeApi(engine);
    let calls = 0;
    const original = engine.getState.bind(engine);
    engine.getState = () => {
      if (++calls > 6) throw new Error("wasm trapped");
      return original();
    };
    const errors: string[] = [];
    const result = await new SessionRunner(engine, api, cfg(), {
      onError: (m) => errors.push(m),
    }).run(canvas);
    expect(result.outcome).toBe("error");
    expect(log.ended.map((e) => e.outcome)).toEqual(["error"]);
    expect(errors.some((m) => m.includes("Game loop crashed: wasm trapped"))).toBe(true);
  });

  it("leaves the game paused when the session cannot be created", async () => {
    const engine = new FakeEngine();
    const { api, log } = fakeApi(engine, false, true);
    await expect(new SessionRunner(engine, api, cfg()).run(canvas)).rejects.toThrow(/503/);
    expect(engine.paused).toBe(true);
    expect(log.ended).toEqual([]);
  });
});
