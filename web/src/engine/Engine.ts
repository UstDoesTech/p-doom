import type { EngineKind, EngineStatus, GameKey, GameState } from "../types";

/** What the session runner needs from a Doom engine. The mock arena and the WASM build both fit. */
export interface Engine {
  readonly kind: EngineKind;
  /** Boot the engine, or start a new game on an engine that is already booted. */
  start(canvas: HTMLCanvasElement, opts: { level: string; seed: number }): Promise<void>;
  getState(): GameState;
  status(): EngineStatus;
  kills(): number;
  setKey(key: GameKey, down: boolean): void;
  /** False when the engine cannot honour paused mode. The runner refuses to record such sessions. */
  canPause(): boolean;
  pause(): void;
  resume(): void;
  /** Base64 JPEG without the data: prefix, or null when the canvas cannot be read. */
  screenshot(): string | null;
  stop(): void;
}
