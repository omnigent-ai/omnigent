import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { useSideChats } from "./useSideChats";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("side-chat soft tabs", () => {
  it("opens an empty pending tab, then rekeys it in place to the real child", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    let pendingId = "";
    act(() => {
      pendingId = result.current.openPending();
    });
    expect(pendingId.startsWith("pending:")).toBe(true);
    expect(result.current.tabs).toEqual([pendingId]);
    expect(result.current.selected).toBe(pendingId);
    // Rekey preserves position + selection (no disappear/reappear).
    act(() => result.current.rekey(pendingId, "conv_real"));
    expect(result.current.tabs).toEqual(["conv_real"]);
    expect(result.current.selected).toBe("conv_real");
  });

  it("rekey keeps a pending tab's slot among siblings", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    let pendingId = "";
    act(() => result.current.open("conv_first"));
    act(() => {
      pendingId = result.current.openPending();
    });
    act(() => result.current.open("conv_third"));
    // pending is the middle tab; rekey it and it stays middle.
    act(() => result.current.rekey(pendingId, "conv_second"));
    expect(result.current.tabs).toEqual(["conv_first", "conv_second", "conv_third"]);
  });

  it("rekey to an already-open id drops the pending tab", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    let pendingId = "";
    act(() => result.current.open("conv_real"));
    act(() => {
      pendingId = result.current.openPending();
    });
    expect(result.current.tabs).toEqual(["conv_real", pendingId]);
    act(() => result.current.rekey(pendingId, "conv_real"));
    expect(result.current.tabs).toEqual(["conv_real"]);
    expect(result.current.selected).toBe("conv_real");
  });

  it("opens child ids as tabs, selecting each, and is idempotent", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    expect(result.current.tabs).toEqual([]);
    act(() => result.current.open("conv_side1"));
    expect(result.current.tabs).toEqual(["conv_side1"]);
    expect(result.current.selected).toBe("conv_side1");
    act(() => result.current.open("conv_side2"));
    expect(result.current.tabs).toEqual(["conv_side1", "conv_side2"]);
    expect(result.current.selected).toBe("conv_side2");
    // Re-opening an existing child re-selects it without duplicating the tab
    // (a create both returns the id and fires a session_created).
    act(() => result.current.open("conv_side1"));
    expect(result.current.tabs).toEqual(["conv_side1", "conv_side2"]);
    expect(result.current.selected).toBe("conv_side1");
  });

  it("reloads its own tabs when the parent conversation changes without a remount", () => {
    // WorkspacePanel isn't keyed by conversationId, so navigating between
    // conversations re-renders the same hook instance with a new id — the tabs
    // must follow, not linger from the previous conversation.
    writeSessionWorkspaceState("session-a", {
      openSideChats: ["conv_a1"],
      selectedSideChatId: "conv_a1",
    });
    writeSessionWorkspaceState("session-b", {
      openSideChats: ["conv_b1"],
      selectedSideChatId: "conv_b1",
    });
    const { result, rerender } = renderHook(({ id }) => useSideChats(id), {
      initialProps: { id: "session-a" },
    });
    expect(result.current.tabs).toEqual(["conv_a1"]);
    rerender({ id: "session-b" });
    expect(result.current.tabs).toEqual(["conv_b1"]);
    expect(result.current.selected).toBe("conv_b1");
  });

  it("persists tabs and selection across remount, isolated per session", () => {
    const first = renderHook(() => useSideChats("session-a"));
    act(() => first.result.current.open("conv_a1"));
    act(() => first.result.current.open("conv_a2"));
    first.unmount();

    const restored = renderHook(() => useSideChats("session-a"));
    expect(restored.result.current.tabs).toEqual(["conv_a1", "conv_a2"]);
    expect(restored.result.current.selected).toBe("conv_a2");
    expect(readSessionWorkspaceState("session-a")).toEqual({
      openSideChats: ["conv_a1", "conv_a2"],
      selectedSideChatId: "conv_a2",
    });

    const other = renderHook(() => useSideChats("session-b"));
    expect(other.result.current.tabs).toEqual([]);
  });

  it("closes the target tab and selects a neighbor, then clears", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.open("conv_1"));
    act(() => result.current.open("conv_2"));
    act(() => result.current.close("conv_2"));
    expect(result.current.tabs).toEqual(["conv_1"]);
    expect(result.current.selected).toBe("conv_1");
    act(() => result.current.close("conv_1"));
    expect(result.current.tabs).toEqual([]);
    expect(result.current.selected).toBeNull();
  });

  it("keeps the selection when closing a background tab", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.open("conv_1"));
    act(() => result.current.open("conv_2"));
    // conv_2 is selected; closing the background conv_1 leaves it selected.
    act(() => result.current.close("conv_1"));
    expect(result.current.tabs).toEqual(["conv_2"]);
    expect(result.current.selected).toBe("conv_2");
  });

  it("select changes the active tab and can clear it", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.open("conv_1"));
    act(() => result.current.open("conv_2"));
    act(() => result.current.select("conv_1"));
    expect(result.current.selected).toBe("conv_1");
    act(() => result.current.select(null));
    expect(result.current.selected).toBeNull();
    // Tabs are untouched by selection changes.
    expect(result.current.tabs).toEqual(["conv_1", "conv_2"]);
  });
});
