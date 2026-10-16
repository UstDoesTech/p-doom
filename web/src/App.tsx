import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Engine } from "./engine/Engine";
import { MockEngine } from "./engine/MockEngine";
import { WasmEngine } from "./engine/WasmEngine";
import { planSet, runSet } from "./batch";
import { SessionRunner, type RunResult } from "./runner";
import {
  PLAYER_TYPES,
  type Decision,
  type EngineKind,
  type GameState,
  type Intent,
  type Mode,
  type PlayerAvailability,
  type PlayerType,
  type Route,
} from "./types";

/** Players that answer in milliseconds, so they can decide several times a second and steer. */
const LOCAL_PLAYERS: PlayerType[] = ["rule_bot"];

interface Hud {
  health: number;
  tic: number;
  enemies: number;
  intent: Intent | null;
  route: Route | null;
}

interface LogLine {
  text: string;
  intent?: string;
  error?: boolean;
}

type Tab = "watch" | "batch";

const MAP_NAMES: Record<number, string[]> = {
  1: ["Hangar", "Nuclear Plant", "Toxin Refinery", "Command Control", "Phobos Lab", "Central Processing", "Computer Station", "Phobos Anomaly", "Military Base"],
  2: ["Deimos Anomaly", "Containment Area", "Refinery", "Deimos Lab", "Command Center", "Halls of the Damned", "Spawning Vats", "Tower of Babel", "Fortress of Mystery"],
  3: ["Hell Keep", "Slough of Despair", "Pandemonium", "House of Pain", "Unholy Cathedral", "Mt. Erebus", "Limbo", "Dis", "Warrens"],
  4: ["Hell Beneath", "Perfect Hatred", "Sever the Wicked", "Unruly Evil", "They Will Repent", "Against Thee Wickedly", "And Hell Followed", "Unto the Cruel", "Fear"],
};
const EPISODE_TITLES: Record<number, string> = {
  1: "Episode 1: Knee-Deep in the Dead",
  2: "Episode 2: The Shores of Hell",
  3: "Episode 3: Inferno",
  4: "Episode 4: Thy Flesh Consumed",
};

const OUTCOME_LABEL: Record<string, string> = {
  cleared: "Cleared",
  died: "Died",
  timeout: "Timed out",
  aborted: "Stopped",
  abandoned: "Abandoned",
  error: "Error",
};

const VIA_LABEL: Record<string, string> = {
  portal: "teleporter",
  door: "door",
  exit: "exit",
  switch: "switch",
  walk: "walk-over trigger",
};

export default function App() {
  const mockCanvasRef = useRef<HTMLCanvasElement>(null);
  const wasmCanvasRef = useRef<HTMLCanvasElement>(null);
  const runnerRef = useRef<SessionRunner | null>(null);
  const engines = useRef<Partial<Record<EngineKind, Engine>>>({});
  const lastHud = useRef(0);

  const [players, setPlayers] = useState<PlayerAvailability[]>([]);
  const [playerType, setPlayerType] = useState<PlayerType>("rule_bot");
  const [mode, setMode] = useState<Mode>("paused");
  const [engineKind, setEngineKind] = useState<EngineKind>(
    import.meta.env.VITE_DEFAULT_ENGINE === "wasm" ? "wasm" : "mock",
  );
  // null while probing, then whether the built engine files are being served.
  const [wasmAvailable, setWasmAvailable] = useState<boolean | null>(null);
  const [level, setLevel] = useState("E1M1");
  const [seed, setSeed] = useState(1);
  const [seedTo, setSeedTo] = useState(10);
  const [roster, setRoster] = useState<PlayerType[]>(["rule_bot"]);
  const [setNote, setSetNote] = useState("");
  const stopSet = useRef(false);
  // null until the user types one: a local player is cheap to ask, so it decides more often.
  const [gapOverride, setGapOverride] = useState<number | null>(null);
  const [timeoutS, setTimeoutS] = useState(0);

  const [running, setRunning] = useState(false);
  const [hud, setHud] = useState<Hud | null>(null);
  const [log, setLog] = useState<LogLine[]>([]);
  const [result, setResult] = useState("");
  const [card, setCard] = useState<{ pt: PlayerType; seed: number; r: RunResult } | null>(null);
  const [tab, setTab] = useState<Tab>("watch");
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [focused, setFocused] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .players()
      .then(setPlayers)
      .catch((e) => setError(`Backend not reachable: ${e.message}`));
  }, []);

  useEffect(() => {
    const base = import.meta.env.VITE_DOOM_BASE ?? "/doom/";
    const loader = import.meta.env.VITE_DOOM_LOADER ?? "websockets-doom.js";
    fetch(base + loader, { method: "HEAD" })
      .then((r) => setWasmAvailable(r.ok))
      .catch(() => setWasmAvailable(false));
  }, []);

  useEffect(() => {
    if (wasmAvailable === false && engineKind === "wasm") setEngineKind("mock");
  }, [wasmAvailable, engineKind]);

  const gapFor = (pt: PlayerType) => gapOverride ?? (LOCAL_PLAYERS.includes(pt) ? 250 : 1000);

  const availability = (pt: PlayerType) => players.find((p) => p.player_type === pt);

  /** One game. Returns null when it could not start, with the reason shown on the page. */
  const runOne = async (pt: PlayerType, sd: number): Promise<RunResult | null> => {
    setError("");
    setResult("");
    setCard(null);
    setLog([]);
    setRunning(true);
    const engine = (engines.current[engineKind] ??=
      engineKind === "mock" ? new MockEngine() : new WasmEngine());

    const runner = new SessionRunner(
      engine,
      api,
      {
        playerType: pt,
        mode,
        engine: engineKind,
        level,
        seed: sd,
        decisionIntervalMs: gapFor(pt),
        eventEveryTics: 5,
        timeoutS,
      },
      {
        onTick: (s: GameState, intent) => {
          const now = performance.now();
          if (now - lastHud.current < 200) return;
          lastHud.current = now;
          setHud({
            health: s.player.health,
            tic: s.tic,
            enemies: s.enemies.length,
            intent,
            route: s.route ?? null,
          });
        },
        onDecision: (d: Decision, s: GameState) => {
          const line: LogLine = d.error
            ? { text: `tic ${s.tic}  error: ${d.error}`, error: true }
            : { text: `tic ${s.tic}  ${d.latency_ms} ms`, intent: d.intent ?? undefined };
          setLog((l) => [line, ...l].slice(0, 40));
        },
        onError: (m) => setError(m),
      },
    );
    runnerRef.current = runner;
    try {
      const canvas = (engineKind === "wasm" ? wasmCanvasRef : mockCanvasRef).current!;
      const r = await runner.run(canvas);
      setCard({ pt, seed: sd, r });
      setResult(
        r.outcome === "aborted"
          ? "Stopped. The attempt is recorded as aborted and does not count toward p(doom)."
          : r.outcome === "error"
            ? "The game loop crashed. Recorded as an error; it does not count toward p(doom)."
            : "",
      );
      return r;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      runnerRef.current = null;
      setRunning(false);
    }
  };

  const start = async () => {
    const r = await runOne(playerType, seed);
    if (r) setSeed((s) => s + 1);
  };

  const startSet = async () => {
    stopSet.current = false;
    const jobs = planSet(roster, seed, seedTo);
    setSetNote(`Set of ${jobs.length} games`);
    setProgress({ done: 0, total: jobs.length });
    const out = await runSet(
      jobs,
      async (job) => (await runOne(job.playerType, job.seed)) ?? { outcome: "error" },
      {
        stop: () => stopSet.current,
        onProgress: (p) => {
          setProgress({ done: p.done, total: p.total });
          setSetNote(`Game ${p.done + 1} of ${p.total}: ${p.job.playerType}, seed ${p.job.seed}`);
        },
      },
    );
    setProgress(null);
    setSetNote(
      out.stoppedBy === "done"
        ? `Set finished: ${out.finished} games`
        : `Set stopped after ${out.finished} of ${jobs.length} games`,
    );
  };

  const stop = () => {
    stopSet.current = true;
    runnerRef.current?.abort();
  };

  const copyLog = () => {
    const text = log
      .slice()
      .reverse()
      .map((l) => (l.intent ? `${l.text}  ${l.intent}` : l.text))
      .join("\n");
    navigator.clipboard
      ?.writeText(text)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {});
  };

  const mock = engineKind === "mock";
  const route = hud?.route ?? null;
  const showFocusHint = running && playerType === "human" && !focused;
  const canvasProps = {
    width: 640,
    height: 400,
    tabIndex: 0,
    onFocus: () => setFocused(true),
    onBlur: () => setFocused(false),
  };

  const playerSelect = (
    <label>
      Player
      <select
        value={playerType}
        disabled={running}
        onChange={(e) => setPlayerType(e.target.value as PlayerType)}
      >
        {PLAYER_TYPES.map((pt) => {
          const a = availability(pt);
          const unavailable = pt !== "human" && a && !a.available;
          return (
            <option key={pt} value={pt} disabled={!!unavailable} title={a?.reason ?? ""}>
              {pt}
              {unavailable ? ` (${a?.reason})` : ""}
            </option>
          );
        })}
      </select>
    </label>
  );

  const levelSelect = (
    <label>
      Level
      <select
        value={level}
        disabled={running || mock}
        onChange={(e) => setLevel(e.target.value)}
        title={mock ? "The mock arena has one fixed level" : ""}
      >
        {mock && <option value={level}>{level} (mock arena)</option>}
        {!mock &&
          [1, 2, 3, 4].map((ep) => (
            <optgroup
              key={ep}
              label={EPISODE_TITLES[ep] + (ep > 1 ? " (needs the full WAD)" : "")}
            >
              {MAP_NAMES[ep].map((name, i) => (
                <option key={i} value={`E${ep}M${i + 1}`}>
                  E{ep}M{i + 1} {name}
                </option>
              ))}
            </optgroup>
          ))}
      </select>
    </label>
  );

  const engineSelect = (
    <label>
      Engine
      <select
        value={engineKind}
        disabled={running}
        onChange={(e) => setEngineKind(e.target.value as EngineKind)}
      >
        <option value="mock">mock arena (pipeline test)</option>
        <option value="wasm" disabled={wasmAvailable === false}>
          Doom (WASM){wasmAvailable === false ? " - engine files not deployed" : ""}
        </option>
      </select>
      {mock && (
        <span className="field-warn">
          Mock sessions are stored with engine = mock. Exclude them from every result.
        </span>
      )}
    </label>
  );

  return (
    <main>
      <header className="top">
        <h1>p(doom)</h1>
        <p>Pick a player, press start, and the attempt lands in Databricks.</p>
      </header>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <section className="stage">
        <div className="screens">
          <div className="canvas-wrap">
            <canvas ref={mockCanvasRef} {...canvasProps} hidden={!mock} />
            <canvas
              ref={wasmCanvasRef}
              id="canvas"
              {...canvasProps}
              hidden={engineKind !== "wasm"}
              onContextMenu={(e) => e.preventDefault()}
            />
            {showFocusHint && (
              <div className="overlay">
                Click the game to take control.
                <br />
                Arrows move, comma and period strafe, Ctrl fires, Space uses.
              </div>
            )}
          </div>
          <div className="actionbar">
            {running ? (
              <button className="stop" onClick={stop}>
                Stop
              </button>
            ) : (
              <button onClick={start}>Start</button>
            )}
            <span className={running ? "status live" : "status"}>
              {running ? `Running: ${playerType} on ${level}, seed ${seed}` : "Ready"}
            </span>
          </div>
          {card && !running && (
            <div className={`card outcome-${card.r.outcome}`} role="status">
              <span className="badge">{OUTCOME_LABEL[card.r.outcome] ?? card.r.outcome}</span>
              <span>
                {card.pt} on seed {card.seed}
              </span>
              <span className="stat">{card.r.durationS.toFixed(1)} s</span>
              <span className="stat">{card.r.kills} kills</span>
              <span className="stat session">session {card.r.sessionId?.slice(0, 8)}</span>
              <button className="ghost" disabled={running} onClick={start}>
                Run seed {seed}
              </button>
            </div>
          )}
          {result && <p className="result">{result}</p>}
        </div>
        <aside className="panel side">
          <h2>Now</h2>
          {hud ? (
            <dl>
              <dt>Health</dt>
              <dd>{hud.health}</dd>
              <dt>Tic</dt>
              <dd>{hud.tic}</dd>
              <dt>Enemies in view</dt>
              <dd>{hud.enemies}</dd>
              <dt>Intent</dt>
              <dd>{hud.intent ?? "none"}</dd>
              <dt>Heading</dt>
              <dd className="heading">
                {route ? (
                  <>
                    <span
                      className="compass"
                      style={{ transform: `rotate(${route.bearing}deg)` }}
                      aria-hidden="true"
                    >
                      ↑
                    </span>
                    {VIA_LABEL[route.via] ?? route.via}, {Math.round(route.distance)} units
                    <span className="sub">{route.remaining} cells to the exit</span>
                  </>
                ) : (
                  "no route"
                )}
              </dd>
            </dl>
          ) : (
            <p>Waiting for a session.</p>
          )}
          <div className="log-head">
            <h2>Decisions</h2>
            <button className="ghost small" onClick={copyLog} disabled={log.length === 0}>
              {copied ? "Copied" : "Copy log"}
            </button>
          </div>
          <ul className="log">
            {log.map((l, i) => (
              <li key={i} className={l.error ? "err" : ""}>
                {l.text}
                {l.intent && <span className={`chip chip-${l.intent}`}>{l.intent}</span>}
              </li>
            ))}
          </ul>
          {playerType === "human" && (
            <p className="hint">Arrows move, comma and period strafe, Ctrl fires, Space uses.</p>
          )}
        </aside>
      </section>

      <div className="tabs" role="tablist">
        <button
          role="tab"
          aria-selected={tab === "watch"}
          className={tab === "watch" ? "tab on" : "tab"}
          onClick={() => setTab("watch")}
        >
          Watch one game
        </button>
        <button
          role="tab"
          aria-selected={tab === "batch"}
          className={tab === "batch" ? "tab on" : "tab"}
          onClick={() => setTab("batch")}
        >
          Run a batch
        </button>
      </div>

      {tab === "watch" ? (
        <section className="panel controls">
          {playerSelect}
          {levelSelect}
          {engineSelect}
          <details className="advanced">
            <summary>Advanced</summary>
            <div className="controls inner">
              <label>
                Mode
                <select
                  value={mode}
                  disabled={running}
                  onChange={(e) => setMode(e.target.value as Mode)}
                >
                  <option value="paused">paused</option>
                  <option value="realtime">realtime</option>
                </select>
              </label>
              <label>
                Seed
                <input
                  type="number"
                  value={seed}
                  disabled={running}
                  onChange={(e) => setSeed(Number(e.target.value))}
                />
              </label>
              <label>
                Decision gap (ms)
                <input
                  type="number"
                  value={gapOverride ?? gapFor(playerType)}
                  disabled={running}
                  onChange={(e) => setGapOverride(Number(e.target.value))}
                />
              </label>
              <label>
                Time cap (game s, 0 = none)
                <input
                  type="number"
                  value={timeoutS}
                  disabled={running}
                  onChange={(e) => setTimeoutS(Number(e.target.value))}
                />
              </label>
            </div>
          </details>
        </section>
      ) : (
        <section className="panel controls batch">
          <fieldset disabled={running}>
            <legend>Players</legend>
            {PLAYER_TYPES.filter((pt) => pt !== "human").map((pt) => {
              const a = availability(pt);
              const off = !!a && !a.available;
              return (
                <label key={pt} className="check" title={a?.reason ?? ""}>
                  <input
                    type="checkbox"
                    disabled={running || off}
                    checked={roster.includes(pt)}
                    onChange={(e) =>
                      setRoster((cur) =>
                        e.target.checked ? [...cur, pt] : cur.filter((x) => x !== pt),
                      )
                    }
                  />
                  {pt}
                </label>
              );
            })}
          </fieldset>
          {levelSelect}
          {engineSelect}
          <label>
            First seed
            <input
              type="number"
              value={seed}
              disabled={running}
              onChange={(e) => setSeed(Number(e.target.value))}
            />
          </label>
          <label>
            Last seed
            <input
              type="number"
              value={seedTo}
              disabled={running}
              onChange={(e) => setSeedTo(Number(e.target.value))}
            />
          </label>
          <div className="batch-run">
            <button
              disabled={running || roster.length === 0 || seedTo < seed}
              onClick={startSet}
            >
              Run {roster.length * Math.max(0, seedTo - seed + 1)} games
            </button>
            {progress && (
              <progress value={progress.done} max={progress.total}>
                {progress.done} of {progress.total}
              </progress>
            )}
            {setNote && <span className="note">{setNote}</span>}
          </div>
        </section>
      )}
    </main>
  );
}
