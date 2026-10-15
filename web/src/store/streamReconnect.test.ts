import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  HIDDEN_RECONNECT_MAX_MS,
  RECONNECT_MAX_MS,
  awaitReconnectDelay,
  nextReconnectDelay,
} from "./streamReconnect";

/** Shadow `document.hidden` (jsdom defines it on the prototype). */
function setDocumentHidden(value: boolean): void {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => value });
}

describe("streamReconnect pacing", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // Pin jitter to its ceiling so each delay equals its base exactly.
    vi.spyOn(Math, "random").mockReturnValue(1);
  });

  afterEach(() => {
    delete (document as { hidden?: boolean }).hidden;
    vi.clearAllTimers();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("caps the saturated delay at the hidden ceiling only while hidden", () => {
    setDocumentHidden(true);
    expect(nextReconnectDelay(20)).toBe(HIDDEN_RECONNECT_MAX_MS);
    setDocumentHidden(false);
    expect(nextReconnectDelay(20)).toBe(RECONNECT_MAX_MS);
  });

  it("waits the stretched hidden cadence from the very first failed open", () => {
    // A page hidden EARLY in the backoff ramp must not keep dialing at the
    // fast foreground cadence until the doubling reaches the hidden cap —
    // that mid-ramp window still wakes the radio every few seconds.
    setDocumentHidden(true);
    expect(nextReconnectDelay(1)).toBe(HIDDEN_RECONNECT_MAX_MS);
    setDocumentHidden(false);
    expect(nextReconnectDelay(1)).toBe(250);
  });

  it("waits out a stretched hidden delay to its exact deadline", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(HIDDEN_RECONNECT_MAX_MS, controller.signal).then(() => {
      resolved = true;
    });

    await vi.advanceTimersByTimeAsync(HIDDEN_RECONNECT_MAX_MS - 1);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(resolved).toBe(true);
  });

  it("wakes immediately on becoming visible when no jitter is requested", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(HIDDEN_RECONNECT_MAX_MS, controller.signal).then(() => {
      resolved = true;
    });

    await vi.advanceTimersByTimeAsync(5_000);
    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(resolved).toBe(true);
  });

  it("staggers a background conversation's visible wake by the provided jitter", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(HIDDEN_RECONNECT_MAX_MS, controller.signal, () => 2_000).then(() => {
      resolved = true;
    });

    await vi.advanceTimersByTimeAsync(1_000);
    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));

    await vi.advanceTimersByTimeAsync(1_999);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(resolved).toBe(true);
  });

  it("resumes the stretched wait when the page re-hides within the jitter window", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(HIDDEN_RECONNECT_MAX_MS, controller.signal, () => 2_000).then(() => {
      resolved = true;
    });

    // Brief foreground visit at t=10s arms the jittered wake for t=12s...
    await vi.advanceTimersByTimeAsync(10_000);
    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));

    // ...but the page re-hides at t=11s, before the wake fires.
    await vi.advanceTimersByTimeAsync(1_000);
    setDocumentHidden(true);
    document.dispatchEvent(new Event("visibilitychange"));

    // Jitter expiry while hidden must NOT reconnect; the remaining stretched
    // wait resumes and resolves only at the original 60 s deadline.
    await vi.advanceTimersByTimeAsync(1_000);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(HIDDEN_RECONNECT_MAX_MS - 12_000 - 1);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(resolved).toBe(true);
  });

  it("wakes a short (≤ visible-cap) hidden delay when the page becomes visible", async () => {
    // A hidden-sampled delay can come out ≤ 5 s; it must still take the
    // visibility-aware path, or a tab foregrounded mid-wait sits out the
    // remainder disconnected.
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(3_000, controller.signal).then(() => {
      resolved = true;
    });

    await vi.advanceTimersByTimeAsync(500);
    expect(resolved).toBe(false);
    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(resolved).toBe(true);
  });

  it("still runs a short hidden delay to its deadline when the page stays hidden", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(3_000, controller.signal).then(() => {
      resolved = true;
    });

    await vi.advanceTimersByTimeAsync(2_999);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(resolved).toBe(true);
  });

  it("resolves immediately when the signal aborts mid-wait", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(HIDDEN_RECONNECT_MAX_MS, controller.signal).then(() => {
      resolved = true;
    });

    await vi.advanceTimersByTimeAsync(1_000);
    controller.abort();
    await vi.advanceTimersByTimeAsync(0);
    expect(resolved).toBe(true);
  });

  it("resolves immediately for an already-aborted signal", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    controller.abort();
    let resolved = false;
    void awaitReconnectDelay(HIDDEN_RECONNECT_MAX_MS, controller.signal).then(() => {
      resolved = true;
    });

    await vi.advanceTimersByTimeAsync(0);
    expect(resolved).toBe(true);
  });
});
