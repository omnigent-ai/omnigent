import { afterEach, describe, expect, it, vi } from "vitest";
import { mainBrowserTabs } from "@/components/BrowserPane/agentTools/tabStore";
import { readSavedBrowserPage, saveBrowserPage, startSavingBrowserPages } from "./browserTabPages";
import { AGENT_BROWSER_TAB_ID, browserViewId, parseBrowserViewId } from "./browserTabs";
import {
  readSessionWorkspaceState,
  SESSION_WORKSPACE_STATE_KEY,
  writeSessionWorkspaceState,
} from "./sessionWorkspaceState";

const identity = vi.hoisted(() => ({
  userId: "user-a" as string | null,
  listeners: new Set<() => void>(),
}));
vi.mock("./identity", () => ({
  getCurrentUserId: () => identity.userId,
  resolveIdentity: async () => identity.userId,
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.add(listener);
    return () => identity.listeners.delete(listener);
  },
}));
vi.mock("@/components/BrowserPane/agentTools/tabStore", () => ({
  mainBrowserTabs: vi.fn(() => null),
}));

afterEach(() => {
  localStorage.clear();
  identity.userId = "user-a";
  identity.listeners.clear();
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

  it("rejects recognized URL credentials without replacing the previous page", () => {
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-1"] });
    const ordinaryParams = "country_code=US&sort_key=date&countryCode=GB&sortKey=name";
    for (const url of [
      "https://docs.example/?q=release&page=2&author=jane&keyword=notes",
      `https://docs.example/?${ordinaryParams}`,
      `https://docs.example/#${ordinaryParams}`,
      `https://docs.example/#/guide?${ordinaryParams}`,
      "https://docs.example/reports;country_code=US;sort_key=date/summary",
      "https://docs.example/#/reports;country_code=US;sort_key=date/summary",
      "https://docs.example/50%off",
      "https://docs.example/#100%",
      "https://docs.example/%FF/guide",
      "https://docs.example/#/%FF;country_code=US",
    ]) {
      saveBrowserPage(userTab, url, "Search");
      expect(readSavedBrowserPage(userTab), url).toEqual({ url, title: "Search" });
    }
    const previousPage = readSavedBrowserPage(userTab);
    const previousStorage = localStorage.getItem(SESSION_WORKSPACE_STATE_KEY);
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
      "https://app.example/return?authorizationCode=synthetic",
      "https://app.example/return?oauth_code=synthetic",
      "https://app.example/return?oauth_verifier=abc",
      "https://app.example/login?pwd=abc",
      "https://files.example/report?authkey=abc",
      "https://files.example/report#authkey=abc",
      "https://files.example/report#/route?authkey=abc",
      "https://app.example/verify?passcode=abc",
      "https://app.example/export?PrivateKey=abc",
      "https://app.example/consume?SAMLart=abc",
      "https://app.example/#samlart=abc",
      "https://app.example/#/consume?SAMLart=abc",
      ...["PHPSESSID", "phpsessid", "JSESSIONID", "ASP.NET_SessionId", "sid", "SID"].flatMap(
        (name) => [
          `https://app.example/?${name}=synthetic`,
          `https://app.example/#${name}=synthetic`,
          `https://app.example/#/callback?${name}=synthetic`,
          `https://app.example/report;${name}=synthetic`,
          `https://app.example/#/report;${name}=synthetic`,
        ],
      ),
      "https://app.example/reports;lang=en;jsessionid=synthetic/details",
      "https://app.example/report;%6Asessionid=synthetic",
      "https://app.example/report%3Bjsessionid%3Dsynthetic",
      "https://app.example/report;code=synthetic",
      "https://app.example/#/callback;code=synthetic",
      "https://app.example/#/callback%3Bcode%3Dsynthetic",
      "https://app.example/#/callback;lang=en;code=synthetic",
      "https://app.example/?lang=en;sid=synthetic",
      "https://app.example/#/callback?lang=en;sid=synthetic",
      "https://app.example/50%off%3Bsid%3Dsynthetic",
      "https://app.example/#100%%3Bcode%3Dsynthetic",
      "https://app.example/report%FF%3Bsid%3Dsynthetic",
    ]) {
      saveBrowserPage(userTab, url);
      expect(readSavedBrowserPage(userTab), url).toEqual(previousPage);
      expect(localStorage.getItem(SESSION_WORKSPACE_STATE_KEY), url).toBe(previousStorage);
    }
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

  it.each([
    "ready",
    "recovered",
    "rejected",
    "rejected after identity",
    "newer",
    "closed",
    "replaced",
    "tab removed",
    "stopped",
  ])("saves navigations from the shell's URL event with identity %s", async (scenario) => {
    writeSessionWorkspaceState("conv_a", { openBrowsers: ["tab-1"] });
    saveBrowserPage(userTab, "https://docs.example/previous");
    if (scenario !== "ready") identity.userId = null;
    let listener:
      ((change: { conversationId: string; url: string; title?: string }) => void) | null = null;
    let closed!: (view: { conversationId: string }) => void;
    const unsubscribe = vi.fn();
    const unsubscribeClosed = vi.fn();
    Object.assign(window, {
      omnigentDesktop: {
        onBrowserUrlChanged: (callback: typeof listener) => {
          listener = callback;
          return unsubscribe;
        },
        onBrowserViewClosed: (callback: typeof closed) => {
          closed = callback;
          return unsubscribeClosed;
        },
      },
    });

    const stop = startSavingBrowserPages();
    listener!({ conversationId: userTab, url: "https://docs.example/", title: "Docs" });
    listener!({ conversationId: userTab, url: "https://docs.example/", title: "" });
    if (scenario === "rejected after identity") identity.userId = "user-a";
    if (scenario.startsWith("rejected")) {
      for (const url of ["https://app.example/?sid=synthetic", "about:blank"]) {
        listener!({ conversationId: userTab, url, title: "Rejected" });
      }
    }
    await Promise.resolve();
    if (scenario !== "ready" && scenario !== "rejected after identity") {
      expect(readSavedBrowserPage(userTab, "user-a")).toEqual({
        url: "https://docs.example/previous",
      });
    }
    if (scenario === "closed" || scenario === "replaced") closed({ conversationId: userTab });
    if (scenario === "tab removed") writeSessionWorkspaceState("conv_a", { openBrowsers: [] });
    if (scenario === "stopped") stop();
    if (scenario === "newer") identity.userId = "user-a";
    if (scenario === "newer" || scenario === "replaced" || scenario === "stopped") {
      listener!({ conversationId: userTab, url: "https://docs.example/newer", title: "Newer" });
    }
    identity.userId = "user-a";
    for (const onIdentity of identity.listeners) onIdentity();
    expect(readSavedBrowserPage(userTab)).toEqual(
      scenario === "tab removed"
        ? null
        : scenario === "closed" || scenario === "stopped"
          ? { url: "https://docs.example/previous" }
          : scenario === "newer" || scenario === "replaced"
            ? { url: "https://docs.example/newer", title: "Newer" }
            : { url: "https://docs.example/", title: "Docs" },
    );
    if (scenario !== "stopped") stop();
    expect(identity.listeners.size).toBe(0);
    expect(unsubscribe).toHaveBeenCalledOnce();
    expect(unsubscribeClosed).toHaveBeenCalledOnce();
  });

  it("is a no-op on a shell without the URL event", () => {
    Object.assign(window, { omnigentDesktop: {} });
    expect(() => startSavingBrowserPages()()).not.toThrow();
  });
});
