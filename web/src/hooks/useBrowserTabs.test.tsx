import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { emitBrowserActionRequest } from "@/lib/browserActionBus";
import { writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import { maybeOpenLinkInApp } from "@/lib/openLinkInApp";
import { readSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import {
  AGENT_BROWSER_TAB_ID,
  browserViewId,
  browserViewOwnerId,
  openAgentBrowserTab,
  openBrowserTab,
  useBrowserTabs,
} from "./useBrowserTabs";

afterEach(() => {
  cleanup();
  localStorage.clear();
  Reflect.deleteProperty(window, "omnigentDesktop");
});

describe("browser tab titles", () => {
  interface Page {
    url?: string;
    title?: string;
  }
  type Probe = Page & { exists: boolean; pendingUrl?: string };
  type Listener = (page: Page & { conversationId: string }) => void;

  it.each<[string, Probe | undefined, string]>([
    ["page title", { exists: true, url: "https://docs.example/guide", title: " Guide " }, "Guide"],
    [
      "older shell without titles",
      { exists: true, url: "https://docs.example/guide" },
      "docs.example",
    ],
    [
      "URL as title",
      { exists: true, url: "https://docs.example/guide", title: "https://docs.example/guide" },
      "docs.example",
    ],
    [
      "scheme-free URL as title",
      { exists: true, url: "https://docs.example/guide", title: "docs.example/guide" },
      "docs.example",
    ],
    [
      "initial navigation",
      { exists: true, url: "about:blank", pendingUrl: "https://docs.example/guide", title: "" },
      "docs.example",
    ],
    ["blank page", { exists: true, url: "about:blank", title: "" }, "Browser 2"],
    ["blank URL as title", { exists: true, url: "about:blank", title: "about:blank" }, "Browser 2"],
    ["older shell without metadata methods", undefined, "Browser 2"],
  ])("restores the label for %s", async (_scenario, page, label) => {
    openBrowserTab("session-a", "known");
    Object.assign(window, {
      omnigentDesktop: page ? { browserHasView: vi.fn().mockResolvedValue(page) } : {},
    });
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    await waitFor(() => expect(result.current.labelFor("known", 1)).toBe(label));
  });

  it("updates a background title without changing selection or accepting an older probe", async () => {
    openBrowserTab("session-a", "background");
    openBrowserTab("session-a", "selected");
    let finishProbe!: (page: Probe) => void;
    const probe = new Promise<Probe>((resolve) => {
      finishProbe = resolve;
    });
    const browserHasView = vi.fn().mockReturnValue(probe);
    const onBrowserUrlChanged = vi
      .fn<(listener: Listener) => () => void>()
      .mockImplementation(() => vi.fn());
    Object.assign(window, {
      omnigentDesktop: { browserHasView, onBrowserUrlChanged },
    });
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    const update = onBrowserUrlChanged.mock.calls.at(-1)![0];
    const background = {
      conversationId: browserViewId("session-a", "background"),
      url: "https://docs.example/guide",
      title: "Live guide",
    };
    act(() => update(background));
    act(() =>
      update({
        ...background,
        conversationId: browserViewId("session-b", "background"),
        title: "Other session",
      }),
    );
    const subscriptions = onBrowserUrlChanged.mock.calls.length;
    const probes = browserHasView.mock.calls.length;
    act(() => result.current.select("background"));
    act(() => result.current.select("selected"));
    expect(onBrowserUrlChanged).toHaveBeenCalledTimes(subscriptions);
    expect(browserHasView).toHaveBeenCalledTimes(probes);
    await act(async () => {
      finishProbe({ exists: true, url: background.url, title: "Old guide" });
    });
    expect(result.current.labelFor("background", 0)).toBe("Live guide");
    expect(result.current.selected).toBe("selected");

    act(() => update({ ...background, title: "", url: "https://next.example/untitled" }));
    expect(result.current.labelFor("background", 0)).toBe("next.example");
    await act(() => result.current.close("background"));
    act(() => onBrowserUrlChanged.mock.calls.at(-1)![0](background));
    expect(result.current.labelFor("background", 0)).toBe("Browser 1");
    expect(result.current.selected).toBe("selected");
  });

  it("discards stale probes and listeners when switching sessions or unmounting", async () => {
    openBrowserTab("session-a", "shared-tab-id");
    openBrowserTab("session-b", "shared-tab-id");
    const finish = new Map<string, (page: Probe) => void>();
    const onBrowserUrlChanged = vi
      .fn<(listener: Listener) => () => void>()
      .mockImplementation(() => vi.fn());
    Object.assign(window, {
      omnigentDesktop: {
        browserHasView: (viewId: string) =>
          new Promise<Probe>((resolve) => {
            finish.set(viewId, resolve);
          }),
        onBrowserUrlChanged,
      },
    });
    const hook = renderHook(({ conversationId }) => useBrowserTabs(conversationId), {
      initialProps: { conversationId: "session-a" },
    });
    const viewA = browserViewId("session-a", "shared-tab-id");
    const viewB = browserViewId("session-b", "shared-tab-id");
    const updateA = onBrowserUrlChanged.mock.calls.at(-1)![0];
    act(() => updateA({ conversationId: viewA, title: "Session A" }));
    expect(hook.result.current.labelFor("shared-tab-id", 0)).toBe("Session A");

    hook.rerender({ conversationId: "session-b" });
    await act(async () => {
      finish.get(viewA)!({ exists: true, title: "Stale A probe" });
      updateA({ conversationId: viewA, title: "Stale A event" });
    });
    expect(hook.result.current.labelFor("shared-tab-id", 0)).toBe("Browser 1");
    const updateB = onBrowserUrlChanged.mock.calls.at(-1)![0];
    act(() => updateB({ conversationId: viewB, title: "Session B" }));
    expect(hook.result.current.labelFor("shared-tab-id", 0)).toBe("Session B");

    hook.unmount();
    await act(async () => {
      finish.get(viewB)!({ exists: true, title: "Unmounted probe" });
      updateB({ conversationId: viewB, title: "Unmounted event" });
    });
    for (const subscription of onBrowserUrlChanged.mock.results) {
      expect(subscription.value).toHaveBeenCalledOnce();
    }
  });
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

  it("keeps existing and agent views when accepted links open independent tabs", async () => {
    const browserOpenOrNavigate = vi.fn().mockResolvedValue({ ok: true });
    Object.assign(window, {
      omnigentDesktop: { kind: "electron", browserOpenOrNavigate },
    });
    writeOpenLinksInApp(true);
    openAgentBrowserTab("session-a");
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    act(() => result.current.add());
    const existingTab = result.current.selected!;

    await act(async () => {
      expect(maybeOpenLinkInApp("session-a", "https://example.com/first")).toBe(true);
    });
    const firstLinkTab = result.current.selected!;
    expect(firstLinkTab).toEqual(expect.any(String));
    expect(firstLinkTab).not.toBe(existingTab);
    expect(firstLinkTab).not.toBe(AGENT_BROWSER_TAB_ID);
    expect(result.current.agentBrowser).toBe(false);

    await act(async () => {
      expect(maybeOpenLinkInApp("session-a", "https://example.com/second")).toBe(true);
    });
    const secondLinkTab = result.current.selected!;
    expect(secondLinkTab).toEqual(expect.any(String));
    expect(secondLinkTab).not.toBe(firstLinkTab);
    expect(result.current.tabs).toEqual([
      AGENT_BROWSER_TAB_ID,
      existingTab,
      firstLinkTab,
      secondLinkTab,
    ]);
    expect(browserOpenOrNavigate.mock.calls.map(([viewId]) => viewId)).toEqual([
      browserViewId("session-a", firstLinkTab),
      browserViewId("session-a", secondLinkTab),
    ]);
    expect(readSessionWorkspaceState("session-a").selectedBrowserId).toBe(secondLinkTab);
    expect(openBrowserTab("session-a", secondLinkTab).tabs).toEqual(result.current.tabs);
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

  it("still closes the agent view when a separate link tab opens during its close", async () => {
    let finishClose!: (value: { ok: boolean }) => void;
    Object.assign(window, {
      omnigentDesktop: {
        browserClose: () =>
          new Promise<{ ok: boolean }>((resolve) => {
            finishClose = resolve;
          }),
      },
    });
    openAgentBrowserTab("session-a");
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    const pendingClose = result.current.close(AGENT_BROWSER_TAB_ID);
    act(() => openBrowserTab("session-a", "accepted-link"));
    await act(async () => {
      finishClose({ ok: true });
      await pendingClose;
    });

    expect(result.current.tabs).toEqual(["accepted-link"]);
    expect(result.current.selected).toBe("accepted-link");
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
