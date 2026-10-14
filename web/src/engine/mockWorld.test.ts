import { describe, expect, it } from "vitest";
import type { GameKey } from "../types";
import { MockWorld } from "./mockWorld";

const keys = (...k: GameKey[]) => new Set<GameKey>(k);

function run(seed: number, held: Set<GameKey>, tics: number) {
  const w = new MockWorld(seed);
  for (let i = 0; i < tics; i++) w.step(held);
  return w;
}

describe("MockWorld", () => {
  it("is deterministic for a seed", () => {
    const a = run(7, keys("forward", "fire"), 200).state();
    const b = run(7, keys("forward", "fire"), 200).state();
    expect(a).toEqual(b);
  });

  it("differs between seeds", () => {
    expect(run(1, keys(), 0).state()).not.toEqual(run(2, keys(), 0).state());
  });

  it("uses positive bearings for targets on the right", () => {
    const w = new MockWorld(1);
    w.angle = 90; // facing +y
    expect(w.bearingTo(100, 0)).toBeCloseTo(90); // east is to the right
    expect(w.bearingTo(-100, 0)).toBeCloseTo(-90);
    expect(w.bearingTo(0, 100)).toBeCloseTo(0);
  });

  it("turning right makes a target on the right move toward the centre", () => {
    const w = new MockWorld(1);
    w.angle = 90;
    const before = w.bearingTo(100, 0);
    for (let i = 0; i < 10; i++) w.step(keys("right"));
    expect(Math.abs(w.bearingTo(100, 0))).toBeLessThan(Math.abs(before));
  });

  it("kills an enemy that is held in the fire cone", () => {
    const w = new MockWorld(3);
    w.enemies = [{ x: 0, y: 400, hp: 20, cooldown: 99 }];
    w.angle = 90;
    for (let i = 0; i < 400 && w.kills === 0; i++) w.step(keys("fire"));
    expect(w.kills).toBe(1);
  });

  it("clears the level on reaching the exit and dies at zero health", () => {
    const w = new MockWorld(1);
    w.enemies = [];
    w.x = w.exit.x;
    w.y = w.exit.y - 10;
    expect(w.status()).toBe("cleared");

    const d = new MockWorld(1);
    d.health = 0;
    expect(d.status()).toBe("died");
  });

  it("stops advancing once the attempt is over", () => {
    const w = new MockWorld(1);
    w.health = 0;
    w.step(keys("forward"));
    expect(w.tic).toBe(0);
  });
});
