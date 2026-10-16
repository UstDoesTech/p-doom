import { ApiTimeout, type Api } from "./api";
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
  /** Realtime only: a decision still pending after this long stops the player moving until it
   * lands. Default 300 ms. Players that answer faster never notice it. */
  stopAfterMs?: number;
  engine: EngineKind;
  level: string;
  seed: number;
  /** Gap between the end of one decision and the start of the next. */
  decisionIntervalMs: number;
  /** Sample one telemetry event every this many tics. */
  eventEveryTics: number;
  /** Game-time cap in seconds. The session ends as a timeout. 0 means no cap: play until the
   * level is cleared or the player dies, or Stop is pressed. */
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
  outcome: Outcome;
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
  private abandoned = false;

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
    const hold = () => {
      if (engine.canPause()) engine.pause();
    };

    await engine.start(canvas, { level: cfg.level, seed: cfg.seed });
    let session: { session_id: string };
    try {
      if (cfg.mode === "paused" && !engine.canPause()) {
        throw new Error("This engine build cannot pause, so paused mode would be recorded wrongly.");
      }
      // Hold the game still while the session registers, so nobody plays unattended.
      hold();
      session = await api.createSession({
        player_type: cfg.playerType,
        mode: cfg.mode,
        engine: cfg.engine,
        level: cfg.level,
        seed: cfg.seed,
      });
    } catch (e) {
      hold(); // no session, so nothing may keep playing behind the error message
      throw e;
    }
    engine.resume();

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
    let decisionTimeouts = 0;

    const decide = async (state: GameState) => {
      const shot = cfg.playerType === "vision_llm" ? engine.screenshot() : null;
      if (cfg.mode === "paused") engine.pause();
      const stopTimer =
        cfg.mode === "realtime"
          ? setTimeout(() => reflex.stopMoving(), cfg.stopAfterMs ?? 300)
          : undefined;
      try {
        const decision = await api.decide(session.session_id, state, shot);
        if (done) return;
        if (decision.intent) {
          const hold = decision.hold_tics;
          reflex.setIntent(decision.intent, hold ? engine.getState().tic + hold : null);
        }
        hooks.onDecision?.(decision, state);
      } catch (e) {
        // The server may still log this decision. It is counted here as well so the session row
        // shows a player that kept timing out, not only one that returned errors.
        if (e instanceof ApiTimeout) decisionTimeouts += 1;
        hooks.onError?.(e instanceof Error ? e.message : String(e));
      } finally {
        clearTimeout(stopTimer);
        if (cfg.mode === "paused" && !done) engine.resume();
        lastDecisionAt = performance.now();
        inFlight = false;
      }
    };

    // Closing the tab mid-game: the normal end call would never finish, so send a keepalive one.
    const onHide = () => {
      if (done || this.abandoned) return;
      this.abandoned = true;
      api.abandonSession(session.session_id, {
        duration_s: (lastState.tic - startTic) / TIC_RATE,
        kills: engine.kills(),
        decision_timeouts: decisionTimeouts,
      });
    };
    globalThis.addEventListener?.("pagehide", onHide);

    const outcome = await new Promise<Outcome>((resolve) => {
      const finish = (o: Outcome) => {
        done = true;
        clearInterval(timer);
        resolve(o);
      };
      const timer = setInterval(() => {
        if (done) return;
        try {
          const state = engine.getState();
          const status = engine.status();
          lastState = state;

          if (state.tic - lastSampled >= cfg.eventEveryTics) {
            lastSampled = state.tic;
            telemetry.push(toEvent(state, isHuman ? human.action() : reflex.current()));
          }
          hooks.onTick?.(state, reflex.current());

          if (this.abandoned) return finish("abandoned");
          if (this.aborted) return finish("aborted");
          if (status !== "running") return finish(status);
          if (cfg.timeoutS > 0 && (state.tic - startTic) / TIC_RATE >= cfg.timeoutS)
            return finish("timeout");

          if (!isHuman) {
            reflex.apply(state.tic);
            if (!inFlight && performance.now() - lastDecisionAt >= cfg.decisionIntervalMs) {
              inFlight = true;
              void decide(state);
            }
          }
        } catch (e) {
          // A throw inside a timer callback would otherwise leave this promise pending for ever
          // and the session without an end.
          hooks.onError?.(`Game loop crashed: ${e instanceof Error ? e.message : String(e)}`);
          finish("error");
        }
      }, cfg.tickMs ?? 30);
    });

    globalThis.removeEventListener?.("pagehide", onHide);
    reflex.releaseAll();
    human.detach();
    hold();
    let kills = 0;
    try {
      kills = engine.kills();
    } catch {
      /* a crashed engine may not answer; the row is still worth writing */
    }
    const result: RunResult = {
      sessionId: session.session_id,
      outcome,
      durationS: (lastState.tic - startTic) / TIC_RATE,
      kills,
    };

    // The keepalive call already ended an abandoned session; the page is going away.
    if (outcome === "abandoned") return result;

    await telemetry.stop();
    if (telemetry.pending > 0) hooks.onError?.(`${telemetry.pending} events were not delivered`);
    await this.endWithRetry(session.session_id, outcome, result, decisionTimeouts);
    return result;
  }

  private async endWithRetry(
    sessionId: string,
    outcome: Outcome,
    r: RunResult,
    decisionTimeouts: number,
  ): Promise<void> {
    for (let attempt = 1; ; attempt++) {
      try {
        await this.api.endSession(sessionId, {
          outcome,
          duration_s: r.durationS,
          kills: r.kills,
          decision_timeouts: decisionTimeouts,
        });
        return;
      } catch (e) {
        if (attempt >= 3) throw e;
        await new Promise((resolve) => setTimeout(resolve, 500 * attempt));
      }
    }
  }
}
