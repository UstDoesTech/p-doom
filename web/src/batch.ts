import type { PlayerType } from "./types";

export interface Job {
  playerType: PlayerType;
  seed: number;
}

/** Seed by seed, every player on the same seed before the next seed starts. If the run is
 * interrupted, the seeds that finished were played by everyone, so any-clear stays comparable. */
export function planSet(players: PlayerType[], fromSeed: number, toSeed: number): Job[] {
  const jobs: Job[] = [];
  for (let seed = fromSeed; seed <= toSeed; seed++) {
    for (const playerType of players) jobs.push({ playerType, seed });
  }
  return jobs;
}

export interface SetProgress {
  done: number;
  total: number;
  job: Job;
}

/** Run the jobs one after another. Stops at the first failure or when stop() says so, and returns
 * how many finished. */
export async function runSet(
  jobs: Job[],
  runOne: (job: Job) => Promise<{ outcome: string }>,
  hooks: { stop(): boolean; onProgress?(p: SetProgress): void },
): Promise<{ finished: number; stoppedBy: "done" | "stop" | "outcome" }> {
  let finished = 0;
  for (const job of jobs) {
    if (hooks.stop()) return { finished, stoppedBy: "stop" };
    hooks.onProgress?.({ done: finished, total: jobs.length, job });
    const r = await runOne(job);
    if (r.outcome === "aborted" || r.outcome === "abandoned" || r.outcome === "error") {
      return { finished, stoppedBy: "outcome" };
    }
    finished += 1;
  }
  return { finished, stoppedBy: "done" };
}
