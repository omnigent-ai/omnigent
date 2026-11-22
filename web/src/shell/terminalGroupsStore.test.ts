import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MAX_SAVED_SESSIONS } from "@/lib/savedSessionEntries";
import { writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import type { TerminalGroup } from "@/lib/terminalGroups";

import {
  readTerminalGroups,
  resetTerminalGroupsCache,
  TERMINAL_GROUPS_KEY,
  updateTerminalGroups,
  useTerminalGroups,
} from "./terminalGroupsStore";

const pair = (id: string, left: string, right: string): TerminalGroup => ({
  id,
  split: { layout: { axis: "columns", ratio: 0.5, children: [left, right] }, focusedKey: left },
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  resetTerminalGroupsCache();
});

describe("terminal groups storage", () => {
  it("saves each session's groups under its own key, leaving other sessions and fields alone", () => {
    window.localStorage.setItem(
      TERMINAL_GROUPS_KEY,
      JSON.stringify({ futureField: "kept", sessions: { other: [{ id: "g_other", note: "?" }] } }),
    );
    updateTerminalGroups("conv_a", () => [pair("g_a", "terminal:a", "terminal:b")]);
    const stored = JSON.parse(window.localStorage.getItem(TERMINAL_GROUPS_KEY)!);
    expect(stored.futureField).toBe("kept");
    expect(stored.sessions.other).toEqual([{ id: "g_other", note: "?" }]);
    resetTerminalGroupsCache();
    expect(readTerminalGroups("conv_a")).toEqual([pair("g_a", "terminal:a", "terminal:b")]);
  });

  it("keeps only the most recently changed sessions' groups", () => {
    const groups = [pair("g_a", "terminal:a", "terminal:b")];
    for (let index = 0; index <= MAX_SAVED_SESSIONS; index++) {
      updateTerminalGroups(`conv_${index}`, () => groups);
    }
    resetTerminalGroupsCache();
    expect(readTerminalGroups("conv_0")).toEqual([]);
    expect(readTerminalGroups(`conv_${MAX_SAVED_SESSIONS}`)).toEqual(groups);
  });

  it("survives an older version rewriting the whole workspace state", () => {
    updateTerminalGroups("conv_a", () => [pair("g_a", "terminal:a", "terminal:b")]);
    updateTerminalGroups("conv_b", () => [pair("g_b", "terminal:c", "terminal:d")]);
    // The workspace state's writer rewrites its own store whole, keeping only known fields.
    writeSessionWorkspaceState("conv_a", { openTerminals: ["terminal:a"] });
    writeSessionWorkspaceState("conv_b", { openTerminals: ["terminal:c"] });
    resetTerminalGroupsCache();
    expect(readTerminalGroups("conv_a")).toEqual([pair("g_a", "terminal:a", "terminal:b")]);
    expect(readTerminalGroups("conv_b")).toEqual([pair("g_b", "terminal:c", "terminal:d")]);
  });

  it("removes a session's entry when its last group goes, and ignores damaged storage", () => {
    updateTerminalGroups("conv_a", () => [pair("g_a", "terminal:a", "terminal:b")]);
    updateTerminalGroups("conv_a", () => []);
    expect(JSON.parse(window.localStorage.getItem(TERMINAL_GROUPS_KEY)!).sessions).toEqual({});
    window.localStorage.setItem(TERMINAL_GROUPS_KEY, "{damaged");
    resetTerminalGroupsCache();
    expect(readTerminalGroups("conv_a")).toEqual([]);
  });

  it("keeps the groups for this page when storage refuses the write", () => {
    const { result } = renderHook(() => useTerminalGroups("conv_a"));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Full", "QuotaExceededError");
    });
    act(() => updateTerminalGroups("conv_a", () => [pair("g_a", "terminal:a", "terminal:b")]));
    expect(result.current).toEqual([pair("g_a", "terminal:a", "terminal:b")]);
    expect(window.localStorage.getItem(TERMINAL_GROUPS_KEY)).toBeNull();
  });
});
