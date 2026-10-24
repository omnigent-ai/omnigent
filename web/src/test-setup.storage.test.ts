import { afterEach, describe, expect, it, vi } from "vitest";

// Node 25+ predefines localStorage (as undefined) and sessionStorage on
// globalThis, which would otherwise shadow jsdom's storages in every test.
describe("test environment storage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    sessionStorage.clear();
  });

  it.each(["localStorage", "sessionStorage"] as const)("exposes jsdom's %s", (name) => {
    const storage = globalThis[name];
    expect(storage).toBeInstanceOf(Storage);
    expect(Object.getPrototypeOf(storage)).toBe(Storage.prototype);
    storage.setItem("probe", "value");
    expect(storage.getItem("probe")).toBe("value");
  });

  it("lets Storage.prototype spies intercept both storages", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {});
    localStorage.setItem("local", "1");
    sessionStorage.setItem("session", "2");
    expect(setItem).toHaveBeenCalledTimes(2);
    setItem.mockRestore();
    expect(localStorage.getItem("local")).toBeNull();
    expect(sessionStorage.getItem("session")).toBeNull();
  });

  it("accepts both storages as a StorageEvent storageArea", () => {
    for (const storageArea of [localStorage, sessionStorage]) {
      const event = new StorageEvent("storage", { key: "probe", storageArea });
      expect(event.storageArea).toBe(storageArea);
    }
  });

  it.each(["localStorage", "sessionStorage"] as const)("keeps %s a spy-able accessor", (name) => {
    vi.spyOn(globalThis, name, "get").mockImplementation(() => {
      throw new DOMException("Storage blocked", "SecurityError");
    });
    expect(() => globalThis[name]).toThrow("Storage blocked");
    vi.restoreAllMocks();
    expect(globalThis[name]).toBeInstanceOf(Storage);
  });
});
