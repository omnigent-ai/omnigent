import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiTimeoutError, DEFAULT_API_TIMEOUT_MS, fetchWithTimeout } from "./fetchTimeout";

describe("fetchWithTimeout", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("resolves with the response when the run function settles quickly", async () => {
    const expected = new Response(null, { status: 200 });
    const result = await fetchWithTimeout(() => Promise.resolve(expected));
    expect(result).toBe(expected);
  });

  it("rejects with ApiTimeoutError when run never settles past the deadline", async () => {
    const promise = fetchWithTimeout(() => new Promise<Response>(() => {}));
    // Attach the rejection handler before advancing timers so Node never sees
    // an unhandled rejection between the timer firing and the assertion.
    const assertion = expect(promise).rejects.toBeInstanceOf(ApiTimeoutError);
    await vi.advanceTimersByTimeAsync(DEFAULT_API_TIMEOUT_MS + 1);
    await assertion;
  });

  it("rejects before the default deadline when a custom timeoutMs is given", async () => {
    const CUSTOM_MS = 5_000;
    const promise = fetchWithTimeout(() => new Promise<Response>(() => {}), CUSTOM_MS);
    // Has not rejected before the custom deadline.
    await vi.advanceTimersByTimeAsync(CUSTOM_MS - 1);
    let settled = false;
    promise.catch(() => {
      settled = true;
    });
    // Give microtasks a chance to run.
    await Promise.resolve();
    expect(settled).toBe(false);
    // Fires at the custom deadline.
    await vi.advanceTimersByTimeAsync(2);
    await expect(promise).rejects.toBeInstanceOf(ApiTimeoutError);
  });

  it("clears the timer when run settles before the deadline", async () => {
    const clearSpy = vi.spyOn(globalThis, "clearTimeout");
    await fetchWithTimeout(() => Promise.resolve(new Response(null, { status: 200 })));
    expect(clearSpy).toHaveBeenCalled();
    // Advancing past the deadline must not throw.
    await vi.advanceTimersByTimeAsync(DEFAULT_API_TIMEOUT_MS + 1);
  });

  it("aborts the internal controller and settles when the external signal fires before the timer", async () => {
    const external = new AbortController();
    let capturedSignal: AbortSignal | undefined;
    const promise = fetchWithTimeout(
      (signal) => {
        capturedSignal = signal;
        // Simulate a transport that honours the abort signal, as `fetch` does.
        return new Promise<Response>((_, reject) => {
          signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        });
      },
      DEFAULT_API_TIMEOUT_MS,
      external.signal,
    );
    // Fire the external cancellation well before the 30 s timeout.
    external.abort();
    await expect(promise).rejects.toThrow("Aborted");
    expect(capturedSignal?.aborted).toBe(true);
  });

  it("aborts the controller when the deadline fires", async () => {
    let capturedSignal: AbortSignal | undefined;
    const promise = fetchWithTimeout((signal) => {
      capturedSignal = signal;
      return new Promise<Response>(() => {});
    });
    // Attach the handler before advancing timers to avoid an unhandled rejection window.
    const assertion = expect(promise).rejects.toBeInstanceOf(ApiTimeoutError);
    await vi.advanceTimersByTimeAsync(DEFAULT_API_TIMEOUT_MS + 1);
    await assertion;
    expect(capturedSignal?.aborted).toBe(true);
  });
});
