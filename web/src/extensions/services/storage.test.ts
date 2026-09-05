import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ExtensionStorageError,
  ExtensionStorageWriteLimiter,
  ExtensionUserStorage,
  resetExtensionStorageForTests,
} from "./storage";

beforeEach(() => resetExtensionStorageForTests());
afterEach(() => vi.useRealTimers());

describe("ExtensionStorageWriteLimiter", () => {
  it("paces writes and rejects work cancelled in the queue", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    const limiter = new ExtensionStorageWriteLimiter();
    await limiter.run(new AbortController().signal, async () => "first");
    const operation = vi.fn(async () => "second");
    const controller = new AbortController();
    const second = limiter.run(controller.signal, operation);
    await Promise.resolve();
    expect(operation).not.toHaveBeenCalled();
    controller.abort();
    vi.advanceTimersByTime(25);

    await expect(second).rejects.toThrow("cancelled");
    expect(operation).not.toHaveBeenCalled();
  });
});

describe("ExtensionUserStorage", () => {
  it("stores JSON values without touching core localStorage", async () => {
    localStorage.setItem("omnigent:theme", "dark");
    const storage = new ExtensionUserStorage("server-a", "user-a", "acme.one");

    await storage.set("layout.v1", { x: 1, y: 2 });
    expect(await storage.get("layout.v1")).toEqual({ x: 1, y: 2 });
    expect(localStorage.getItem("omnigent:theme")).toBe("dark");

    await storage.delete("layout.v1");
    expect(await storage.get("layout.v1")).toBeNull();
  });

  it("scopes values by server, user, and extension", async () => {
    const original = new ExtensionUserStorage("server-a", "user-a", "acme.one");
    const otherServer = new ExtensionUserStorage("server-b", "user-a", "acme.one");
    const otherUser = new ExtensionUserStorage("server-a", "user-b", "acme.one");
    const otherExtension = new ExtensionUserStorage("server-a", "user-a", "acme.two");
    await original.set("key", "secret");

    await expect(otherServer.get("key")).resolves.toBeNull();
    await expect(otherUser.get("key")).resolves.toBeNull();
    await expect(otherExtension.get("key")).resolves.toBeNull();
  });

  it("rejects invalid keys and non-JSON values with typed errors", async () => {
    const storage = new ExtensionUserStorage("server", "user", "acme.one");

    await expect(storage.set("../escape", 1)).rejects.toMatchObject({ code: "InvalidKey" });
    await expect(storage.set("value", BigInt(1))).rejects.toMatchObject({ code: "InvalidValue" });
  });

  it("rejects per-value and aggregate quota overflow without eviction", async () => {
    const storage = new ExtensionUserStorage("server", "user", "acme.quota");
    await expect(storage.set("large", "x".repeat(33 * 1024))).rejects.toMatchObject({
      code: "QuotaExceeded",
    });

    await Promise.all(
      Array.from({ length: 8 }, (_, index) => storage.set(`value-${index}`, "x".repeat(32_000))),
    );
    const error = await storage.set("overflow", "x".repeat(32_000)).catch((reason) => reason);
    expect(error).toBeInstanceOf(ExtensionStorageError);
    expect(error).toMatchObject({ code: "QuotaExceeded" });
    await expect(storage.get("value-0")).resolves.toBe("x".repeat(32_000));
    await expect(storage.get("overflow")).resolves.toBeNull();
    localStorage.setItem("omnigent:after-extension-quota", "still-works");
    expect(localStorage.getItem("omnigent:after-extension-quota")).toBe("still-works");
  });

  it("enforces the key-count cap independently of the byte cap", async () => {
    const storage = new ExtensionUserStorage("server", "user", "acme.keys");
    await Promise.all(Array.from({ length: 128 }, (_, index) => storage.set(`key-${index}`, null)));

    await expect(storage.set("key-128", null)).rejects.toMatchObject({
      code: "QuotaExceeded",
    });
  });
});
