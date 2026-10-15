import { describe, expect, it } from "vitest";
import { planSet, runSet } from "./batch";

describe("run set", () => {
  it("plays every player on a seed before moving to the next seed", () => {
    const jobs = planSet(["rule_bot", "rl_policy"], 5, 6);
    expect(jobs.map((j) => `${j.seed}:${j.playerType}`)).toEqual([
      "5:rule_bot",
      "5:rl_policy",
      "6:rule_bot",
      "6:rl_policy",
    ]);
  });

  it("stops when asked and when a game did not finish normally", async () => {
    const jobs = planSet(["rule_bot"], 1, 5);
    let n = 0;
    const out = await runSet(jobs, async () => ({ outcome: ++n === 3 ? "aborted" : "died" }), {
      stop: () => false,
    });
    expect(out).toEqual({ finished: 2, stoppedBy: "outcome" });

    let ran = 0;
    const stopped = await runSet(jobs, async () => (ran++, { outcome: "cleared" }), {
      stop: () => ran >= 2,
    });
    expect(stopped).toEqual({ finished: 2, stoppedBy: "stop" });

    const all = await runSet(jobs, async () => ({ outcome: "timeout" }), { stop: () => false });
    expect(all).toEqual({ finished: 5, stoppedBy: "done" });
  });
});
