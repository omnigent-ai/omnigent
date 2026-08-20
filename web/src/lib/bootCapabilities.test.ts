import { afterEach, describe, expect, it, vi } from "vitest";
import type { ServerInfo } from "./capabilities";
import { createBootServerInfo, SERVER_INFO_OFFLINE_FALLBACK } from "./bootCapabilities";

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
