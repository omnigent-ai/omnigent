import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSIONS = "./workspaceTabs/workspaceTabsSessions";
const toastError = vi.hoisted(() => vi.fn());
vi.mock("sonner", () => ({ toast: { error: toastError } }));

/** Mocks the tabs chunk loading or failing, once per case: queued mocks of one path can reorder. */
function tabsCodeLoads(loads: boolean) {
  vi.doMock(SESSIONS, () => {
    if (!loads) throw new Error("Failed to fetch dynamically imported module");
    return { openWorkspacePage: vi.fn(async () => ({ ok: true })) };
  });
}

/** A fresh routing module, so each case starts with nothing loaded. */
async function routing() {
  return import("./workspaceTabsRouting");
}

beforeEach(() => {
  vi.resetModules();
  toastError.mockClear();
  vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => vi.restoreAllMocks());

describe("routeToWorkspaceTabs", () => {
  it("says why when the tabs refuse an open", async () => {
    tabsCodeLoads(true);
    const { routeToWorkspaceTabs } = await routing();
    await routeToWorkspaceTabs(async () => ({ ok: false, reason: "That file is gone." }));
    expect(toastError).toHaveBeenCalledWith("That file is gone.");
  });

  it("says it couldn't open when the tabs code doesn't load, and loads it again next time", async () => {
    tabsCodeLoads(false);
    const { routeToWorkspaceTabs } = await routing();
    const open = vi.fn(async () => ({ ok: true as const }));

    await expect(routeToWorkspaceTabs(open)).resolves.toBeUndefined();
    expect(open.mock.calls.length).toBe(0);
    expect(toastError).toHaveBeenCalledWith(
      "Couldn't open this in the workspace sidebar. Try again.",
    );

    tabsCodeLoads(true);
    await routeToWorkspaceTabs(open);
    expect(open.mock.calls.length).toBe(1);
  });

  it("says it couldn't open when opening throws", async () => {
    tabsCodeLoads(true);
    const { routeToWorkspaceTabs } = await routing();
    await expect(
      routeToWorkspaceTabs(async () => {
        throw new Error("store gone");
      }),
    ).resolves.toBeUndefined();
    expect(toastError).toHaveBeenCalledWith(
      "Couldn't open this in the workspace sidebar. Try again.",
    );
  });
});

describe("showWorkspaceTabOrChooser", () => {
  /** The session's tabs, two shells and a file, with `selected` selected. */
  function sessionTabs(selected: string) {
    const records = [
      { id: "shell-a", binding: { kind: "terminal" } },
      { id: "file" },
      { id: "shell-b", binding: { kind: "terminal" } },
    ];
    const store = {
      tabs: () => records,
      selectedTab: () => records.find((tab) => tab.id === selected),
      select: vi.fn(() => true),
    };
    const openWorkspaceNewTab = vi.fn(async () => ({ ok: true }));
    vi.doMock(SESSIONS, () => ({ workspaceTabsSession: () => ({ store }), openWorkspaceNewTab }));
    return { store, openWorkspaceNewTab };
  }

  it("keeps the selected tab when it is of that kind, else shows the last one", async () => {
    const selectedShell = sessionTabs("shell-a");
    await (await routing()).showWorkspaceTabOrChooser("s", "terminal");
    expect(selectedShell.store.select).toHaveBeenCalledWith("shell-a");

    vi.resetModules();
    const fromFile = sessionTabs("file");
    await (await routing()).showWorkspaceTabOrChooser("s", "terminal");
    expect(fromFile.store.select).toHaveBeenCalledWith("shell-b");
    expect(fromFile.openWorkspaceNewTab).not.toHaveBeenCalled();
  });

  it("opens a New tab when the session has no tab of that kind", async () => {
    const { store, openWorkspaceNewTab } = sessionTabs("file");
    await (await routing()).showWorkspaceTabOrChooser("s", "chat");
    expect(store.select).not.toHaveBeenCalled();
    expect(openWorkspaceNewTab).toHaveBeenCalledWith("s");
  });
});

describe("showAgentBrowserTab", () => {
  /** The session's tabs code, opening `opens` (null when the strip is full), and its browser tabs. */
  async function agentTabs({ opens = "tab-opened" as string | null, browserTabs = ["t1"] } = {}) {
    const openWorkspaceBrowserView = vi.fn(
      (_sessionId: string, _viewKey: string, _options: { background: boolean }) => opens,
    );
    vi.doMock(SESSIONS, () => ({ openWorkspaceBrowserView }));
    vi.doMock("@/hooks/useBrowserTabs", () => ({
      readBrowserTabsState: () => ({ tabs: browserTabs }),
    }));
    const { showAgentBrowserTab } = await routing();
    const { emitBrowserActionSettled } = await import("@/lib/browserActionBus");
    /** This window's relay reporting how `actionId`, run for `sessionId`, ended. */
    const settle = (sessionId: string, actionId: string, outcome: object) =>
      emitBrowserActionSettled(
        { type: "browser_action_request", actionId, action: "tabs", args: {} },
        sessionId,
        { ok: false, ...outcome },
      );
    const shown = async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
      return openWorkspaceBrowserView.mock.calls.map(([sessionId, viewKey]) => [
        sessionId,
        viewKey,
      ]);
    };
    return { showAgentBrowserTab, settle, shown };
  }

  it("shows a selected tab the session has, and nothing for one it doesn't", async () => {
    const { showAgentBrowserTab, shown } = await agentTabs();
    showAgentBrowserTab("s", { actionId: "a1", action: "select", tabId: "t1" });
    showAgentBrowserTab("s", { actionId: "a2", action: "select", tabId: "gone" });
    showAgentBrowserTab("s", { actionId: "a3", action: "select", tabId: null });
    expect(await shown()).toEqual([["s", "t1"]]);
  });

  it("shows a new tab once only its latest request succeeds, for that session alone", async () => {
    const { showAgentBrowserTab, settle, shown } = await agentTabs();
    showAgentBrowserTab("s", { actionId: "old", action: "new" });
    showAgentBrowserTab("s", { actionId: "latest", action: "new" });
    showAgentBrowserTab("other", { actionId: "theirs", action: "new" });

    // A replaced request, a failure and a success without a tab id show nothing.
    settle("s", "old", { ok: true, data: { tab_id: "t-old" } });
    settle("other", "theirs", { ok: false, error: "no browser" });
    settle("s", "latest", { ok: true, data: {} });
    expect(await shown()).toEqual([]);

    showAgentBrowserTab("s", { actionId: "next", action: "new" });
    settle("s", "next", { ok: true, data: { tab_id: "t-next" } });
    settle("s", "next", { ok: true, data: { tab_id: "t-again" } });
    expect(await shown()).toEqual([["s", "t-next"]]);
  });

  it("says the strip is full when the tab can't open", async () => {
    const { showAgentBrowserTab } = await agentTabs({ opens: null });
    showAgentBrowserTab("s", { actionId: "a1", action: "select", tabId: "t1" });
    await vi.waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Close some workspace tabs to open more."),
    );
  });
});
