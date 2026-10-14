import type { Api } from "./api";
import type { EventIn } from "./types";

const BATCH = 500;

/** Batches events and posts them on a timer. A failed post puts the batch back for the next try. */
export class TelemetryBuffer {
  private queue: EventIn[] = [];
  private timer?: ReturnType<typeof setInterval>;
  private busy = false;

  constructor(
    private api: Pick<Api, "sendEvents">,
    private sessionId: string,
    private flushMs = 2000,
    private cap = 5000,
  ) {}

  push(event: EventIn): void {
    this.queue.push(event);
    if (this.queue.length > this.cap) this.queue.splice(0, this.queue.length - this.cap);
  }

  start(): void {
    this.timer = setInterval(() => void this.flush(), this.flushMs);
  }

  /** Send one batch. Returns false when the post failed or another flush is running. */
  async flush(): Promise<boolean> {
    if (this.busy) return false;
    this.busy = true;
    const batch = this.queue.splice(0, BATCH);
    try {
      await this.api.sendEvents(this.sessionId, batch); // an empty batch still flushes decisions
      return true;
    } catch {
      this.queue.unshift(...batch);
      return false;
    } finally {
      this.busy = false;
    }
  }

  /** Stop the timer and drain the queue, giving up after a few failures in a row. */
  async stop(): Promise<void> {
    if (this.timer) clearInterval(this.timer);
    let failures = 0;
    do {
      if (await this.flush()) failures = 0;
      else if (++failures >= 3) return;
    } while (this.queue.length > 0);
  }

  /** Drop everything without sending. For aborted sessions. */
  discard(): void {
    if (this.timer) clearInterval(this.timer);
    this.queue = [];
  }

  get pending(): number {
    return this.queue.length;
  }
}
