import { describe, expect, it } from "vitest";
import { INTENT_KEYS, ReflexLayer } from "./reflex";
import { INTENTS, type GameKey } from "./types";

function fake() {
  const calls: Array<[GameKey, boolean]> = [];
  return { calls, setKey: (k: GameKey, d: boolean) => void calls.push([k, d]) };
}

describe("ReflexLayer", () => {
  it("presses keys for an intent and does not repeat them", () => {
    const engine = fake();
    const reflex = new ReflexLayer(engine);
    reflex.setIntent("advance");
    reflex.apply();
    reflex.apply();
    expect(engine.calls).toEqual([["forward", true]]);
  });

  it("releases the old keys when the intent changes", () => {
    const engine = fake();
    const reflex = new ReflexLayer(engine);
    reflex.setIntent("advance");
    reflex.apply();
    reflex.setIntent("fire");
    reflex.apply();
    expect(engine.calls).toEqual([
      ["forward", true],
      ["forward", false],
      ["fire", true],
    ]);
  });

  it("releaseAll lets go of everything", () => {
    const engine = fake();
    const reflex = new ReflexLayer(engine);
    reflex.setIntent("turn_left");
    reflex.apply();
    reflex.releaseAll();
    expect(engine.calls.at(-1)).toEqual(["left", false]);
    expect(reflex.current()).toBeNull();
  });

  it("maps every intent to at least one key", () => {
    for (const intent of INTENTS) expect(INTENT_KEYS[intent].length).toBeGreaterThan(0);
  });
});
