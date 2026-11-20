import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSIONS = "./workspaceTabs/workspaceTabsSessions";
const toastError = vi.hoisted(() => vi.fn());
vi.mock("sonner", () => ({ toast: { error: toastError } }));

/**
 * The tabs code loads, or its chunk can't be fetched, as after an update replaced it. Each case
 * mocks the module once before importing it: two queued mocks of one path can apply out of order.
 */
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
