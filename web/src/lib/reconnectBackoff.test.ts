import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  awaitReconnectDelay,
  HIDDEN_RECONNECT_JITTER_MS,
  HIDDEN_RECONNECT_MS,
  nextReconnectDelay,
  RECONNECT_BASE_MS,
  RECONNECT_MAX_MS,
} from "./reconnectBackoff";

function setPageHidden(hidden: boolean): void {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
}

function fireVisibilityChange(): void {
  document.dispatchEvent(new Event("visibilitychange"));
}

afterEach(() => {
  Reflect.deleteProperty(document, "hidden");
  vi.restoreAllMocks();
});

describe("nextReconnectDelay", () => {
  it("ramps visible retries from the base to the cap with jitter", () => {
    vi.spyOn(Math, "random").mockReturnValue(0);
    expect(nextReconnectDelay(1, false)).toBe(RECONNECT_BASE_MS / 2);
    expect(nextReconnectDelay(2, false)).toBe(RECONNECT_BASE_MS);
    expect(nextReconnectDelay(12, false)).toBe(RECONNECT_MAX_MS / 2);
    vi.spyOn(Math, "random").mockReturnValue(0.999);
    expect(nextReconnectDelay(12, false)).toBeLessThan(RECONNECT_MAX_MS);
  });

  it("stretches every hidden retry, including the first, to the hidden cadence", () => {
    const floor = HIDDEN_RECONNECT_MS - HIDDEN_RECONNECT_JITTER_MS;
    for (const random of [0, 0.5, 0.999]) {
      vi.spyOn(Math, "random").mockReturnValue(random);
      for (const attempt of [1, 2, 12]) {
        const delay = nextReconnectDelay(attempt, true);
        expect(delay).toBeGreaterThan(floor);
        expect(delay).toBeLessThanOrEqual(HIDDEN_RECONNECT_MS);
      }
    }
  });

  it("reads the live document visibility by default", () => {
    setPageHidden(true);
    expect(nextReconnectDelay(1)).toBeGreaterThan(RECONNECT_MAX_MS);
    setPageHidden(false);
    expect(nextReconnectDelay(1)).toBeLessThanOrEqual(RECONNECT_BASE_MS);
  });
});

describe("awaitReconnectDelay", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  async function settled(promise: Promise<void>): Promise<boolean> {
    let done = false;
    void promise.then(() => {
      done = true;
    });
    await vi.advanceTimersByTimeAsync(0);
    return done;
  }

  it("ends a hidden wait within a jittered first-retry delay once the page is visible", async () => {
    setPageHidden(true);
    const first = awaitReconnectDelay(1, new AbortController().signal);
    const second = awaitReconnectDelay(1, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(RECONNECT_MAX_MS);
    expect(await settled(first)).toBe(false);

    // A visibility event that leaves the page hidden changes nothing.
    fireVisibilityChange();
    expect(await settled(first)).toBe(false);

    // Waits woken by the same event spread over the visible first-retry window
    // instead of all ending in that tick.
    setPageHidden(false);
    vi.spyOn(Math, "random").mockReturnValueOnce(0).mockReturnValueOnce(0.999);
    fireVisibilityChange();
    expect(await settled(first)).toBe(false);
    await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS / 2);
    expect(await settled(first)).toBe(true);
    expect(await settled(second)).toBe(false);
    await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS / 2);
    expect(await settled(second)).toBe(true);
  });

  it("lets a hidden wait run out on its own timer when the page stays hidden", async () => {
    setPageHidden(true);
    const wait = awaitReconnectDelay(1, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(HIDDEN_RECONNECT_MS - HIDDEN_RECONNECT_JITTER_MS - 1);
    expect(await settled(wait)).toBe(false);
    await vi.advanceTimersByTimeAsync(HIDDEN_RECONNECT_JITTER_MS + 1);
    expect(await settled(wait)).toBe(true);
  });

  it("ignores visibility events for a wait scheduled while visible", async () => {
    setPageHidden(false);
    const wait = awaitReconnectDelay(12, new AbortController().signal);
    fireVisibilityChange();
    expect(await settled(wait)).toBe(false);
    await vi.advanceTimersByTimeAsync(RECONNECT_MAX_MS);
    expect(await settled(wait)).toBe(true);
  });

  it("ends immediately on abort, even one that fired before the wait began", async () => {
    setPageHidden(true);
    const live = new AbortController();
    const wait = awaitReconnectDelay(1, live.signal);
    live.abort();
    expect(await settled(wait)).toBe(true);

    const aborted = new AbortController();
    aborted.abort();
    expect(await settled(awaitReconnectDelay(1, aborted.signal))).toBe(true);
  });
});
