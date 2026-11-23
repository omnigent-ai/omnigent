import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  resetTerminalPlacement,
  useMainOwnedTerminalKeys,
  useMainSplitMembers,
  usePublishMainTerminalKeys,
} from "./terminalPlacement";
import { updatePaneSplit } from "./paneSplitStore";

describe("useMainSplitMembers", () => {
  const conversationId = "session-split-members";
  afterEach(() => updatePaneSplit(conversationId, () => undefined));

  it("names the saved split's shells where the Terminal view shows splits, as they change", () => {
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "columns", ratio: 0.5, children: ["terminal:agent", "terminal:split"] },
      focusedKey: "terminal:split",
    }));
    const { result } = renderHook(() => useMainSplitMembers(conversationId, true));
    expect([...result.current].sort()).toEqual(["terminal:agent", "terminal:split"]);
    // Closed down to one pane, the split is gone and its last shell is no member.
    act(() => updatePaneSplit(conversationId, () => undefined));
    expect(result.current.size).toBe(0);
  });

  it("names none where the view can't show the split, so its shells keep their tabs", () => {
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "rows", ratio: 0.5, children: ["terminal:agent", "terminal:split"] },
      focusedKey: "terminal:agent",
    }));
    expect(renderHook(() => useMainSplitMembers(conversationId, false)).result.current.size).toBe(
      0,
    );
    expect(renderHook(() => useMainSplitMembers(null, true)).result.current.size).toBe(0);
  });
});

describe("useMainOwnedTerminalKeys", () => {
  afterEach(() => resetTerminalPlacement());

  it("joins every view's mounted shells with the split's, per session, as views come and go", () => {
    const splitMembers = new Set(["terminal:split"]);
    const owned = renderHook(
      ({ conversationId }) => useMainOwnedTerminalKeys(conversationId, { splitMembers }),
      { initialProps: { conversationId: "session-a" } },
    );
    const first = renderHook(() => usePublishMainTerminalKeys("session-a", ["terminal:one"]));
    const second = renderHook(() =>
      usePublishMainTerminalKeys("session-a", ["terminal:one", "terminal:two"]),
    );
    renderHook(() => usePublishMainTerminalKeys("session-b", ["terminal:other"]));
    expect([...owned.result.current].sort()).toEqual([
      "terminal:one",
      "terminal:split",
      "terminal:two",
    ]);

    // A shell another view still mounts stays owned after one view lets it go.
    first.unmount();
    expect([...owned.result.current].sort()).toEqual([
      "terminal:one",
      "terminal:split",
      "terminal:two",
    ]);
    second.unmount();
    expect([...owned.result.current]).toEqual(["terminal:split"]);

    owned.rerender({ conversationId: "session-b" });
    expect([...owned.result.current].sort()).toEqual(["terminal:other", "terminal:split"]);
  });
});
