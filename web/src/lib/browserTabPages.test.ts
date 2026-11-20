import { afterEach, describe, expect, it, vi } from "vitest";
import { mainBrowserTabs } from "@/components/BrowserPane/agentTools/tabStore";
import { readSavedBrowserPage, saveBrowserPage, startSavingBrowserPages } from "./browserTabPages";
import { AGENT_BROWSER_TAB_ID, browserViewId, parseBrowserViewId } from "./browserTabs";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "./sessionWorkspaceState";

const identity = vi.hoisted(() => ({ userId: "user-a" as string | null }));
vi.mock("./identity", () => ({ getCurrentUserId: () => identity.userId }));
vi.mock("@/components/BrowserPane/agentTools/tabStore", () => ({
  mainBrowserTabs: vi.fn(() => null),
}));

afterEach(() => {
  localStorage.clear();
  identity.userId = "user-a";
  vi.mocked(mainBrowserTabs).mockReturnValue(null);
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

  it("saves a tab main holds before its tab list is stored, and keeps it on other writes", () => {
    vi.mocked(mainBrowserTabs).mockReturnValue({
      version: 1,
      tabs: ["tab-1"],
      selected: "tab-1",
      agentTab: null,
      navs: {},
    });
    saveBrowserPage(userTab, "https://docs.example/");
    writeSessionWorkspaceState("conv_a", { widthPx: 400 });
    expect(readSavedBrowserPage(userTab)).toEqual({ url: "https://docs.example/" });

    writeSessionWorkspaceState("conv_a", { openBrowsers: [] });
    expect(readSavedBrowserPage(userTab)).toBeNull();
  });

  it("shows a page only to the account that visited it", () => {
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-1"] });
    saveBrowserPage(userTab, "https://docs.example/private", "Private");

    identity.userId = "user-b";
    expect(readSavedBrowserPage(userTab)).toBeNull();
    identity.userId = null;
    expect(readSavedBrowserPage(userTab)).toBeNull();
    saveBrowserPage(userTab, "https://docs.example/unowned");

    identity.userId = "user-a";
    expect(readSavedBrowserPage(userTab)).toEqual({
      url: "https://docs.example/private",
      title: "Private",
    });
    expect(readSavedBrowserPage(userTab, "user-b")).toBeNull();
  });

  it("never saves an address that carries a credential", () => {
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-1"] });
    saveBrowserPage(
      userTab,
      "https://docs.example/?q=release&page=2&author=jane&keyword=notes",
      "Search",
    );
    for (const url of [
      "https://app.example/callback?code=abc&state=xyz",
      "https://app.example/#access_token=abc&token_type=bearer",
      "https://files.example/report.pdf?X-Amz-Signature=abc",
      "https://app.example/login?API_KEY=abc",
      "https://user:secret@app.example/",
      "https://app.example/#/callback?code=abc&state=xyz",
      "https://app.example/#/settings?Key=abc",
      "https://app.example/#route=home?sig=abc",
      "https://app.example/return?auth_code=abc",
      "https://app.example/return?accessCode=abc",
      "https://app.example/return?oauth_verifier=abc",
      "https://app.example/login?pwd=abc",
      "https://files.example/report?authkey=abc",
      "https://files.example/report#authkey=abc",
      "https://files.example/report#/route?authkey=abc",
      "https://app.example/verify?passcode=abc",
      "https://app.example/export?PrivateKey=abc",
    ]) {
      saveBrowserPage(userTab, url);
    }

    expect(readSavedBrowserPage(userTab)).toEqual({
      url: "https://docs.example/?q=release&page=2&author=jane&keyword=notes",
      title: "Search",
    });
  });

  it("keeps a page's title when a navigation event to it has none", () => {
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-1"] });
    saveBrowserPage(userTab, "https://docs.example/", "Docs");
    saveBrowserPage(userTab, "https://docs.example/", "");
    expect(readSavedBrowserPage(userTab)).toEqual({ url: "https://docs.example/", title: "Docs" });

    saveBrowserPage(userTab, "https://docs.example/next");
    expect(readSavedBrowserPage(userTab)).toEqual({ url: "https://docs.example/next" });
  });

  it("keeps the last page that can be reopened", () => {
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-1"] });
    saveBrowserPage(userTab, "https://docs.example/", "Docs");
    for (const url of ["about:blank", "file:///etc/hosts", "data:text/html,x", "not a url", ""]) {
      saveBrowserPage(userTab, url, "Other");
    }
    const atLimit = `https://docs.example/${"x".repeat(8192 - "https://docs.example/".length)}`;
    saveBrowserPage(userTab, `${atLimit}y`);
    expect(readSavedBrowserPage(userTab)).toEqual({ url: "https://docs.example/", title: "Docs" });

    saveBrowserPage(userTab, atLimit, "t".repeat(600));
    expect(readSavedBrowserPage(userTab)).toEqual({ url: atLimit, title: "t".repeat(512) });
  });

  it("forgets a tab's page when the tab closes", () => {
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-1", "tab-2"] });
    saveBrowserPage(userTab, "https://docs.example/");
    saveBrowserPage(browserViewId("conv_a", "tab-2"), "https://other.example/");
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-2"] });

    expect(readSessionWorkspaceState("conv_a").browserPages).toEqual({
      "tab-2": { url: "https://other.example/", owner: "user-a" },
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
