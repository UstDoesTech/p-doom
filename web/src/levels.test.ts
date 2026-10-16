import { describe, expect, it } from "vitest";
import { nextLevel } from "./levels";

describe("nextLevel", () => {
  it("goes M1 to M8 in order", () => {
    expect(nextLevel("E1M1")).toBe("E1M2");
    expect(nextLevel("E2M7")).toBe("E2M8");
  });
  it("goes to the next episode after M8, and stops after episode 4", () => {
    expect(nextLevel("E1M8")).toBe("E2M1");
    expect(nextLevel("E3M8")).toBe("E4M1");
    expect(nextLevel("E4M8")).toBeNull();
  });
  it("returns from the secret level into the episode", () => {
    expect(nextLevel("E1M9")).toBe("E1M4");
    expect(nextLevel("E2M9")).toBe("E2M6");
    expect(nextLevel("E4M9")).toBe("E4M3");
  });
  it("gives up on a level name it does not know", () => {
    expect(nextLevel("arena")).toBeNull();
  });
});
