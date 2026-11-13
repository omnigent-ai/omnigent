// A managed create's host id is sent only when `/v1/info` reports the server accepts it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchMock = vi.fn();

async function load() {
  vi.resetModules();
  return import("./managedSandboxHost");
}

async function serverInfo(supported: boolean) {
  const { FALLBACK_SERVER_INFO } = await import("./capabilities");
  return { ...FALLBACK_SERVER_INFO, request_managed_sandbox_host_id: supported };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("managedSandboxHostId", () => {
  it("sends an id only when the server reports support", async () => {
    const { managedSandboxHostId } = await load();

    expect(await managedSandboxHostId(await serverInfo(false))).toBeUndefined();
    expect(await managedSandboxHostId(await serverInfo(true))).toMatch(/^[0-9a-f]{32}$/);
  });

  it("waits for the capability probe while it is loading", async () => {
    const { managedSandboxHostId } = await load();
    fetchMock.mockResolvedValueOnce(
      Response.json({ request_managed_sandbox_host_id: true }, { status: 200 }),
    );

    const id = await managedSandboxHostId("loading");

    expect(id).toMatch(/^[0-9a-f]{32}$/);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("sends no id when the probe fails", async () => {
    const { managedSandboxHostId } = await load();
    fetchMock.mockRejectedValueOnce(new Error("offline"));

    expect(await managedSandboxHostId("loading")).toBeUndefined();
  });

  it("sends no id when the probe is still pending after the wait", async () => {
    vi.useFakeTimers();
    const { managedSandboxHostId } = await load();
    fetchMock.mockReturnValueOnce(new Promise(() => {}));

    const pending = managedSandboxHostId("loading");
    await vi.advanceTimersByTimeAsync(3_000);

    expect(await pending).toBeUndefined();
  });
});
