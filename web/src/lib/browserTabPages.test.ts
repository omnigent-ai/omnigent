import { afterEach, describe, expect, it, vi } from "vitest";
import {
  readBrowserTabPage,
  recordBrowserTabPage,
  startBrowserTabPagePersistence,
} from "./browserTabPages";
import { AGENT_BROWSER_TAB_ID, browserViewId, parseBrowserViewId } from "./browserTabs";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "./sessionWorkspaceState";

afterEach(() => {
  localStorage.clear();
  Reflect.deleteProperty(window, "omnigentDesktop");
});

describe("parseBrowserViewId", () => {
  it("inverts browserViewId for agent views and user tabs", () => {
    for (const [conversationId, tabId] of [
      ["conv_A", AGENT_BROWSER_TAB_ID],
      ["conv_A", "tab-1"],
      ["session:one/two", "tab-2"],
    ]) {
      expect(parseBrowserViewId(browserViewId(conversationId, tabId))).toEqual({
        conversationId,
        tabId,
      });
    }
  });

  it("treats malformed tab keys as their own agent view", () => {
    for (const viewId of ["browser-tab:conv_A", "browser-tab:conv_A:", "browser-tab:%ZZ:tab"]) {
      expect(parseBrowserViewId(viewId)).toEqual({
        conversationId: viewId,
        tabId: AGENT_BROWSER_TAB_ID,
      });
    }
  });
});

describe("browser tab pages", () => {
  const userTab = browserViewId("conv_A", "tab-1");

  it("records the page of open tabs only", () => {
    writeSessionWorkspaceState("conv_A", { openBrowsers: [AGENT_BROWSER_TAB_ID, "tab-1"] });
    recordBrowserTabPage(userTab, "https://docs.example/guide", "Guide");
    recordBrowserTabPage("conv_A", "https://agent.example/");
    recordBrowserTabPage(browserViewId("conv_A", "closed"), "https://closed.example/");
    expect(readBrowserTabPage(userTab)).toEqual({
      url: "https://docs.example/guide",
      title: "Guide",
    });
    expect(readBrowserTabPage("conv_A")).toEqual({ url: "https://agent.example/" });
    expect(readBrowserTabPage(browserViewId("conv_A", "closed"))).toBeNull();
  });

  it("ignores pages that cannot be reopened", () => {
    writeSessionWorkspaceState("conv_A", { openBrowsers: ["tab-1"] });
    recordBrowserTabPage(userTab, "https://docs.example/", "Docs");
    for (const url of ["about:blank", "file:///etc/passwd", "data:text/html,x", "not a url"]) {
      recordBrowserTabPage(userTab, url, "Other");
    }
    expect(readBrowserTabPage(userTab)).toEqual({ url: "https://docs.example/", title: "Docs" });
  });

  it("forgets a page when its tab closes", () => {
    writeSessionWorkspaceState("conv_A", { openBrowsers: ["tab-1", "tab-2"] });
    recordBrowserTabPage(userTab, "https://docs.example/");
    recordBrowserTabPage(browserViewId("conv_A", "tab-2"), "https://other.example/");
    writeSessionWorkspaceState("conv_A", { openBrowsers: ["tab-2"] });
    expect(readSessionWorkspaceState("conv_A").browserTabPages).toEqual({
      "tab-2": { url: "https://other.example/" },
    });
  });

  it("records navigations from the shell's URL event and stops on unsubscribe", () => {
    writeSessionWorkspaceState("conv_A", { openBrowsers: ["tab-1"] });
    let listener: ((page: { conversationId: string; url: string; title?: string }) => void) | null =
      null;
    const unsubscribe = vi.fn();
    Object.assign(window, {
      omnigentDesktop: {
        onBrowserUrlChanged: (callback: typeof listener) => {
          listener = callback;
          return unsubscribe;
        },
      },
    });
    const stop = startBrowserTabPagePersistence();
    listener!({ conversationId: userTab, url: "https://docs.example/", title: "Docs" });
    expect(readBrowserTabPage(userTab)).toEqual({ url: "https://docs.example/", title: "Docs" });
    stop();
    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });

  it("is a no-op on a shell without the URL event", () => {
    Object.assign(window, { omnigentDesktop: {} });
    expect(() => startBrowserTabPagePersistence()()).not.toThrow();
  });
});
