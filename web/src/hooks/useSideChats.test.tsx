import { act, cleanup, renderHook } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  readSessionWorkspaceState,
  SESSION_WORKSPACE_STATE_KEY,
  writeSessionWorkspaceState,
} from "@/lib/sessionWorkspaceState";
import { subagentTabId } from "@/lib/subagentTabs";
import { useSideChats } from "./useSideChats";

type SideChatsHook = ReturnType<typeof useSideChats>;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
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

  it("rekey to an already-open id merges it into the pending tab's slot, name and pin", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    let pendingId = "";
    act(() => result.current.open("conv_real"));
    act(() => result.current.open("conv_other"));
    act(() => {
      pendingId = result.current.openPending();
    });
    act(() => result.current.setPinned(pendingId, true));
    expect(result.current.tabs).toEqual([pendingId, "conv_real", "conv_other"]);
    act(() => result.current.rekey(pendingId, "conv_real"));
    expect(result.current.tabs).toEqual(["conv_real", "conv_other"]);
    expect(result.current.selected).toBe("conv_real");
    expect(result.current.pinned).toEqual(["conv_real"]);
    expect(result.current.numbers).toEqual({ conv_real: 3, conv_other: 2 });
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
    // Every committed selection, so the switch can't commit session-a's tabs first.
    const committed: (string | null)[] = [];
    const { result, rerender } = renderHook(
      ({ id }) => {
        const sideChats = useSideChats(id);
        useLayoutEffect(() => {
          committed.push(sideChats.selected);
        });
        return sideChats;
      },
      { initialProps: { id: "session-a" } },
    );
    expect(result.current.tabs).toEqual(["conv_a1"]);
    rerender({ id: "session-b" });
    expect(result.current.tabs).toEqual(["conv_b1"]);
    expect(result.current.selected).toBe("conv_b1");
    expect(committed).toEqual(["conv_a1", "conv_b1"]);
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
      sideChatNumbers: { conv_a1: 1, conv_a2: 2 },
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

  it("adds remote chats without duplicates or reopening locally closed tabs", () => {
    const first = renderHook(() => useSideChats("session-a"));
    act(() => first.result.current.open("local"));
    act(() => first.result.current.open(subagentTabId("native")));
    act(() => first.result.current.select("local"));
    act(() => first.result.current.restore(["remote", "local", "native"]));
    expect(first.result.current.tabs).toEqual(["local", "remote", subagentTabId("native")]);
    expect(first.result.current.selected).toBe("local");
    act(() => first.result.current.close("remote"));
    act(() => first.result.current.close(subagentTabId("native")));
    first.unmount();

    const restored = renderHook(() => useSideChats("session-a"));
    act(() => restored.result.current.restore(["remote", "new-remote", "native"]));
    expect(restored.result.current.tabs).toEqual(["local", "new-remote"]);
    expect(restored.result.current.selected).toBe("local");
    act(() => restored.result.current.open("remote"));
    expect(readSessionWorkspaceState("session-a").closedSideChats).toEqual(["native"]);
    restored.unmount();

    localStorage.clear();
    const otherDevice = renderHook(() => useSideChats("session-a"));
    act(() => otherDevice.result.current.restore(["remote"]));
    expect(otherDevice.result.current.tabs).toEqual(["remote"]);
    expect(otherDevice.result.current.selected).toBe("remote");
  });

  it("keeps existing tabs when discovery would exceed the stored tab limit", () => {
    const local = Array.from({ length: 19 }, (_, i) => `local-${i}`);
    writeSessionWorkspaceState("session-a", { openSideChats: local, selectedSideChatId: local[0] });
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.restore(["remote-1", "remote-2"]));
    expect(result.current.tabs).toEqual([...local, "remote-1"]);
    expect(readSessionWorkspaceState("session-a").openSideChats).toEqual(result.current.tabs);
    expect(result.current.selected).toBe(local[0]);
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

describe("sub-agent tabs", () => {
  const subagentTab = subagentTabId("conv_child");

  it("restore after a reload, after the side chats, with the selection", () => {
    const first = renderHook(() => useSideChats("session-a"));
    act(() => first.result.current.open(subagentTab));
    act(() => first.result.current.open("conv_a"));
    act(() => first.result.current.select(subagentTab));
    first.unmount();

    const reloaded = renderHook(() => useSideChats("session-a"));
    expect(reloaded.result.current.tabs).toEqual(["conv_a", subagentTab]);
    expect(reloaded.result.current.selected).toBe(subagentTab);
  });

  it("stay out of the side-chat list older clients read", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.open("conv_a"));
    act(() => result.current.open(subagentTab));
    expect(readSessionWorkspaceState("session-a")).toEqual({
      openSideChats: ["conv_a"],
      openSubagentTabs: [subagentTab],
      selectedSideChatId: subagentTab,
      sideChatNumbers: { conv_a: 1 },
    });

    // Closing a native side chat opened as a sub-agent must also survive discovery.
    act(() => result.current.close(subagentTab));
    expect(readSessionWorkspaceState("session-a")).toEqual({
      openSideChats: ["conv_a"],
      closedSideChats: ["conv_child"],
      selectedSideChatId: "conv_a",
      sideChatNumbers: { conv_a: 1 },
    });
  });

  it.each([
    ["opened", (hook: SideChatsHook) => hook.open("conv_b"), ["conv_a", "conv_b"], [subagentTab]],
    ["closed", (hook: SideChatsHook) => hook.close(subagentTab), ["conv_a"], undefined],
  ])("keep a tab another window %s", (_change, change, sideChats, subagentTabs) => {
    const first = renderHook(() => useSideChats("session-a"));
    act(() => first.result.current.open("conv_a"));
    act(() => first.result.current.open(subagentTab));
    const second = renderHook(() => useSideChats("session-a"));

    act(() => change(second.result.current));
    act(() => first.result.current.select("conv_a"));

    expect(readSessionWorkspaceState("session-a").openSideChats).toEqual(sideChats);
    expect(readSessionWorkspaceState("session-a").openSubagentTabs).toEqual(subagentTabs);
  });

  it("are the only tabs lost when an older client rewrites the store", () => {
    const first = renderHook(() => useSideChats("session-a"));
    act(() => first.result.current.open("conv_a"));
    act(() => first.result.current.open(subagentTab));
    first.unmount();
    // An older client keeps only the fields it knows when it rewrites the store.
    const raw = JSON.parse(localStorage.getItem(SESSION_WORKSPACE_STATE_KEY)!) as {
      id: string;
      state: Record<string, unknown>;
    }[];
    const olderClientStore = raw.map(({ id, state }) => ({
      id,
      state: { openSideChats: state.openSideChats, selectedSideChatId: state.selectedSideChatId },
    }));
    localStorage.setItem(SESSION_WORKSPACE_STATE_KEY, JSON.stringify(olderClientStore));

    const reloaded = renderHook(() => useSideChats("session-a"));
    expect(reloaded.result.current.tabs).toEqual(["conv_a"]);
    expect(reloaded.result.current.selected).toBeNull();
  });

  it("let a pending side chat rekey in place beside them", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.open(subagentTab));
    let pendingId = "";
    act(() => {
      pendingId = result.current.openPending();
    });
    act(() => result.current.rekey(pendingId, "conv_real"));
    expect(result.current.tabs).toEqual(["conv_real", subagentTab]);
    expect(result.current.selected).toBe("conv_real");
  });

  it("hand the selection to the strip's left neighbour on close", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.open("conv_a"));
    act(() => result.current.open(subagentTab));
    act(() => result.current.close(subagentTab));
    expect(result.current.tabs).toEqual(["conv_a"]);
    expect(result.current.selected).toBe("conv_a");
  });
});

describe("side-chat tabs when storage writes fail", () => {
  it("keep one chat's unsaved changes out of the next chat", () => {
    const hook = renderHook(({ id }) => useSideChats(id), { initialProps: { id: "session-a" } });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Quota exceeded", "QuotaExceededError");
    });
    act(() => hook.result.current.open("conv_unsaved"));
    setItem.mockRestore();

    hook.rerender({ id: "session-b" });
    act(() => hook.result.current.open("conv_b"));

    expect(hook.result.current.tabs).toEqual(["conv_b"]);
    expect(readSessionWorkspaceState("session-b").openSideChats).toEqual(["conv_b"]);
    expect(readSessionWorkspaceState("session-a")).toEqual({});
  });

  // The unsaved tab keeps its number unless the other window has since given it out.
  it.each([
    ["closes a tab", (hook: SideChatsHook) => hook.close("conv_z"), ["conv_x"], { conv_x: 2 }],
    [
      "opens a tab",
      (hook: SideChatsHook) => hook.open("conv_y"),
      ["conv_z", "conv_y", "conv_x"],
      { conv_z: 1, conv_y: 2, conv_x: 3 },
    ],
  ])(
    "replay an unsaved change on top of what another window then %s",
    (_change, otherWindowChange, sideChats, sideChatNumbers) => {
      const subagentTab = subagentTabId("conv_child");
      const first = renderHook(() => useSideChats("session-a"));
      act(() => first.result.current.open("conv_z"));
      act(() => first.result.current.open(subagentTab));
      const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
        throw new DOMException("Quota exceeded", "QuotaExceededError");
      });
      act(() => first.result.current.open("conv_x"));
      setItem.mockRestore();
      expect(first.result.current.numbers).toEqual({ conv_z: 1, conv_x: 2 });

      const second = renderHook(() => useSideChats("session-a"));
      act(() => otherWindowChange(second.result.current));
      act(() => first.result.current.select(subagentTab));

      expect(readSessionWorkspaceState("session-a")).toEqual({
        openSideChats: sideChats,
        ...(_change === "closes a tab" ? { closedSideChats: ["conv_z"] } : {}),
        openSubagentTabs: [subagentTab],
        selectedSideChatId: subagentTab,
        sideChatNumbers,
      });
      expect(first.result.current.numbers).toEqual(sideChatNumbers);
    },
  );

  it("keep opening, selecting and closing, then save once storage works", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Quota exceeded", "QuotaExceededError");
    });
    const subagentTab = subagentTabId("conv_child");
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.open("conv_a"));
    act(() => result.current.open(subagentTab));
    act(() => result.current.select("conv_a"));
    act(() => result.current.close(subagentTab));
    expect(result.current.tabs).toEqual(["conv_a"]);
    expect(result.current.selected).toBe("conv_a");

    setItem.mockRestore();
    act(() => result.current.open("conv_b"));
    expect(readSessionWorkspaceState("session-a").openSideChats).toEqual(["conv_a", "conv_b"]);
  });
});

describe("side-chat names, pins and order", () => {
  it("keeps each side chat's number when another closes or a draft starts", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    act(() => result.current.open("conv_a"));
    act(() => result.current.open("conv_b"));
    let pendingId = "";
    act(() => {
      pendingId = result.current.openPending();
    });
    act(() => result.current.close("conv_a"));
    act(() => result.current.rekey(pendingId, "conv_c"));
    act(() => result.current.open("conv_d"));

    expect(result.current.tabs).toEqual(["conv_b", "conv_c", "conv_d"]);
    expect(result.current.numbers).toEqual({ conv_b: 2, conv_c: 3, conv_d: 4 });
    // Numbers come from the highest open tab, so they don't climb forever.
    act(() => result.current.close("conv_d"));
    act(() => result.current.open("conv_e"));
    expect(result.current.numbers.conv_e).toBe(4);
  });

  it("numbers tabs saved before numbering in their saved order", () => {
    writeSessionWorkspaceState("session-a", { openSideChats: ["conv_x", "conv_y"] });
    const { result } = renderHook(() => useSideChats("session-a"));
    expect(result.current.numbers).toEqual({ conv_x: 1, conv_y: 2 });
  });

  it("pins tabs to the front and unpins them to the start of the rest", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    for (const id of ["conv_a", "conv_b", "conv_c"]) act(() => result.current.open(id));
    act(() => result.current.setPinned("conv_c", true));
    act(() => result.current.setPinned("conv_b", true));
    expect(result.current.tabs).toEqual(["conv_c", "conv_b", "conv_a"]);
    expect(result.current.pinned).toEqual(["conv_c", "conv_b"]);

    act(() => result.current.setPinned("conv_c", false));
    expect(result.current.tabs).toEqual(["conv_b", "conv_c", "conv_a"]);
    expect(result.current.pinned).toEqual(["conv_b"]);
    // Pinning and moving never renames.
    expect(result.current.numbers).toEqual({ conv_a: 1, conv_b: 2, conv_c: 3 });
  });

  it("keeps a pending tab's pin once it starts, and forgets a closed tab's pin", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    let pendingId = "";
    act(() => {
      pendingId = result.current.openPending();
    });
    act(() => result.current.setPinned(pendingId, true));
    act(() => result.current.rekey(pendingId, "conv_started"));
    expect(result.current.pinned).toEqual(["conv_started"]);

    act(() => result.current.close("conv_started"));
    act(() => result.current.open("conv_started"));
    expect(result.current.pinned).toEqual([]);
  });

  it("moves tabs only within their pinned or unpinned group", () => {
    const { result } = renderHook(() => useSideChats("session-a"));
    for (const id of ["conv_a", "conv_b", "conv_c", "conv_d"]) act(() => result.current.open(id));
    act(() => result.current.setPinned("conv_a", true));
    act(() => result.current.move("conv_d", "conv_b"));
    expect(result.current.tabs).toEqual(["conv_a", "conv_d", "conv_b", "conv_c"]);
    act(() => result.current.move("conv_d", "conv_c"));
    expect(result.current.tabs).toEqual(["conv_a", "conv_b", "conv_c", "conv_d"]);
    // A pinned and an unpinned tab don't trade places.
    act(() => result.current.move("conv_c", "conv_a"));
    expect(result.current.tabs).toEqual(["conv_a", "conv_b", "conv_c", "conv_d"]);
    // Pinned tabs reorder among themselves.
    act(() => result.current.setPinned("conv_b", true));
    act(() => result.current.move("conv_b", "conv_a"));
    expect(result.current.tabs).toEqual(["conv_b", "conv_a", "conv_c", "conv_d"]);
    expect(result.current.pinned).toEqual(["conv_b", "conv_a"]);
  });

  it("restores pins, order and names after a reload", () => {
    const first = renderHook(() => useSideChats("session-a"));
    for (const id of ["conv_a", "conv_b", "conv_c"]) act(() => first.result.current.open(id));
    act(() => first.result.current.setPinned("conv_b", true));
    act(() => first.result.current.move("conv_c", "conv_a"));
    act(() => first.result.current.close("conv_a"));
    first.unmount();

    const reloaded = renderHook(() => useSideChats("session-a"));
    expect(reloaded.result.current.tabs).toEqual(["conv_b", "conv_c"]);
    expect(reloaded.result.current.pinned).toEqual(["conv_b"]);
    expect(reloaded.result.current.numbers).toEqual({ conv_b: 2, conv_c: 3 });
  });
});
