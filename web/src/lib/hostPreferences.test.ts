import { afterEach, describe, expect, it, vi } from "vitest";
import {
  readLastHostChoice,
  writeLastHostChoice,
  readLastSandboxProvider,
  writeLastSandboxProvider,
  SANDBOX_HOST_CHOICE,
} from "./hostPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("hostPreferences", () => {
  it("returns null when nothing is stored", () => {
    // A first-time visitor has no host on record — read must say so (null)
    // rather than invent one, so the composer falls back to its default.
    expect(readLastHostChoice()).toBeNull();
  });

  it("round-trips a written host id", () => {
    writeLastHostChoice("host_abc");
    // The exact id written must come back — this is what restores the picker
    // on the next visit.
    expect(readLastHostChoice()).toBe("host_abc");
  });

  it("round-trips the sandbox sentinel", () => {
    // The sandbox option has no host id, so it persists as the reserved
    // sentinel; it must survive the round trip distinctly from any host id.
    writeLastHostChoice(SANDBOX_HOST_CHOICE);
    expect(readLastHostChoice()).toBe(SANDBOX_HOST_CHOICE);
  });

  it("overwrites the previous pick", () => {
    writeLastHostChoice("host_one");
    writeLastHostChoice(SANDBOX_HOST_CHOICE);
    // Only the latest pick matters; the preference is a single slot.
    expect(readLastHostChoice()).toBe(SANDBOX_HOST_CHOICE);
  });

  it("never throws when storage is inaccessible", () => {
    // Private-mode / quota failures surface as throws from the Storage API.
    // Both helpers must swallow them — a broken preference must not break
    // session creation.
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeLastHostChoice("host_x")).not.toThrow();
    expect(readLastHostChoice()).toBeNull();
  });

  it("round-trips the last sandbox provider", () => {
    // Paired with the sandbox sentinel: names which provider the sandbox pick
    // launched on, so the composer reopens on it.
    writeLastSandboxProvider("modal");
    expect(readLastSandboxProvider()).toBe("modal");
  });

  it("clears the sandbox provider when the pick names none", () => {
    writeLastSandboxProvider("modal");
    // A provider-less server default (null) must clear the slot, not persist
    // the string "null" — the next read falls back to the first offered row.
    writeLastSandboxProvider(null);
    expect(readLastSandboxProvider()).toBeNull();
  });

  it("never throws reading/writing the sandbox provider when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeLastSandboxProvider("modal")).not.toThrow();
    expect(readLastSandboxProvider()).toBeNull();
  });
});
