// Drives the Emscripten build of Chocolate Doom described in engine/README.md.
// UNTESTED against a real build: the engine shim (engine/pdoom_state.c) has not been compiled
// yet. The contract it must meet is spelled out below.
//
// The build must export, via EXPORTED_FUNCTIONS and EXPORTED_RUNTIME_METHODS:
//   pdoom_get_state() -> char*   JSON: GameState fields plus "status" and "kills"
//   pdoom_new_game(int episode, int map, int seed) -> void
//   ccall, pauseMainLoop, resumeMainLoop

import type { EngineStatus, GameKey, GameState } from "../types";
import type { Engine } from "./Engine";

interface EmscriptenModule {
  canvas: HTMLCanvasElement;
  arguments: string[];
  locateFile: (path: string) => string;
  onRuntimeInitialized: () => void;
  print: (text: string) => void;
  printErr: (text: string) => void;
  ccall: (name: string, ret: string | null, argTypes: string[], args: unknown[]) => unknown;
  pauseMainLoop?: () => void;
  resumeMainLoop?: () => void;
}

type WasmState = GameState & { status: EngineStatus; kills: number };

// Doom's default bindings: arrows move and turn, comma and period strafe, Ctrl fires, Space uses.
const KEYS: Record<GameKey, { key: string; code: string; keyCode: number }> = {
  forward: { key: "ArrowUp", code: "ArrowUp", keyCode: 38 },
  back: { key: "ArrowDown", code: "ArrowDown", keyCode: 40 },
  left: { key: "ArrowLeft", code: "ArrowLeft", keyCode: 37 },
  right: { key: "ArrowRight", code: "ArrowRight", keyCode: 39 },
  strafe_left: { key: ",", code: "Comma", keyCode: 188 },
  strafe_right: { key: ".", code: "Period", keyCode: 190 },
  fire: { key: "Control", code: "ControlLeft", keyCode: 17 },
  use: { key: " ", code: "Space", keyCode: 32 },
};

// Emscripten modules cannot be reloaded, so the engine boots once per page and later sessions
// start through pdoom_new_game.
let booted: Promise<EmscriptenModule> | null = null;

export class WasmEngine implements Engine {
  readonly kind = "wasm" as const;
  private mod?: EmscriptenModule;
  private last?: WasmState;

  constructor(
    private baseUrl = import.meta.env.VITE_DOOM_BASE ?? "/doom/",
    private loader = import.meta.env.VITE_DOOM_LOADER ?? "pdoom.js",
  ) {}

  async start(canvas: HTMLCanvasElement, opts: { level: string; seed: number }): Promise<void> {
    const match = /^E(\d)M(\d)$/i.exec(opts.level);
    if (!match) throw new Error(`WASM engine supports levels like E1M1, got ${opts.level}`);
    const [episode, map] = [Number(match[1]), Number(match[2])];

    booted ??= this.boot(canvas, episode, map);
    this.mod = await booted;
    this.mod.ccall("pdoom_new_game", null, ["number", "number", "number"], [episode, map, opts.seed]);
  }

  private boot(canvas: HTMLCanvasElement, episode: number, map: number): Promise<EmscriptenModule> {
    return new Promise((resolve, reject) => {
      const mod = {
        canvas,
        arguments: ["-iwad", "doom1.wad", "-warp", String(episode), String(map)],
        locateFile: (path: string) => this.baseUrl + path,
        onRuntimeInitialized: () => resolve(mod),
        print: (t: string) => console.log(t),
        printErr: (t: string) => console.warn(t),
      } as EmscriptenModule;
      (window as unknown as { Module: EmscriptenModule }).Module = mod;
      const script = document.createElement("script");
      script.src = this.baseUrl + this.loader;
      script.onerror = () => {
        booted = null;
        reject(new Error(`Could not load ${script.src}. Build the engine first: engine/README.md`));
      };
      document.body.appendChild(script);
    });
  }

  private read(): WasmState {
    if (!this.mod) throw new Error("Engine not started");
    const json = this.mod.ccall("pdoom_get_state", "string", [], []) as string;
    this.last = JSON.parse(json) as WasmState;
    return this.last;
  }

  getState(): GameState {
    const { status: _status, kills: _kills, ...state } = this.read();
    return state;
  }
  status(): EngineStatus {
    return (this.last ?? this.read()).status;
  }
  kills(): number {
    return (this.last ?? this.read()).kills;
  }

  setKey(key: GameKey, down: boolean): void {
    const k = KEYS[key];
    const init = { key: k.key, code: k.code, keyCode: k.keyCode, which: k.keyCode, bubbles: true };
    document.dispatchEvent(new KeyboardEvent(down ? "keydown" : "keyup", init));
  }

  canPause(): boolean {
    return typeof this.mod?.pauseMainLoop === "function";
  }
  pause(): void {
    this.mod?.pauseMainLoop?.();
  }
  resume(): void {
    this.mod?.resumeMainLoop?.();
  }

  screenshot(): string | null {
    // A WebGL canvas reads back blank unless the build keeps its drawing buffer.
    const canvas = this.mod?.canvas;
    return canvas ? canvas.toDataURL("image/jpeg", 0.6).split(",")[1] : null;
  }

  stop(): void {
    // Booted engines stay alive for the next session.
  }
}
