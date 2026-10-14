import type { Engine } from "./engine/Engine";
import type { GameKey } from "./types";

// Doom's default keys. The mock arena has no keyboard handling of its own, so forward = true
// relays them. The WASM build reads the keyboard itself, so there we only record them.
const BINDINGS: Record<string, GameKey> = {
  ArrowUp: "forward",
  ArrowDown: "back",
  ArrowLeft: "left",
  ArrowRight: "right",
  ",": "strafe_left",
  ".": "strafe_right",
  Control: "fire",
  " ": "use",
};

const ACTION_NAMES: Record<GameKey, string> = {
  forward: "advance",
  back: "retreat",
  left: "turn_left",
  right: "turn_right",
  strafe_left: "strafe_left",
  strafe_right: "strafe_right",
  fire: "fire",
  use: "use",
};

export class HumanInput {
  private held = new Set<GameKey>();
  private attached = false;

  constructor(
    private engine: Pick<Engine, "setKey">,
    private forward: boolean,
  ) {}

  private onKey = (e: KeyboardEvent, down: boolean) => {
    const target = e.target as HTMLElement | null;
    if (target && ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName)) return;
    const key = BINDINGS[e.key];
    if (!key) return;
    e.preventDefault(); // keep arrows and space from scrolling the page
    if (down) this.held.add(key);
    else this.held.delete(key);
    if (this.forward) this.engine.setKey(key, down);
  };
  private onDown = (e: KeyboardEvent) => this.onKey(e, true);
  private onUp = (e: KeyboardEvent) => this.onKey(e, false);

  attach(): void {
    this.attached = true;
    window.addEventListener("keydown", this.onDown);
    window.addEventListener("keyup", this.onUp);
  }

  detach(): void {
    if (this.attached) {
      window.removeEventListener("keydown", this.onDown);
      window.removeEventListener("keyup", this.onUp);
      this.attached = false;
    }
    if (this.forward) for (const key of this.held) this.engine.setKey(key, false);
    this.held.clear();
  }

  /** The keys held right now as one action string, such as "advance+fire", or null. */
  action(): string | null {
    if (this.held.size === 0) return null;
    return [...this.held].map((k) => ACTION_NAMES[k]).sort().join("+");
  }
}
