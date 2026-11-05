import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import type * as IdentityModule from "@/lib/identity";
import {
  readSessionWorkspaceState,
  SESSION_WORKSPACE_STATE_KEY,
  writeSessionWorkspaceState,
} from "@/lib/sessionWorkspaceState";
import { subagentTabId } from "@/lib/subagentTabs";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useSideChats } from "./useSideChats";

type SideChatsHook = ReturnType<typeof useSideChats>;

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));

let queryClient: QueryClient;
function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

interface SideChatRow {
  id: string;
  created_at: number;
  closed?: boolean;
}

/** Serve the parent's side chats in id order, two to a page, or 404 for an older server. */
function serverSideChats(rows: SideChatRow[] | null) {
  vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
    if (rows === null) return new Response("{}", { status: 404 });
    const url = new URL(typeof input === "string" ? input : input.toString(), "http://test");
    const after = url.searchParams.get("after");
    const start = after === null ? 0 : rows.findIndex((row) => row.id === after) + 1;
    const data = rows.slice(start, start + 2);
    return new Response(
      JSON.stringify({ data, has_more: start + 2 < rows.length, last_id: data.at(-1)?.id ?? null }),
    );
  });
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(authenticatedFetch).mockClear();
  serverSideChats([]);
});

afterEach(() => {
  cleanup();
  useChatStore.setState({ queuedMessages: [] });
  conversationRegistry.clear();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("side-chat soft tabs", () => {
  it("opens an empty pending tab, then rekeys it in place to the real child", () => {
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
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
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
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
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
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
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
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
      wrapper,
    });
    expect(result.current.tabs).toEqual(["conv_a1"]);
    rerender({ id: "session-b" });
    expect(result.current.tabs).toEqual(["conv_b1"]);
    expect(result.current.selected).toBe("conv_b1");
  });

  it("persists tabs and selection across remount, isolated per session", () => {
    const first = renderHook(() => useSideChats("session-a"), { wrapper });
    act(() => first.result.current.open("conv_a1"));
    act(() => first.result.current.open("conv_a2"));
    first.unmount();

    const restored = renderHook(() => useSideChats("session-a"), { wrapper });
    expect(restored.result.current.tabs).toEqual(["conv_a1", "conv_a2"]);
    expect(restored.result.current.selected).toBe("conv_a2");
    expect(readSessionWorkspaceState("session-a")).toEqual({
      openSideChats: ["conv_a1", "conv_a2"],
      selectedSideChatId: "conv_a2",
    });

    const other = renderHook(() => useSideChats("session-b"), { wrapper });
    expect(other.result.current.tabs).toEqual([]);
  });

  it("closes the target tab and selects a neighbor, then clears", () => {
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
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
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
    act(() => result.current.open("conv_1"));
    act(() => result.current.open("conv_2"));
    // conv_2 is selected; closing the background conv_1 leaves it selected.
    act(() => result.current.close("conv_1"));
    expect(result.current.tabs).toEqual(["conv_2"]);
    expect(result.current.selected).toBe("conv_2");
  });

  it("select changes the active tab and can clear it", () => {
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
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
    const first = renderHook(() => useSideChats("session-a"), { wrapper });
    act(() => first.result.current.open(subagentTab));
    act(() => first.result.current.open("conv_a"));
    act(() => first.result.current.select(subagentTab));
    first.unmount();

    const reloaded = renderHook(() => useSideChats("session-a"), { wrapper });
    expect(reloaded.result.current.tabs).toEqual(["conv_a", subagentTab]);
    expect(reloaded.result.current.selected).toBe(subagentTab);
  });

  it("stay out of the side-chat list older clients read", () => {
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
    act(() => result.current.open("conv_a"));
    act(() => result.current.open(subagentTab));
    expect(readSessionWorkspaceState("session-a")).toEqual({
      openSideChats: ["conv_a"],
      openSubagentTabs: [subagentTab],
      selectedSideChatId: subagentTab,
    });

    // Without sub-agent tabs, the store is written exactly as before.
    act(() => result.current.close(subagentTab));
    expect(readSessionWorkspaceState("session-a")).toEqual({
      openSideChats: ["conv_a"],
      selectedSideChatId: "conv_a",
    });
  });

  it.each([
    ["opened", (hook: SideChatsHook) => hook.open("conv_b"), ["conv_a", "conv_b"], [subagentTab]],
    ["closed", (hook: SideChatsHook) => hook.close(subagentTab), ["conv_a"], undefined],
  ])("keep a tab another window %s", (_change, change, sideChats, subagentTabs) => {
    const first = renderHook(() => useSideChats("session-a"), { wrapper });
    act(() => first.result.current.open("conv_a"));
    act(() => first.result.current.open(subagentTab));
    const second = renderHook(() => useSideChats("session-a"), { wrapper });

    act(() => change(second.result.current));
    act(() => first.result.current.select("conv_a"));

    expect(readSessionWorkspaceState("session-a").openSideChats).toEqual(sideChats);
    expect(readSessionWorkspaceState("session-a").openSubagentTabs).toEqual(subagentTabs);
  });

  it("are the only tabs lost when an older client rewrites the store", () => {
    const first = renderHook(() => useSideChats("session-a"), { wrapper });
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

    const reloaded = renderHook(() => useSideChats("session-a"), { wrapper });
    expect(reloaded.result.current.tabs).toEqual(["conv_a"]);
    expect(reloaded.result.current.selected).toBeNull();
  });

  it("let a pending side chat rekey in place beside them", () => {
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
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
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
    act(() => result.current.open("conv_a"));
    act(() => result.current.open(subagentTab));
    act(() => result.current.close(subagentTab));
    expect(result.current.tabs).toEqual(["conv_a"]);
    expect(result.current.selected).toBe("conv_a");
  });
});

describe("side-chat tabs when storage writes fail", () => {
  it("keep one chat's unsaved changes out of the next chat", () => {
    const hook = renderHook(({ id }) => useSideChats(id), {
      initialProps: { id: "session-a" },
      wrapper,
    });
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

  it.each([
    ["closes a tab", (hook: SideChatsHook) => hook.close("conv_z"), ["conv_x"]],
    ["opens a tab", (hook: SideChatsHook) => hook.open("conv_y"), ["conv_z", "conv_y", "conv_x"]],
  ])(
    "replay an unsaved change on top of what another window then %s",
    (_change, otherWindowChange, sideChats) => {
      const subagentTab = subagentTabId("conv_child");
      const first = renderHook(() => useSideChats("session-a"), { wrapper });
      act(() => first.result.current.open("conv_z"));
      act(() => first.result.current.open(subagentTab));
      const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
        throw new DOMException("Quota exceeded", "QuotaExceededError");
      });
      act(() => first.result.current.open("conv_x"));
      setItem.mockRestore();

      const second = renderHook(() => useSideChats("session-a"), { wrapper });
      act(() => otherWindowChange(second.result.current));
      act(() => first.result.current.select(subagentTab));

      expect(readSessionWorkspaceState("session-a")).toEqual({
        openSideChats: sideChats,
        openSubagentTabs: [subagentTab],
        selectedSideChatId: subagentTab,
      });
    },
  );

  it("keep opening, selecting and closing, then save once storage works", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Quota exceeded", "QuotaExceededError");
    });
    const subagentTab = subagentTabId("conv_child");
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
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

  it("still drop a tab closed on another device", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Quota exceeded", "QuotaExceededError");
    });
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
    await waitFor(() =>
      expect(queryClient.getQueryCache().getAll()[0]?.state.status).toBe("success"),
    );
    // Opened while writes fail, so only the unsaved changes hold this tab.
    act(() => result.current.open("conv_side"));
    expect(result.current.selected).toBe("conv_side");

    serverSideChats([{ id: "conv_side", created_at: 1, closed: true }]);
    await act(() => queryClient.invalidateQueries());

    await waitFor(() => expect(result.current.tabs).toEqual([]));
    expect(result.current.selected).toBeNull();
    setItem.mockRestore();
  });
});

describe("side chats opened on another device", () => {
  it.each(["queued", "sending", "running"])(
    "keeps a remotely closed tab and its selection while work is %s here",
    async (work) => {
      writeSessionWorkspaceState("session-a", {
        openSideChats: ["conv_busy", "conv_idle"],
        selectedSideChatId: "conv_busy",
      });
      const queued = { queueId: "followup", conversationId: "conv_busy", text: "Check tests" };
      if (work === "queued") useChatStore.setState({ queuedMessages: [queued] });
      const entry = conversationRegistry.acquire("conv_busy");
      entry.setState({
        status: work === "sending" ? "streaming" : "idle",
        sessionStatus: work === "running" ? "running" : "idle",
      });
      serverSideChats([
        { id: "conv_busy", created_at: 1, closed: true },
        { id: "conv_idle", created_at: 2, closed: true },
      ]);

      const { result } = renderHook(() => useSideChats("session-a"), { wrapper });

      await waitFor(() => expect(result.current.tabs).toEqual(["conv_busy"]));
      expect(result.current.selected).toBe("conv_busy");
      expect(useChatStore.getState().queuedMessages).toEqual(work === "queued" ? [queued] : []);
      expect(readSessionWorkspaceState("session-a").openSideChats).toEqual(["conv_busy"]);

      // A fresh list can apply the close once this device's work has settled.
      act(() => {
        useChatStore.setState({ queuedMessages: [] });
        entry.setState({ status: "idle", sessionStatus: "idle" });
      });
      await act(() => queryClient.invalidateQueries());
      await waitFor(() => expect(result.current.tabs).toEqual([]));
      expect(result.current.selected).toBeNull();
    },
  );

  it("are added in creation order, and ones closed there are dropped", async () => {
    writeSessionWorkspaceState("session-a", {
      openSideChats: ["conv_closed", "conv_local", "pending:draft"],
      selectedSideChatId: "conv_closed",
    });
    serverSideChats([
      { id: "conv_b_newer", created_at: 30 },
      { id: "conv_closed", created_at: 10, closed: true },
      { id: "conv_c_older", created_at: 20 },
    ]);

    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });

    // A tab the server doesn't list (just created here, or an older side chat) stays.
    await waitFor(() =>
      expect(result.current.tabs).toEqual([
        "conv_local",
        "pending:draft",
        "conv_c_older",
        "conv_b_newer",
      ]),
    );
    expect(result.current.selected).toBeNull();
    expect(readSessionWorkspaceState("session-a").openSideChats).toEqual(result.current.tabs);
    const urls = vi.mocked(authenticatedFetch).mock.calls.map(([input]) => String(input));
    expect(urls).toEqual([
      "/v1/sessions/session-a/side_chats?limit=100",
      "/v1/sessions/session-a/side_chats?limit=100&after=conv_closed",
    ]);
  });

  it("appear when the window regains focus, despite the app's query defaults", async () => {
    // The app turns focus refetches off and caches for 30 seconds; this list opts back in.
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false, staleTime: 30_000 } },
    });
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
    await waitFor(() =>
      expect(queryClient.getQueryCache().getAll()[0]?.state.status).toBe("success"),
    );

    serverSideChats([{ id: "conv_remote", created_at: 1 }]);
    try {
      act(() => {
        focusManager.setFocused(false);
        focusManager.setFocused(true);
      });
      await waitFor(() => expect(result.current.tabs).toEqual(["conv_remote"]));
    } finally {
      focusManager.setFocused(undefined);
    }
  });

  it("stay closed when the refresh after closing fails", async () => {
    serverSideChats([{ id: "conv_side", created_at: 1 }]);
    const { result, rerender } = renderHook(({ id }) => useSideChats(id), {
      initialProps: { id: "session-a" },
      wrapper,
    });
    await waitFor(() => expect(result.current.tabs).toEqual(["conv_side"]));

    serverSideChats(null);
    act(() => result.current.close("conv_side"));
    await waitFor(() =>
      expect(queryClient.getQueryCache().getAll()[0]?.state.fetchStatus).toBe("idle"),
    );
    rerender({ id: "session-b" });
    rerender({ id: "session-a" });

    expect(result.current.tabs).toEqual([]);
    expect(readSessionWorkspaceState("session-a").openSideChats).toEqual([]);
  });

  it("stay closed when a focus refresh is in flight and the next one fails", async () => {
    serverSideChats([{ id: "conv_side", created_at: 1 }]);
    const { result, rerender } = renderHook(({ id }) => useSideChats(id), {
      initialProps: { id: "session-a" },
      wrapper,
    });
    await waitFor(() => expect(result.current.tabs).toEqual(["conv_side"]));

    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    // The refresh still in flight was read before the close and names it open.
    vi.mocked(authenticatedFetch).mockImplementationOnce(async () => {
      await held;
      return new Response(
        JSON.stringify({ data: [{ id: "conv_side", created_at: 1 }], last_id: "conv_side" }),
      );
    });
    try {
      act(() => {
        focusManager.setFocused(false);
        focusManager.setFocused(true);
      });
      await waitFor(() => expect(vi.mocked(authenticatedFetch)).toHaveBeenCalledTimes(2));
      serverSideChats(null);
      act(() => result.current.close("conv_side"));
      release();
      await waitFor(() => expect(vi.mocked(authenticatedFetch)).toHaveBeenCalledTimes(3));
      await waitFor(() =>
        expect(queryClient.getQueryCache().getAll()[0]?.state.fetchStatus).toBe("idle"),
      );
    } finally {
      focusManager.setFocused(undefined);
    }
    rerender({ id: "session-b" });
    rerender({ id: "session-a" });

    expect(result.current.tabs).toEqual([]);
    expect(readSessionWorkspaceState("session-a").openSideChats).toEqual([]);
  });

  it("stay closed when closed before the first list arrives", async () => {
    writeSessionWorkspaceState("session-a", { openSideChats: ["conv_side"] });
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    // The first list, read before the close, still names the side chat open.
    vi.mocked(authenticatedFetch).mockImplementationOnce(async () => {
      await held;
      return new Response(
        JSON.stringify({ data: [{ id: "conv_side", created_at: 1 }], last_id: "conv_side" }),
      );
    });
    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });
    expect(result.current.tabs).toEqual(["conv_side"]);

    serverSideChats([{ id: "conv_side", created_at: 1, closed: true }]);
    act(() => result.current.close("conv_side"));
    release();
    await waitFor(() => expect(vi.mocked(authenticatedFetch)).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(queryClient.getQueryCache().getAll()[0]?.state.fetchStatus).toBe("idle"),
    );

    expect(result.current.tabs).toEqual([]);
    expect(readSessionWorkspaceState("session-a").openSideChats).toEqual([]);
  });

  it("leave local tabs alone when the server can't list them", async () => {
    writeSessionWorkspaceState("session-a", { openSideChats: ["conv_local"] });
    serverSideChats(null);

    const { result } = renderHook(() => useSideChats("session-a"), { wrapper });

    await waitFor(() =>
      expect(queryClient.getQueryCache().getAll()[0]?.state.status).toBe("error"),
    );
    expect(result.current.tabs).toEqual(["conv_local"]);
  });

  it("never push a local tab out of storage to make room for them", async () => {
    writeSessionWorkspaceState("session-a", { openSideChats: ["pending:draft"] });
    // Storage keeps 20 side-chat tabs; the newest 19 listed ones fit beside the draft.
    const listed = Array.from({ length: 20 }, (_, index) => ({
      id: `conv_${String(index).padStart(2, "0")}`,
      created_at: index,
    }));
    serverSideChats(listed);

    const first = renderHook(() => useSideChats("session-a"), { wrapper });

    const shown = ["pending:draft", ...listed.slice(1).map((row) => row.id)];
    await waitFor(() => expect(first.result.current.tabs).toEqual(shown));
    expect(readSessionWorkspaceState("session-a").openSideChats).toEqual(shown);
    first.unmount();
    // After a reload the draft is still a tab, and the list doesn't reshuffle anything.
    const reloaded = renderHook(() => useSideChats("session-a"), { wrapper });
    await waitFor(() =>
      expect(queryClient.getQueryCache().getAll()[0]?.state.dataUpdateCount).toBe(2),
    );
    expect(reloaded.result.current.tabs).toEqual(shown);
  });
});
