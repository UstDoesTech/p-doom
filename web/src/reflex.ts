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
  private down = new Set<GameKey>();

  constructor(private engine: Pick<Engine, "setKey">) {}

  setIntent(intent: Intent | null): void {
    this.intent = intent;
  }

  current(): Intent | null {
    return this.intent;
  }

  apply(): void {
    const want = new Set<GameKey>(this.intent ? INTENT_KEYS[this.intent] : []);
    for (const key of this.down) if (!want.has(key)) this.engine.setKey(key, false);
    for (const key of want) if (!this.down.has(key)) this.engine.setKey(key, true);
    this.down = want;
  }

  releaseAll(): void {
    this.intent = null;
    this.apply();
  }
}
