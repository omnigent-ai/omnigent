import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isChunkLoadError, reloadAfterChunkError } from "./chunkLoadRecovery";

const reload = vi.fn();

beforeEach(() => {
  sessionStorage.clear();
  reload.mockReset();
  vi.stubGlobal("location", { reload });
  vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
  vi.spyOn(Date, "now").mockReturnValue(1_000_000);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("isChunkLoadError", () => {
  it.each([
    new TypeError("Failed to fetch dynamically imported module: https://example.com/assets/old.js"),
    new TypeError("Importing a module script failed."),
    new TypeError("error loading dynamically imported module: https://example.com/assets/old.js"),
    new Error("Unable to preload CSS for /assets/old.css"),
    Object.assign(new Error("Loading chunk 123 failed."), { name: "ChunkLoadError" }),
    Object.assign(new Error("Loading CSS chunk 123 failed."), { code: "CSS_CHUNK_LOAD_FAILED" }),
    { message: "Failed to fetch dynamically imported module: /assets/old.js" },
  ])("recognizes a chunk load failure: %s", (error) => {
    expect(isChunkLoadError(error)).toBe(true);
  });

  it.each([
    new Error("render failed"),
    new TypeError("Cannot read properties of undefined (reading 'default')"),
    new TypeError("Failed to fetch"),
    new SyntaxError("Unexpected token"),
    null,
    undefined,
    "Failed to fetch",
    {},
    { message: 42 },
  ])("does not classify other failures as deployment errors: %s", (error) => {
    expect(isChunkLoadError(error)).toBe(false);
  });
});

describe("reloadAfterChunkError", () => {
  it("records the guard before refreshing the current URL", () => {
    reload.mockImplementation(() => {
      expect(sessionStorage.getItem("omnigent:chunk-reload-at")).toBe("1000000");
    });

    expect(reloadAfterChunkError()).toBe(true);
    expect(reload).toHaveBeenCalledExactlyOnceWith();
  });

  it("does not reload twice for concurrent failures", () => {
    expect(reloadAfterChunkError()).toBe(true);
    expect(reloadAfterChunkError()).toBe(false);
    expect(reload).toHaveBeenCalledOnce();
  });

  it("retains the cooldown after a new document loads", async () => {
    reloadAfterChunkError();
    vi.resetModules();
    const freshPage = await import("./chunkLoadRecovery");

    expect(freshPage.reloadAfterChunkError()).toBe(false);
    expect(reload).toHaveBeenCalledOnce();
  });

  it("allows recovery from a later deployment after the cooldown", () => {
    reloadAfterChunkError();
    vi.mocked(Date.now).mockReturnValue(1_059_999);
    expect(reloadAfterChunkError()).toBe(false);
    vi.mocked(Date.now).mockReturnValue(1_060_000);
    expect(reloadAfterChunkError()).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("does not spend the automatic retry while offline", () => {
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    expect(reloadAfterChunkError()).toBe(false);
    expect(reload).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("omnigent:chunk-reload-at")).toBeNull();
  });

  it.each(["getItem", "setItem"] as const)("does not reload if storage.%s throws", (method) => {
    vi.spyOn(Storage.prototype, method).mockImplementation(() => {
      throw new Error("Storage unavailable");
    });
    expect(reloadAfterChunkError()).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });

  it("does not reload if sessionStorage itself is blocked", () => {
    vi.spyOn(globalThis, "sessionStorage", "get").mockImplementation(() => {
      throw new DOMException("Storage blocked", "SecurityError");
    });
    expect(reloadAfterChunkError()).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });
});
