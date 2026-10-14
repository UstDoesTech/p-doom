import type { Api } from "./api";
import type { Engine } from "./engine/Engine";
import { TIC_RATE } from "./engine/mockWorld";
import { HumanInput } from "./human";
import { ReflexLayer } from "./reflex";
import { TelemetryBuffer } from "./telemetry";
import type {
  Decision,
  EngineKind,
  EventIn,
  GameState,
  Intent,
  Mode,
  Outcome,
  PlayerType,
} from "./types";

export interface RunnerConfig {
  playerType: PlayerType;
  mode: Mode;
  engine: EngineKind;
  level: string;
  seed: number;
  /** Gap between the end of one decision and the start of the next. */
  decisionIntervalMs: number;
  /** Sample one telemetry event every this many tics. */
  eventEveryTics: number;
  /** Game-time cap in seconds. The session ends as a timeout. */
  timeoutS: number;
  /** Milliseconds between loop ticks. */
  tickMs?: number;
}

export interface RunnerHooks {
  onTick?(state: GameState, intent: Intent | null): void;
  onDecision?(decision: Decision, state: GameState): void;
  onError?(message: string): void;
}

export interface RunResult {
  sessionId: string;
  /** "aborted" sessions are discarded: no session row is written, so they never count. */
  outcome: Outcome | "aborted";
  durationS: number;
  kills: number;
}

const sum = (values: Record<string, number>) => Object.values(values).reduce((a, b) => a + b, 0);

function toEvent(state: GameState, action: string | null): EventIn {
  const p = state.player;
  return {
    tic: state.tic,
    ts: new Date().toISOString(),
    health: p.health,
    armor: p.armor,
    weapon: p.weapon,
    ammo: sum(p.ammo),
    x: p.x,
    y: p.y,
    angle: p.angle,
    action,
  };
}

export class SessionRunner {
  private aborted = false;

  constructor(
    private engine: Engine,
    private api: Api,
    private cfg: RunnerConfig,
    private hooks: RunnerHooks = {},
  ) {}

  abort(): void {
    this.aborted = true;
  }

  async run(canvas: HTMLCanvasElement): Promise<RunResult> {
    const { cfg, engine, api, hooks } = this;
    const isHuman = cfg.playerType === "human";

    await engine.start(canvas, { level: cfg.level, seed: cfg.seed });
    if (cfg.mode === "paused" && !engine.canPause()) {
      throw new Error("This engine build cannot pause, so paused mode would be recorded wrongly.");
    }
    // Hold the game still while the session registers, so nobody plays unattended.
    if (engine.canPause()) engine.pause();
    let session: { session_id: string };
    try {
      session = await api.createSession({
        player_type: cfg.playerType,
        mode: cfg.mode,
        engine: cfg.engine,
        level: cfg.level,
        seed: cfg.seed,
      });
    } finally {
      engine.resume();
    }

    const telemetry = new TelemetryBuffer(api, session.session_id);
    const reflex = new ReflexLayer(engine);
    const human = new HumanInput(engine, engine.kind === "mock");
    if (isHuman) human.attach();
    telemetry.start();

    const startTic = engine.getState().tic;
    let lastSampled = -Infinity;
    let lastDecisionAt = -Infinity;
    let inFlight = false;
    let done = false;
    let lastState = engine.getState();

    const decide = async (state: GameState) => {
      const shot = cfg.playerType === "vision_llm" ? engine.screenshot() : null;
      if (cfg.mode === "paused") engine.pause();
      try {
        const decision = await api.decide(session.session_id, state, shot);
        if (done) return;
        if (decision.intent) reflex.setIntent(decision.intent);
        hooks.onDecision?.(decision, state);
      } catch (e) {
        hooks.onError?.(e instanceof Error ? e.message : String(e));
      } finally {
        if (cfg.mode === "paused" && !done) engine.resume();
        lastDecisionAt = performance.now();
        inFlight = false;
      }
    };

    const outcome = await new Promise<Outcome | "aborted">((resolve) => {
      const finish = (o: Outcome | "aborted") => {
        done = true;
        clearInterval(timer);
        resolve(o);
      };
      const timer = setInterval(() => {
        if (done) return;
        const state = engine.getState();
        const status = engine.status();
        lastState = state;

        if (state.tic - lastSampled >= cfg.eventEveryTics) {
          lastSampled = state.tic;
          telemetry.push(toEvent(state, isHuman ? human.action() : reflex.current()));
        }
        hooks.onTick?.(state, reflex.current());

        if (this.aborted) return finish("aborted");
        if (status !== "running") return finish(status);
        if ((state.tic - startTic) / TIC_RATE >= cfg.timeoutS) return finish("timeout");

        if (!isHuman) {
          reflex.apply();
          if (!inFlight && performance.now() - lastDecisionAt >= cfg.decisionIntervalMs) {
            inFlight = true;
            void decide(state);
          }
        }
      }, cfg.tickMs ?? 30);
    });

    reflex.releaseAll();
    human.detach();
    if (engine.canPause()) engine.pause();
    const result: RunResult = {
      sessionId: session.session_id,
      outcome,
      durationS: (lastState.tic - startTic) / TIC_RATE,
      kills: engine.kills(),
    };

    if (outcome === "aborted") {
      telemetry.discard();
      return result;
    }

    await telemetry.stop();
    if (telemetry.pending > 0) hooks.onError?.(`${telemetry.pending} events were not delivered`);
    await this.endWithRetry(session.session_id, outcome, result);
    return result;
  }

  private async endWithRetry(sessionId: string, outcome: Outcome, r: RunResult): Promise<void> {
    for (let attempt = 1; ; attempt++) {
      try {
        await this.api.endSession(sessionId, {
          outcome,
          duration_s: r.durationS,
          kills: r.kills,
        });
        return;
      } catch (e) {
        if (attempt >= 3) throw e;
        await new Promise((resolve) => setTimeout(resolve, 500 * attempt));
      }
    }
  }
}
