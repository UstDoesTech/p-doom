import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiTimeout, DECIDE_TIMEOUT_MS, api } from "./api";
import type { GameState } from "./types";

const state = {} as GameState;

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("api", () => {
  it("gives up on a decision the server never answers", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(new DOMException("x", "AbortError")));
        }),
    );
    const pending = api.decide("s1", state, null);
    const caught = pending.catch((e) => e);
    await vi.advanceTimersByTimeAsync(DECIDE_TIMEOUT_MS + 1);
    expect(await caught).toBeInstanceOf(ApiTimeout);
  });

  it("passes a normal answer through", async () => {
    vi.stubGlobal("fetch", async () => new Response(JSON.stringify({ intent: "fire" })));
    expect(await api.decide("s1", state, null)).toEqual({ intent: "fire" });
  });
});
