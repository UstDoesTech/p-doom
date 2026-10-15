import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Engine } from "./engine/Engine";
import { MockEngine } from "./engine/MockEngine";
import { WasmEngine } from "./engine/WasmEngine";
import { SessionRunner } from "./runner";
import {
  PLAYER_TYPES,
  type Decision,
  type EngineKind,
  type GameState,
  type Intent,
  type Mode,
  type PlayerAvailability,
  type PlayerType,
} from "./types";

interface Hud {
  health: number;
  tic: number;
  enemies: number;
  intent: Intent | null;
}

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
  const [intervalMs, setIntervalMs] = useState(1000);
  const [timeoutS, setTimeoutS] = useState(120);

  const [running, setRunning] = useState(false);
  const [hud, setHud] = useState<Hud | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [result, setResult] = useState("");
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

  const availability = (pt: PlayerType) => players.find((p) => p.player_type === pt);

  const start = async () => {
    setError("");
    setResult("");
    setLog([]);
    setRunning(true);
    const engine = (engines.current[engineKind] ??=
      engineKind === "mock" ? new MockEngine() : new WasmEngine());

    const runner = new SessionRunner(
      engine,
      api,
      {
        playerType,
        mode,
        engine: engineKind,
        level,
        seed,
        decisionIntervalMs: intervalMs,
        eventEveryTics: 5,
        timeoutS,
      },
      {
        onTick: (s: GameState, intent) => {
          const now = performance.now();
          if (now - lastHud.current < 200) return;
          lastHud.current = now;
          setHud({ health: s.player.health, tic: s.tic, enemies: s.enemies.length, intent });
        },
        onDecision: (d: Decision, s: GameState) => {
          const line = d.error
            ? `tic ${s.tic}  error: ${d.error}`
            : `tic ${s.tic}  ${d.intent}  ${d.latency_ms} ms`;
          setLog((l) => [line, ...l].slice(0, 8));
        },
        onError: (m) => setError(m),
      },
    );
    runnerRef.current = runner;
    try {
      const canvas = (engineKind === "wasm" ? wasmCanvasRef : mockCanvasRef).current!;
      const r = await runner.run(canvas);
      setResult(
        r.outcome === "aborted"
          ? "Stopped. The attempt is recorded as aborted and does not count toward p(doom)."
          : `${r.outcome} after ${r.durationS.toFixed(1)} s, ${r.kills} kills`,
      );
      setSeed((s) => s + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      runnerRef.current = null;
      setRunning(false);
    }
  };

  return (
    <main>
      <header>
        <h1>p(doom)</h1>
        <p>Pick a player, press start, and the attempt lands in Databricks.</p>
      </header>

      <section className="panel controls">
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
        <label>
          Mode
          <select value={mode} disabled={running} onChange={(e) => setMode(e.target.value as Mode)}>
            <option value="paused">paused</option>
            <option value="realtime">realtime</option>
          </select>
        </label>
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
        </label>
        <label>
          Level
          <input value={level} disabled={running} onChange={(e) => setLevel(e.target.value)} />
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
            value={intervalMs}
            disabled={running}
            onChange={(e) => setIntervalMs(Number(e.target.value))}
          />
        </label>
        <label>
          Time cap (game s)
          <input
            type="number"
            value={timeoutS}
            disabled={running}
            onChange={(e) => setTimeoutS(Number(e.target.value))}
          />
        </label>
        {running ? (
          <button className="stop" onClick={() => runnerRef.current?.abort()}>
            Stop
          </button>
        ) : (
          <button onClick={start}>Start</button>
        )}
      </section>

      {engineKind === "mock" && (
        <p className="note">
          Mock arena sessions are stored with engine = mock. Exclude them from every result.
        </p>
      )}
      {error && <p className="error">{error}</p>}
      {result && <p className="result">{result}</p>}

      <section className="stage">
        <div className="screens">
          <canvas
            ref={mockCanvasRef}
            width={640}
            height={400}
            tabIndex={0}
            hidden={engineKind !== "mock"}
          />
          <canvas
            ref={wasmCanvasRef}
            id="canvas"
            width={640}
            height={400}
            tabIndex={0}
            hidden={engineKind !== "wasm"}
            onContextMenu={(e) => e.preventDefault()}
          />
        </div>
        <aside className="panel">
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
            </dl>
          ) : (
            <p>Waiting for a session.</p>
          )}
          <h2>Decisions</h2>
          <ul className="log">
            {log.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
          {playerType === "human" && (
            <p className="hint">Arrows move, comma and period strafe, Ctrl fires, Space uses.</p>
          )}
        </aside>
      </section>
    </main>
  );
}
