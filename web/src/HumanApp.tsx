import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { WasmEngine } from "./engine/WasmEngine";
import { EPISODE_TITLES, MAP_NAMES, OUTCOME_LABEL, nextLevel } from "./levels";
import { SessionRunner, type RunResult } from "./runner";
import type { GameState } from "./types";

/** The public app: people only, the real Doom engine, nothing to configure. Every game is a
 * session in the same tables the other players write to, with player_type = human. */

/** Episodes offered. The shareware WAD that the engine build fetches holds episode 1 only. */
const EPISODES = Number(import.meta.env.VITE_EPISODES ?? 1);

const CONTROLS = "Arrows move and turn, comma and period strafe, Ctrl fires, Space opens doors.";

const randomSeed = () => Math.floor(Math.random() * 1_000_000);

interface Done {
  r: RunResult;
  level: string;
}

export default function HumanApp() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const runnerRef = useRef<SessionRunner | null>(null);
  const engine = useRef<WasmEngine | null>(null);
  const lastHud = useRef(0);
  const stopped = useRef(false);
  const liveSeen = useRef(false);

  const [level, setLevel] = useState("E1M1");
  const [running, setRunning] = useState(false);
  // True once the game is on screen and listening for keys. Booting the engine takes a few seconds.
  const [live, setLive] = useState(false);
  const [focused, setFocused] = useState(false);
  const [health, setHealth] = useState<number | null>(null);
  const [done, setDone] = useState<Done | null>(null);
  const [error, setError] = useState("");
  // null while probing, then whether the engine files are being served.
  const [engineReady, setEngineReady] = useState<boolean | null>(null);
  const touch = typeof window !== "undefined" && window.matchMedia?.("(pointer: coarse)").matches;

  useEffect(() => {
    document.title = "Play Doom · p(doom)";
    const base = import.meta.env.VITE_DOOM_BASE ?? "/doom/";
    const loader = import.meta.env.VITE_DOOM_LOADER ?? "websockets-doom.js";
    fetch(base + loader, { method: "HEAD" })
      .then((r) => setEngineReady(r.ok))
      .catch(() => setEngineReady(false));
  }, []);

  /** One game. Returns null when it could not start; the reason is shown on the page. */
  const play = async (lvl: string): Promise<RunResult | null> => {
    setError("");
    setDone(null);
    setHealth(null);
    setLive(false);
    setRunning(true);
    engine.current ??= new WasmEngine();
    const runner = new SessionRunner(
      engine.current,
      api,
      {
        playerType: "human",
        mode: "realtime",
        engine: "wasm",
        level: lvl,
        seed: randomSeed(),
        decisionIntervalMs: 1000,
        eventEveryTics: 5,
        timeoutS: 0,
      },
      {
        onTick: (s: GameState) => {
          if (!liveSeen.current) {
            liveSeen.current = true;
            setLive(true);
            canvasRef.current?.focus(); // the Play click was the gesture; no second click needed
          }
          const now = performance.now();
          if (now - lastHud.current < 200) return;
          lastHud.current = now;
          setHealth(s.player.health);
        },
        onError: (m) => setError(m),
      },
    );
    runnerRef.current = runner;
    liveSeen.current = false;
    try {
      const r = await runner.run(canvasRef.current!);
      setDone({ r, level: lvl });
      return r;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      runnerRef.current = null;
      setRunning(false);
    }
  };

  const start = async (from = level) => {
    stopped.current = false;
    setLevel(from);
    await play(from);
  };

  const next = async (r: RunResult, from: string) => {
    const lvl = nextLevel(from);
    if (r.outcome !== "cleared" || !lvl || stopped.current || Number(lvl[1]) > EPISODES) return;
    await start(lvl);
  };

  const stop = () => {
    stopped.current = true;
    runnerRef.current?.abort();
  };

  const following = done && !running ? nextLevel(done.level) : null;
  const canContinue =
    done?.r.outcome === "cleared" && following !== null && Number(following[1]) <= EPISODES;
  const unavailable = engineReady === false;

  return (
    <main className="human">
      <header className="top">
        <h1>
          <img className="logo" src="/doom-logo.png" alt="p(doom)" width={128} height={64} />
        </h1>
        <div className="strap">
          <p>Probability you die before you complete the level</p>
          <p className="sub">Delivering more shareholder value since 2026 by letting machines play games on our behalf.</p>
        </div>
      </header>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {unavailable && (
        <p className="error" role="alert">
          The game engine is not available right now. Try again later.
        </p>
      )}
      {touch && (
        <p className="note">This needs a keyboard. On a phone or tablet the game cannot be played.</p>
      )}

      <section className="screens solo">
        <div className="canvas-wrap">
          <canvas
            ref={canvasRef}
            id="canvas"
            width={640}
            height={400}
            tabIndex={0}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onContextMenu={(e) => e.preventDefault()}
            aria-label="Doom"
          />
          {!running && (
            <div className="overlay">
              <div>
                <p className="big">{done ? "Run over" : "Ready when you are"}</p>
                <p>{CONTROLS}</p>
              </div>
            </div>
          )}
          {running && !live && (
            <div className="overlay">
              <div>
                <p className="big">Loading Doom</p>
                <p>The first game takes a few seconds to start.</p>
              </div>
            </div>
          )}
          {running && live && !focused && (
            <div className="overlay">
              <div>
                <p className="big">Click the game to take control</p>
                <p>{CONTROLS}</p>
              </div>
            </div>
          )}
        </div>

        <div className="actionbar">
          {running ? (
            <button className="stop" onClick={stop}>
              Stop
            </button>
          ) : (
            <button onClick={() => start()} disabled={unavailable || engineReady === null}>
              {done ? "Play again" : "Play"}
            </button>
          )}
          <label className="inline">
            Level
            <select value={level} disabled={running} onChange={(e) => setLevel(e.target.value)}>
              {Array.from({ length: EPISODES }, (_, i) => i + 1).map((ep) => (
                <optgroup key={ep} label={EPISODE_TITLES[ep]}>
                  {MAP_NAMES[ep].map((name, i) => (
                    <option key={i} value={`E${ep}M${i + 1}`}>
                      E{ep}M{i + 1} {name}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </label>
          <span className={running ? "status live" : "status"} aria-live="polite">
            {running
              ? live
                ? `Playing ${level}${health !== null ? ` · health ${health}` : ""}`
                : "Starting…"
              : "Ready"}
          </span>
        </div>

        {done && !running && (
          <div className={`card outcome-${done.r.outcome}`} role="status">
            <span className="badge">{OUTCOME_LABEL[done.r.outcome] ?? done.r.outcome}</span>
            <span>{done.level}</span>
            <span className="stat">{done.r.durationS.toFixed(1)} s</span>
            <span className="stat">{done.r.kills} kills</span>
            {canContinue && (
              <button onClick={() => next(done.r, done.level)}>Next level, {following}</button>
            )}
          </div>
        )}
        {done?.r.outcome === "aborted" && (
          <p className="result">Stopped. It is recorded as aborted and does not count toward p(doom).</p>
        )}
      </section>

      <p className="hint">
        Games are stored with no name or account, so they can be compared with how AI players do on the
        same levels.
      </p>
    </main>
  );
}
