import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { emitBrowserActionRequest } from "@/lib/browserActionBus";
import { readSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import { maybeOpenLinkInApp } from "@/lib/openLinkInApp";
import {
  AGENT_BROWSER_TAB_ID,
  addBrowserTab,
  agentBrowserTabId,
  browserTabIds,
  browserViewId,
  browserViewOwnerId,
  closeBrowserTab,
  openAgentBrowserTab,
  selectAgentBrowserTab,
  useBrowserTabs,
} from "./useBrowserTabs";

afterEach(() => {
  cleanup();
  localStorage.clear();
  Reflect.deleteProperty(window, "omnigentDesktop");
});

describe("browserViewOwnerId", () => {
  it("round-trips browserViewId for agent and user-opened tabs", () => {
    expect(browserViewOwnerId(browserViewId("conv a", AGENT_BROWSER_TAB_ID))).toBe("conv a");
    expect(browserViewOwnerId(browserViewId("conv a", "tab-two"))).toBe("conv a");
  });

  it("returns malformed or undecodable view IDs unchanged", () => {
    expect(browserViewOwnerId("browser-tab:conv")).toBe("browser-tab:conv");
    expect(browserViewOwnerId("browser-tab:%E0%A4%A:tab")).toBe("browser-tab:%E0%A4%A:tab");
  });
});

describe("browser soft tabs", () => {
  it("persists a pending close even if the workspace unmounts", async () => {
    let finishClose!: (value: { ok: boolean }) => void;
    const browserClose = vi.fn(
      () =>
        new Promise<{ ok: boolean }>((resolve) => {
          finishClose = resolve;
        }),
    );
    Object.assign(window, { omnigentDesktop: { browserClose } });
    const { result, unmount } = renderHook(() => useBrowserTabs("session-a"));
    act(() => result.current.add());
    const pendingClose = result.current.close(result.current.selected!);
    unmount();
    await act(async () => {
      finishClose({ ok: true });
      await pendingClose;
    });
    expect(readSessionWorkspaceState("session-a").openBrowsers).toEqual([]);
  });

  it("does not clobber a tab added after remount while a close resolves", async () => {
    let finishClose!: (value: { ok: boolean }) => void;
    const browserClose = vi.fn(
      () =>
        new Promise<{ ok: boolean }>((resolve) => {
          finishClose = resolve;
        }),
    );
    Object.assign(window, { omnigentDesktop: { browserClose } });

    const first = renderHook(() => useBrowserTabs("session-a"));
    act(() => first.result.current.add());
    const firstId = first.result.current.selected!;
    act(() => first.result.current.add());
    const secondId = first.result.current.selected!;
    const pendingClose = first.result.current.close(firstId);
    first.unmount();

    const second = renderHook(() => useBrowserTabs("session-a"));
    act(() => second.result.current.add());
    const thirdId = second.result.current.selected!;

    await act(async () => {
      finishClose({ ok: true });
      await pendingClose;
    });
    second.unmount();
    const reopened = renderHook(() => useBrowserTabs("session-a"));
    expect(reopened.result.current.tabs).toEqual([secondId, thirdId]);
    expect(reopened.result.current.selected).toBe(thirdId);
    expect(readSessionWorkspaceState("session-a")).toEqual({
      openBrowsers: [secondId, thirdId],
      selectedBrowserId: thirdId,
    });
  });

  it("creates independent views and restores the selection on remount", () => {
    const first = renderHook(() => useBrowserTabs("session-a"));
    expect(first.result.current.viewId).toBeNull();
    act(() => first.result.current.add());
    const firstId = first.result.current.selected!;
    act(() => first.result.current.add());
    const secondId = first.result.current.selected!;
    expect(secondId).not.toBe(firstId);
    expect(first.result.current.tabs).toEqual([firstId, secondId]);
    expect(browserViewId("session-a", firstId)).not.toBe(browserViewId("session-b", firstId));
    first.unmount();
    const restored = renderHook(() => useBrowserTabs("session-a"));
    expect(restored.result.current.selected).toBe(secondId);
    expect(restored.result.current.tabs).toEqual([firstId, secondId]);
    const other = renderHook(() => useBrowserTabs("session-b"));
    expect(other.result.current.tabs).toEqual([]);
  });

  it("closes only the target view and selects a neighbor, then no browser", async () => {
    const browserClose = vi.fn().mockResolvedValue({ ok: true });
    Object.assign(window, { omnigentDesktop: { browserClose } });
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    act(() => result.current.add());
    const firstId = result.current.selected!;
    act(() => result.current.add());
    const secondId = result.current.selected!;
    await act(() => result.current.close(secondId));
    expect(browserClose).toHaveBeenCalledWith(browserViewId("session-a", secondId));
    expect(result.current.selected).toBe(firstId);
    await act(() => result.current.close(firstId));
    expect(result.current.viewId).toBeNull();
    expect(readSessionWorkspaceState("session-a").openBrowsers).toEqual([]);
  });

  it("keeps the selection when closing a background tab or a close fails", async () => {
    const browserClose = vi.fn().mockResolvedValue({ ok: true });
    Object.assign(window, { omnigentDesktop: { browserClose } });
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    act(() => result.current.add());
    const firstId = result.current.selected!;
    act(() => result.current.add());
    const secondId = result.current.selected!;
    await act(() => result.current.close(firstId));
    expect(result.current.selected).toBe(secondId);
    browserClose.mockRejectedValueOnce(new Error("disconnected"));
    let closed = true;
    await act(async () => {
      closed = await result.current.close(secondId);
    });
    expect(closed).toBe(false);
    expect(result.current.tabs).toEqual([secondId]);
  });

  it("opens only the owning session's agent browser as a soft tab", () => {
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    act(() => result.current.add());
    const selected = result.current.selected;
    const event = {
      type: "browser_action_request" as const,
      actionId: "navigate-1",
      action: "navigate",
      args: {},
    };
    act(() => emitBrowserActionRequest(event, "session-b"));
    expect(result.current.selected).toBe(selected);
    act(() => emitBrowserActionRequest(event, "session-a"));
    expect(result.current.selected).toBe(AGENT_BROWSER_TAB_ID);
    expect(result.current.tabs).toEqual([selected, AGENT_BROWSER_TAB_ID]);
    expect(result.current.viewId).toBe("session-a");
    expect(result.current.agentBrowser).toBe(true);
  });

  it("closes and later recreates the agent browser soft tab", async () => {
    const browserClose = vi.fn().mockResolvedValue({ ok: true });
    Object.assign(window, { omnigentDesktop: { browserClose } });
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    const event = {
      type: "browser_action_request" as const,
      actionId: "navigate-1",
      action: "navigate",
      args: {},
    };

    act(() => emitBrowserActionRequest(event, "session-a"));
    await act(() => result.current.close(AGENT_BROWSER_TAB_ID));
    expect(browserClose).toHaveBeenCalledWith("session-a");
    expect(result.current.tabs).toEqual([]);
    expect(result.current.selected).toBeNull();

    act(() => emitBrowserActionRequest(event, "session-a"));
    expect(result.current.tabs).toEqual([AGENT_BROWSER_TAB_ID]);
    expect(result.current.selected).toBe(AGENT_BROWSER_TAB_ID);
  });

  it("keeps a newer agent navigation when an earlier close resolves late", async () => {
    let finishClose!: (value: { ok: boolean }) => void;
    const browserClose = vi.fn(
      () =>
        new Promise<{ ok: boolean }>((resolve) => {
          finishClose = resolve;
        }),
    );
    Object.assign(window, { omnigentDesktop: { browserClose } });
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    const navigate = (actionId: string) =>
      emitBrowserActionRequest(
        {
          type: "browser_action_request",
          actionId,
          action: "navigate",
          args: {},
        },
        "session-a",
      );

    act(() => navigate("navigate-1"));
    const pendingClose = result.current.close(AGENT_BROWSER_TAB_ID);
    act(() => navigate("navigate-2"));
    await act(async () => {
      finishClose({ ok: true });
      await pendingClose;
    });

    expect(result.current.tabs).toEqual([AGENT_BROWSER_TAB_ID]);
    expect(result.current.selected).toBe(AGENT_BROWSER_TAB_ID);
  });

  it("keeps newer background navigation after switching sessions during a close", async () => {
    let finishClose!: (value: { ok: boolean }) => void;
    const browserClose = vi.fn(
      () =>
        new Promise<{ ok: boolean }>((resolve) => {
          finishClose = resolve;
        }),
    );
    Object.assign(window, { omnigentDesktop: { browserClose } });
    const hook = renderHook(({ conversationId }) => useBrowserTabs(conversationId), {
      initialProps: { conversationId: "session-a" },
    });

    act(() =>
      emitBrowserActionRequest(
        {
          type: "browser_action_request",
          actionId: "navigate-a-1",
          action: "navigate",
          args: {},
        },
        "session-a",
      ),
    );
    const pendingClose = hook.result.current.close(AGENT_BROWSER_TAB_ID);
    hook.rerender({ conversationId: "session-b" });
    act(() => openAgentBrowserTab("session-a"));
    await act(async () => {
      finishClose({ ok: true });
      await pendingClose;
    });

    expect(readSessionWorkspaceState("session-a")).toMatchObject({
      openBrowsers: [AGENT_BROWSER_TAB_ID],
      selectedBrowserId: AGENT_BROWSER_TAB_ID,
    });
  });
});

describe("agent browser tabs", () => {
  const navigate = (conversationId: string) =>
    emitBrowserActionRequest(
      { type: "browser_action_request", actionId: "nav", action: "navigate", args: {} },
      conversationId,
    );

  it("tracks the agent's current tab and shows it in mounted hooks", () => {
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    expect(agentBrowserTabId("session-a")).toBe(AGENT_BROWSER_TAB_ID);
    let agentTab = "";
    act(() => {
      agentTab = addBrowserTab("session-a", { forAgent: true });
    });
    expect(result.current.selected).toBe(agentTab);
    expect(agentBrowserTabId("session-a")).toBe(agentTab);
    // A user-added tab is shown but does not move the agent.
    act(() => result.current.add());
    expect(agentBrowserTabId("session-a")).toBe(agentTab);
    // The agent's next navigation brings its own tab back to the front.
    act(() => navigate("session-a"));
    expect(result.current.selected).toBe(agentTab);
    expect(browserTabIds("session-a")).toHaveLength(2);
  });

  it("selects only open tabs for the agent", () => {
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    act(() => result.current.add());
    const userTab = result.current.selected!;
    act(() => result.current.select(null));
    expect(selectAgentBrowserTab("session-a", "missing")).toBe(false);
    act(() => {
      expect(selectAgentBrowserTab("session-a", userTab)).toBe(true);
    });
    expect(result.current.selected).toBe(userTab);
    expect(agentBrowserTabId("session-a")).toBe(userTab);
  });

  it("opens in-app links in the reserved agent browser, not the agent's current tab", async () => {
    const browserOpenOrNavigate = vi.fn().mockResolvedValue({ ok: true });
    Object.assign(window, { omnigentDesktop: { kind: "electron", browserOpenOrNavigate } });
    writeOpenLinksInApp(true);
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    act(() => {
      addBrowserTab("session-a", { forAgent: true });
    });
    expect(maybeOpenLinkInApp("session-a", "https://example.com/")).toBe(true);
    await vi.waitFor(() => expect(result.current.selected).toBe(AGENT_BROWSER_TAB_ID));
    expect(browserOpenOrNavigate).toHaveBeenCalledWith(
      "session-a",
      "https://example.com/",
      undefined,
      {
        agent: true,
      },
    );
  });

  it("moves the agent to the shown tab when its current tab closes", async () => {
    Object.assign(window, {
      omnigentDesktop: { browserClose: vi.fn().mockResolvedValue({ ok: true }) },
    });
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    act(() => result.current.add());
    const first = result.current.selected!;
    let agentTab = "";
    act(() => {
      agentTab = addBrowserTab("session-a", { forAgent: true });
    });
    await act(() => closeBrowserTab("session-a", agentTab));
    expect(result.current.tabs).toEqual([first]);
    expect(agentBrowserTabId("session-a")).toBe(first);
    await act(() => closeBrowserTab("session-a", first));
    expect(agentBrowserTabId("session-a")).toBe(AGENT_BROWSER_TAB_ID);
    expect(readSessionWorkspaceState("session-a").agentBrowserId).toBeNull();
  });
});
