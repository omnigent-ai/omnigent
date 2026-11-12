import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const STORAGE_KEY = "omnigent:routing-key";

async function freshKey(): Promise<string> {
  vi.resetModules();
  const { clientRoutingKey } = await import("./routingKey");
  return clientRoutingKey();
}

describe("clientRoutingKey", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("keeps one key for the client across reloads", async () => {
    const first = await freshKey();
    expect(first).toMatch(/^[0-9a-f]{32}$/);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(first);
    expect(await freshKey()).toBe(first);
  });

  it("replaces a stored value that is not a key", async () => {
    window.localStorage.setItem(STORAGE_KEY, "not-a-key");
    const key = await freshKey();
    expect(key).toMatch(/^[0-9a-f]{32}$/);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(key);
  });

  it("holds a key for the page when storage is unavailable", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.resetModules();
    const { clientRoutingKey } = await import("./routingKey");
    const key = clientRoutingKey();
    expect(key).toMatch(/^[0-9a-f]{32}$/);
    expect(clientRoutingKey()).toBe(key);
  });
});
