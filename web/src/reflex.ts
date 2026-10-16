import type { Engine } from "./engine/Engine";
import type { GameKey, Intent } from "./types";

/** Which keys each intent holds down. One intent at a time, as the decision layer returns. */
export const INTENT_KEYS: Record<Intent, GameKey[]> = {
  advance: ["forward"],
  retreat: ["back"],
  turn_left: ["left"],
  turn_right: ["right"],
  strafe_left: ["strafe_left"],
  strafe_right: ["strafe_right"],
  fire: ["fire"],
  use: ["use"],
};

/**
 * Holds the current intent as key presses. The decision layer changes the intent about once a
 * second. apply() runs every tick and only touches keys whose state changed.
 */
export class ReflexLayer {
  private intent: Intent | null = null;
  private until: number | null = null;
  private down = new Set<GameKey>();

  constructor(private engine: Pick<Engine, "setKey">) {}

  /** `untilTic` lets go of the keys at that game tic. Without it the intent is held until the next one. */
  setIntent(intent: Intent | null, untilTic?: number | null): void {
    this.intent = intent;
    this.until = intent && untilTic != null ? untilTic : null;
  }

  /**
   * Lets go of a movement key and keeps fire. Used in realtime mode while a slow player is still
   * thinking: the game goes on, and a turn or walk held on an old answer carries the player away
   * from the spot the new answer is about.
   */
  stopMoving(): void {
    if (this.intent && this.intent !== "fire" && this.intent !== "use") {
      this.intent = null;
      this.until = null;
    }
  }

  current(): Intent | null {
    return this.intent;
  }

  apply(tic?: number): void {
    if (tic !== undefined && this.until !== null && tic >= this.until) {
      this.intent = null;
      this.until = null;
    }
    const want = new Set<GameKey>(this.intent ? INTENT_KEYS[this.intent] : []);
    for (const key of this.down) if (!want.has(key)) this.engine.setKey(key, false);
    for (const key of want) if (!this.down.has(key)) this.engine.setKey(key, true);
    this.down = want;
  }

  releaseAll(): void {
    this.intent = null;
    this.until = null;
    this.apply();
  }
}
