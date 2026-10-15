// Mirrors backend/src/pdoom/models.py.
// Bearing: degrees relative to the direction the player faces, -180 to 180,
// positive to the player's right (clockwise).

export const INTENTS = [
  "advance",
  "turn_left",
  "turn_right",
  "strafe_left",
  "strafe_right",
  "fire",
  "retreat",
  "use",
] as const;
export type Intent = (typeof INTENTS)[number];

export const PLAYER_TYPES = [
  "human",
  "rule_bot",
  "ai_decide",
  "vision_llm",
  "tool_agent",
  "rl_policy",
  "bc_policy",
  "supervisor",
] as const;
export type PlayerType = (typeof PLAYER_TYPES)[number];

export type Mode = "paused" | "realtime";
export type EngineKind = "wasm" | "mock";
/** aborted: the player pressed Stop. abandoned: the page closed mid-game. error: the game loop
 * crashed. None counts as an attempt in p(doom), but all are stored so they can be monitored. */
export type Outcome = "cleared" | "died" | "timeout" | "aborted" | "abandoned" | "error";
export type EngineStatus = "running" | "cleared" | "died";

export interface PlayerState {
  health: number;
  armor: number;
  weapon: string;
  ammo: Record<string, number>;
  x: number;
  y: number;
  angle: number;
}

export interface Enemy {
  type: string;
  distance: number;
  bearing: number;
  line_of_sight: boolean;
}

export interface Pickup {
  type: string;
  distance: number;
  bearing: number;
}

export interface Walls {
  ahead: number;
  ahead_right: number;
  right: number;
  behind_right: number;
  behind: number;
  behind_left: number;
  left: number;
  ahead_left: number;
}

export interface Interactable {
  kind: string;
  distance: number;
  bearing: number;
  facing: boolean;
  open: boolean;
  locked: boolean;
}

export interface Route {
  bearing: number;
  distance: number;
  via: string;
  hops: number;
}

export interface GameState {
  tic: number;
  level: string;
  seed: number;
  player: PlayerState;
  enemies: Enemy[];
  pickups: Pickup[];
  exit_bearing: number | null;
  exit_distance?: number | null;
  /** Scenery. Only the Doom engine sends these. */
  walls?: Walls | null;
  keys?: string[];
  interactables?: Interactable[];
  use_target?: { kind: string; open: boolean; locked: boolean } | null;
  route?: Route | null;
}

/** Keys the reflex layer and human input work in. Mapped to Doom's default bindings. */
export type GameKey =
  | "forward"
  | "back"
  | "left"
  | "right"
  | "strafe_left"
  | "strafe_right"
  | "fire"
  | "use";

export interface Decision {
  intent: Intent | null;
  confidence: number | null;
  threat_prob: number | null;
  danger_score: number | null;
  latency_ms: number;
  error: string | null;
}

export interface EventIn {
  tic: number;
  ts: string;
  health: number;
  armor: number;
  weapon: string;
  ammo: number;
  x: number;
  y: number;
  angle: number;
  action: string | null;
}

export interface PlayerAvailability {
  player_type: PlayerType;
  available: boolean;
  reason: string | null;
}
