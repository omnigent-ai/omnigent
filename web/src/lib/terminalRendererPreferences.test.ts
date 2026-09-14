import { afterEach, describe, expect, it, vi } from "vitest";
import {
  normalizeTerminalRendererMode,
  readTerminalRendererMode,
  resolveTerminalWebglEnabled,
  subscribeTerminalRenderer,
  TERMINAL_RENDERER_DEFAULT,
  writeTerminalRendererMode,
} from "./terminalRendererPreferences";

const STORAGE_KEY = "omnigent:terminal-renderer";

afterEach(() => {
  localStorage.clear();
});

describe("terminalRendererPreferences — read/write", () => {
  it("returns auto when nothing is stored", () => {
    expect(readTerminalRendererMode()).toBe(TERMINAL_RENDERER_DEFAULT);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("stores the raw dom string", () => {
    writeTerminalRendererMode("dom");
    expect(readTerminalRendererMode()).toBe("dom");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("dom");
  });

  it("removes the key when written auto", () => {
    writeTerminalRendererMode("dom");
    expect(localStorage.getItem(STORAGE_KEY)).not.toBeNull();
    writeTerminalRendererMode("auto");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(readTerminalRendererMode()).toBe("auto");
  });
});

describe("normalizeTerminalRendererMode", () => {
  it("passes through valid modes", () => {
    expect(normalizeTerminalRendererMode("auto")).toBe("auto");
    expect(normalizeTerminalRendererMode("dom")).toBe("dom");
  });

  it("maps unknown, null, and garbage to auto", () => {
    expect(normalizeTerminalRendererMode("webgl")).toBe("auto");
    expect(normalizeTerminalRendererMode("canvas")).toBe("auto");
    expect(normalizeTerminalRendererMode(null)).toBe("auto");
    expect(normalizeTerminalRendererMode(undefined)).toBe("auto");
  });
});

describe("resolveTerminalWebglEnabled", () => {
  it("enables WebGL in auto mode", () => {
    expect(resolveTerminalWebglEnabled("auto")).toBe(true);
  });

  it("disables WebGL in dom mode", () => {
    expect(resolveTerminalWebglEnabled("dom")).toBe(false);
  });
});

describe("terminalRendererPreferences — pub/sub", () => {
  it("notifies subscribers with the written mode", () => {
    const cb = vi.fn();
    const unsubscribe = subscribeTerminalRenderer(cb);

    writeTerminalRendererMode("dom");
    expect(cb).toHaveBeenCalledWith("dom");

    unsubscribe();
  });

  it("stops notifying after unsubscribe", () => {
    const cb = vi.fn();
    const unsubscribe = subscribeTerminalRenderer(cb);
    unsubscribe();

    writeTerminalRendererMode("dom");
    expect(cb).not.toHaveBeenCalled();
  });
});
