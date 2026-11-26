import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MAX_SAVED_SESSIONS } from "@/lib/savedSessionEntries";
import { SESSION_WORKSPACE_STATE_KEY } from "@/lib/sessionWorkspaceState";
import type { TerminalSplitState } from "@/lib/paneSplitLayout";

import {
  PANE_SPLITS_KEY,
  readPaneSplit,
  resetPaneSplitsCache,
  updatePaneSplit,
  usePaneSplit,
} from "./paneSplitStore";

// A fifth of the row, which the Terminal view's own split would refuse to restore.
const NARROW: TerminalSplitState = {
  layout: { axis: "columns", ratio: 0.2, children: ["agent", "app:files"] },
  focusedKey: "app:files",
};

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  resetPaneSplitsCache();
});

describe("paneSplitStore", () => {
  it("saves a session's split under its own key and reads it back after a reload", () => {
    updatePaneSplit("session-1", () => NARROW);
    expect(window.localStorage.getItem(SESSION_WORKSPACE_STATE_KEY)).toBeNull();
    resetPaneSplitsCache();
    expect(readPaneSplit("session-1")).toEqual(NARROW);
    expect(readPaneSplit("session-2")).toBeUndefined();
  });

  it("forgets a session's split when it goes back to one pane, keeping the others", () => {
    updatePaneSplit("session-1", () => NARROW);
    updatePaneSplit("session-2", () => NARROW);
    updatePaneSplit("session-1", () => undefined);
    resetPaneSplitsCache();
    expect(readPaneSplit("session-1")).toBeUndefined();
    expect(readPaneSplit("session-2")).toEqual(NARROW);
  });

  it("keeps only the most recently changed sessions' splits", () => {
    for (let index = 0; index <= MAX_SAVED_SESSIONS; index++) {
      updatePaneSplit(`session-${index}`, () => NARROW);
    }
    resetPaneSplitsCache();
    expect(readPaneSplit("session-0")).toBeUndefined();
    expect(readPaneSplit("session-1")).toEqual(NARROW);
    expect(readPaneSplit(`session-${MAX_SAVED_SESSIONS}`)).toEqual(NARROW);
  });

  it("restores a malformed saved split as unsplit", () => {
    window.localStorage.setItem(
      PANE_SPLITS_KEY,
      JSON.stringify({ sessions: { "session-1": { layout: "agent", focusedKey: "agent" } } }),
    );
    expect(readPaneSplit("session-1")).toBeUndefined();
    window.localStorage.setItem(PANE_SPLITS_KEY, "not json");
    resetPaneSplitsCache();
    expect(readPaneSplit("session-1")).toBeUndefined();
  });

  it("changes nothing when an update hands back the same split", () => {
    updatePaneSplit("session-1", () => NARROW);
    let renders = 0;
    renderHook(() => {
      renders += 1;
      return usePaneSplit("session-1");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    act(() => updatePaneSplit("session-1", (current) => current));
    expect(setItem).not.toHaveBeenCalled();
    expect(renders).toBe(1);
  });

  it("keeps a split for this page when storage refuses the write", () => {
    const { result } = renderHook(() => usePaneSplit("session-1"));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Full", "QuotaExceededError");
    });
    act(() => updatePaneSplit("session-1", () => NARROW));
    expect(result.current).toEqual(NARROW);
    expect(window.localStorage.getItem(PANE_SPLITS_KEY)).toBeNull();
  });

  it("re-renders a view as its session's split changes, here or in another window", () => {
    const { result } = renderHook(() => usePaneSplit("session-1"));
    expect(result.current).toBeUndefined();
    act(() => updatePaneSplit("session-1", () => NARROW));
    expect(result.current).toEqual(NARROW);

    const moved = { ...NARROW, focusedKey: "agent" };
    const newValue = JSON.stringify({ sessions: { "session-1": moved } });
    act(() => {
      window.localStorage.setItem(PANE_SPLITS_KEY, newValue);
      window.dispatchEvent(new StorageEvent("storage", { key: PANE_SPLITS_KEY, newValue }));
    });
    expect(result.current).toEqual(moved);
  });
});
