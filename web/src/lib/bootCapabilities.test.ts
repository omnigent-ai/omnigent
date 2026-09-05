import { afterEach, describe, expect, it, vi } from "vitest";
import type { ServerInfo } from "./capabilities";
import {
  createBootServerInfo,
  SERVER_INFO_OFFLINE_FALLBACK,
  withBootTimeout,
} from "./bootCapabilities";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("createBootServerInfo", () => {
  it("paints the timeout fallback then exposes the eventual real branding", async () => {
    vi.useFakeTimers();
    const probe = deferred<ServerInfo>();
    const boot = createBootServerInfo(probe.promise, 1500);
    const initialResult = vi.fn();
    void boot.initial.then(initialResult);

    await vi.advanceTimersByTimeAsync(1500);
    expect(initialResult).toHaveBeenCalledWith(SERVER_INFO_OFFLINE_FALLBACK);

    const branded = {
      ...SERVER_INFO_OFFLINE_FALLBACK,
      branding: {
        app_name: "Acme Agent",
        heading: "Build with Acme",
        logos: { main: "/logo", loading: null, favicon: null },
        powered_by: true,
      },
    } satisfies ServerInfo;
    probe.resolve(branded);

    await expect(boot.settled).resolves.toEqual(branded);
  });
});

// Shared by the /v1/info and /v1/me boot gates in main.tsx: both must be
// awaited before first render, and neither may deadlock first paint.
describe("withBootTimeout", () => {
  it("resolves with the probe when it beats the timeout", async () => {
    await expect(withBootTimeout(Promise.resolve("alice"), null, 1500)).resolves.toBe("alice");
  });

  it("falls back when the probe hangs past the timeout", async () => {
    vi.useFakeTimers();
    const hung = new Promise<string | null>(() => {});
    const result = vi.fn();
    void withBootTimeout(hung, null, 1500).then(result);

    await vi.advanceTimersByTimeAsync(1500);

    expect(result).toHaveBeenCalledWith(null);
  });

  it("clears the timer once the probe settles", async () => {
    vi.useFakeTimers();
    const probe = deferred<string | null>();
    const settled = withBootTimeout(probe.promise, null, 1500);
    probe.resolve("alice");
    await expect(settled).resolves.toBe("alice");

    // A live timer here would keep the fallback armed (and hold the event
    // loop open) long after boot resolved.
    expect(vi.getTimerCount()).toBe(0);
  });
});
