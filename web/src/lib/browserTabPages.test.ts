import { afterEach, describe, expect, it, vi } from "vitest";
import { readSavedBrowserPage, saveBrowserPage, startSavingBrowserPages } from "./browserTabPages";
import { AGENT_BROWSER_TAB_ID, browserViewId, parseBrowserViewId } from "./browserTabs";
import {
  readSessionWorkspaceState,
  SESSION_WORKSPACE_STATE_KEY,
  writeSessionWorkspaceState,
} from "./sessionWorkspaceState";

afterEach(() => {
  localStorage.clear();
  Reflect.deleteProperty(window, "omnigentDesktop");
});

describe("parseBrowserViewId", () => {
  it("inverts browserViewId for the agent view and user tabs", () => {
    for (const [conversationId, tabId] of [
      ["conv_a", AGENT_BROWSER_TAB_ID],
      ["conv_a", "tab-1"],
      ["session:one/two", "tab-2"],
    ]) {
      expect(parseBrowserViewId(browserViewId(conversationId, tabId))).toEqual({
        conversationId,
        tabId,
      });
    }
  });

  it("treats a malformed tab key as its own agent view", () => {
    for (const viewId of ["browser-tab:conv_a", "browser-tab:conv_a:", "browser-tab:%ZZ:tab"]) {
      expect(parseBrowserViewId(viewId)).toEqual({
        conversationId: viewId,
        tabId: AGENT_BROWSER_TAB_ID,
      });
    }
  });
});

describe("saved browser pages", () => {
  const userTab = browserViewId("conv_a", "tab-1");

  it("saves the page of open tabs only, including the agent tab", () => {
    writeSessionWorkspaceState("conv_a", { openBrowsers: [AGENT_BROWSER_TAB_ID, "tab-1"] });
    saveBrowserPage(userTab, "https://docs.example/guide", "Guide");
    saveBrowserPage("conv_a", "https://agent.example/");
    saveBrowserPage(browserViewId("conv_a", "closed"), "https://closed.example/");

    expect(readSavedBrowserPage(userTab)).toEqual({
      url: "https://docs.example/guide",
      title: "Guide",
    });
    expect(readSavedBrowserPage("conv_a")).toEqual({ url: "https://agent.example/" });
    expect(readSavedBrowserPage(browserViewId("conv_a", "closed"))).toBeNull();
  });

  it("keeps the last page that can be reopened", () => {
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-1"] });
    saveBrowserPage(userTab, "https://docs.example/", "Docs");
    for (const url of ["about:blank", "file:///etc/hosts", "data:text/html,x", "not a url", ""]) {
      saveBrowserPage(userTab, url, "Other");
    }
    saveBrowserPage(userTab, `https://docs.example/${"x".repeat(9000)}`);

    expect(readSavedBrowserPage(userTab)).toEqual({ url: "https://docs.example/", title: "Docs" });
  });

  it("forgets a tab's page when the tab closes", () => {
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-1", "tab-2"] });
    saveBrowserPage(userTab, "https://docs.example/");
    saveBrowserPage(browserViewId("conv_a", "tab-2"), "https://other.example/");
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-2"] });

    expect(readSessionWorkspaceState("conv_a").browserPages).toEqual({
      "tab-2": { url: "https://other.example/" },
    });
  });

  it("drops stored pages that cannot be reopened", () => {
    localStorage.setItem(
      SESSION_WORKSPACE_STATE_KEY,
      JSON.stringify([
        {
          id: "conv_a",
          state: {
            openBrowsers: ["tab-1", "tab-2", "tab-3"],
            browserPages: {
              "tab-1": { url: "https://docs.example/", title: 7 },
              "tab-2": { url: "javascript:alert(1)" },
              "tab-3": "https://bare.example/",
            },
          },
        },
      ]),
    );

    expect(readSessionWorkspaceState("conv_a").browserPages).toEqual({
      "tab-1": { url: "https://docs.example/" },
    });
  });

  it("saves navigations from the shell's URL event until stopped", () => {
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-1"] });
    let listener:
      ((change: { conversationId: string; url: string; title?: string }) => void) | null = null;
    const unsubscribe = vi.fn();
    Object.assign(window, {
      omnigentDesktop: {
        onBrowserUrlChanged: (callback: typeof listener) => {
          listener = callback;
          return unsubscribe;
        },
      },
    });

    const stop = startSavingBrowserPages();
    listener!({ conversationId: userTab, url: "https://docs.example/", title: "Docs" });
    expect(readSavedBrowserPage(userTab)).toEqual({ url: "https://docs.example/", title: "Docs" });
    stop();
    expect(unsubscribe).toHaveBeenCalledOnce();
  });

  it("is a no-op on a shell without the URL event", () => {
    Object.assign(window, { omnigentDesktop: {} });
    expect(() => startSavingBrowserPages()()).not.toThrow();
  });
});
