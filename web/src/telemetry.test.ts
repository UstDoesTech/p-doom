import { describe, expect, it } from "vitest";
import { TelemetryBuffer } from "./telemetry";
import type { EventIn } from "./types";

const ev = (tic: number) => ({ tic }) as unknown as EventIn;

describe("TelemetryBuffer", () => {
  it("drains everything even when a timer flush is still running at stop", async () => {
    const sent: number[] = [];
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    let first = true;
    const api = {
      sendEvents: async (_id: string, batch: EventIn[]) => {
        if (first) {
          first = false;
          await gate; // a slow write, as with two sinks
        }
        sent.push(...batch.map((e) => (e as unknown as { tic: number }).tic));
      },
    };
    const buf = new TelemetryBuffer(api, "s", 1_000_000);
    buf.push(ev(1));
    const slow = buf.flush(); // plays the part of the timer's flush
    buf.push(ev(2));
    const stopping = buf.stop();
    release();
    await slow;
    await stopping;
    expect(sent.sort()).toEqual([1, 2]);
    expect(buf.pending).toBe(0);
  });
});
