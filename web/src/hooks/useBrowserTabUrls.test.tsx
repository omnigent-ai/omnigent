import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AGENT_BROWSER_TAB_ID, browserViewId } from "./useBrowserTabs";
import { useBrowserTabUrls } from "./useBrowserTabUrls";

interface UrlPayload {
  conversationId: string;
  url: string;
}

function installBridge(hasView: (viewId: string) => Promise<{ exists: boolean; url?: string }>) {
  const urlListeners = new Set<(payload: UrlPayload) => void>();
  const closedListeners = new Set<(payload: { conversationId: string }) => void>();
  Object.assign(window, {
    omnigentDesktop: {
      kind: "electron",
      browserOpenOrNavigate: vi.fn(),
      browserHasView: vi.fn(hasView),
      onBrowserUrlChanged: (callback: (payload: UrlPayload) => void) => {
        urlListeners.add(callback);
        return () => urlListeners.delete(callback);
      },
      onBrowserViewClosed: (callback: (payload: { conversationId: string }) => void) => {
        closedListeners.add(callback);
        return () => closedListeners.delete(callback);
      },
    },
  });
  return {
    urlListeners,
    closedListeners,
    navigate: (viewId: string, url: string) =>
      act(() => urlListeners.forEach((listener) => listener({ conversationId: viewId, url }))),
    closeView: (viewId: string) =>
      act(() => closedListeners.forEach((listener) => listener({ conversationId: viewId }))),
  };
}

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(window, "omnigentDesktop");
});

describe("useBrowserTabUrls", () => {
  it("reads existing views, follows navigation, and forgets closed views", async () => {
    const bridge = installBridge((viewId) =>
      Promise.resolve(
        viewId === "session-a"
          ? { exists: true, url: "https://agent.example/" }
          : { exists: false },
      ),
    );
    const { result } = renderHook(() =>
      useBrowserTabUrls("session-a", [AGENT_BROWSER_TAB_ID, "tab-two"]),
    );
    await act(async () => {});
    expect(result.current).toEqual({ [AGENT_BROWSER_TAB_ID]: "https://agent.example/" });

    bridge.navigate(browserViewId("session-a", "tab-two"), "https://two.example/");
    // Views owned by another session never leak into this one.
    bridge.navigate(browserViewId("session-b", "tab-two"), "https://other.example/");
    expect(result.current).toEqual({
      [AGENT_BROWSER_TAB_ID]: "https://agent.example/",
      "tab-two": "https://two.example/",
    });

    bridge.closeView("session-a");
    expect(result.current).toEqual({ "tab-two": "https://two.example/" });
  });

  it("forgets the URL of a removed tab before it is reopened", async () => {
    let exists = true;
    installBridge(() =>
      exists ? Promise.resolve({ exists, url: "https://old.example/" }) : new Promise(() => {}),
    );
    const { result, rerender } = renderHook(({ tabs }) => useBrowserTabUrls("session-a", tabs), {
      initialProps: { tabs: [AGENT_BROWSER_TAB_ID] },
    });
    await act(async () => {});
    expect(result.current).toEqual({ [AGENT_BROWSER_TAB_ID]: "https://old.example/" });

    // The view went away without a close event, and the reopened tab's probe never settles.
    exists = false;
    rerender({ tabs: [] });
    rerender({ tabs: [AGENT_BROWSER_TAB_ID] });
    expect(result.current).toEqual({});
  });

  it("keeps a navigation that lands before the existing-view snapshot", async () => {
    let finishProbe!: (value: { exists: boolean; url?: string }) => void;
    const bridge = installBridge(
      () =>
        new Promise((resolve) => {
          finishProbe = resolve;
        }),
    );
    const { result } = renderHook(() => useBrowserTabUrls("session-a", ["tab-one"]));
    bridge.navigate(browserViewId("session-a", "tab-one"), "https://newer.example/");
    await act(async () => finishProbe({ exists: true, url: "https://older.example/" }));
    expect(result.current).toEqual({ "tab-one": "https://newer.example/" });
  });

  it("keeps a close that lands before the existing-view snapshot", async () => {
    let finishProbe!: (value: { exists: boolean; url?: string }) => void;
    const bridge = installBridge(
      () =>
        new Promise((resolve) => {
          finishProbe = resolve;
        }),
    );
    const { result } = renderHook(() => useBrowserTabUrls("session-a", ["tab-one"]));
    bridge.closeView(browserViewId("session-a", "tab-one"));
    await act(async () => finishProbe({ exists: true, url: "https://stale.example/" }));
    expect(result.current).toEqual({});
  });

  it("does nothing outside a browser-capable shell", () => {
    const { result } = renderHook(() => useBrowserTabUrls("session-a", ["tab-one"]));
    expect(result.current).toEqual({});
  });

  it("unsubscribes when unmounted", () => {
    const bridge = installBridge(() => Promise.resolve({ exists: false }));
    const { unmount } = renderHook(() => useBrowserTabUrls("session-a", ["tab-one"]));
    expect(bridge.urlListeners.size).toBe(1);
    expect(bridge.closedListeners.size).toBe(1);
    unmount();
    expect(bridge.urlListeners.size).toBe(0);
    expect(bridge.closedListeners.size).toBe(0);
  });
});
