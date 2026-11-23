import { describe, expect, it, vi } from "vitest";

import { currentVisit } from "./workspaceHistory";
import type { WorkspacePlace } from "./workspacePlace";
import type { WorkspaceTab } from "./workspaceTab";
import {
  MAX_WORKSPACE_TABS,
  parseSessionState,
  type WorkspaceTabsSessionState,
} from "./workspaceTabsPersistence";
import {
  ALREADY_CLOSING_REASON,
  createWorkspaceTabsStore,
  isOnlyEmptyNewTab,
  LAST_NEW_TAB_REASON,
  type WorkspaceTabsStoreOptions,
} from "./workspaceTabsStore";

const files: WorkspaceTab = {
  id: "F",
  place: { kind: "files" },
  role: "files",
  openedBy: "restore",
};
const pulls: WorkspaceTab = { id: "P", place: { kind: "pulls" }, openedBy: "restore" };

function initial(tabs: WorkspaceTab[] = [files, pulls], selected = "F"): WorkspaceTabsSessionState {
  const tab = tabs.find((candidate) => candidate.id === selected)!;
  return {
    tabs,
    selectedTabId: selected,
    timeline: [{ tabId: selected, place: tab.place }],
    cursor: 0,
    closed: [],
  };
}

/** A deferred answer, so a test can act while the guard waits. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function makeStore(overrides: Partial<WorkspaceTabsStoreOptions> = {}) {
  let next = 0;
  let views = 0;
  let drafts = 0;
  const dirty = new Set<string>();
  const save = vi.fn().mockReturnValue("saved");
  const requestLeave = vi.fn().mockResolvedValue("leave");
  const store = createWorkspaceTabsStore({
    scopeId: "s1",
    initial: initial(),
    save,
    requestLeave,
    isTabDirty: (tabId) => dirty.has(tabId),
    newTabId: () => `n${++next}`,
    newViewKey: () => `view${++views}`,
    newDraftKey: () => `pending:${++drafts}`,
    ...overrides,
  });
  return { store, dirty, save, requestLeave };
}

function file(path: string, line?: number): WorkspacePlace {
  return { kind: "files", path, ...(line ? { line: { start: line } } : {}) };
}

/** After every operation the cursor visit belongs to the selected tab and equals its place. */
function expectInvariant(store: ReturnType<typeof makeStore>["store"]) {
  const current = currentVisit(store.history.state);
  const selected = store.selectedTab();
  expect(current?.tabId).toBe(selected?.id);
  expect(current?.place).toEqual(selected?.place);
}

describe("opening", () => {
  it("opens a file tab after the selected tab, selects it and records a step", async () => {
    const { store, save } = makeStore();
    const result = await store.open(
      { place: file("a.ts") },
      { opener: "user", disposition: "new" },
    );
    expect(result).toEqual({ ok: true, tabId: "n1", created: true, selected: true });
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F", "n1", "P"]);
    expect(store.history.serialize().visits).toHaveLength(2);
    expect(save).toHaveBeenCalled();
    expectInvariant(store);
  });

  it("marks a fresh landing for each open and history step, and none for a click on a tab", async () => {
    let choices = 0;
    const { store } = makeStore({ choiceCount: () => choices });
    expect(store.landing("P")).toBeUndefined();
    await store.open({ place: { kind: "pulls" } }, { opener: "user" });
    const opened = store.landing("P");
    expect(opened).toEqual({ settled: false, choices: 0 });
    store.select("F");
    expect(store.landing("F")).toBeUndefined();
    // Each landing keeps the session's choice count as it lands.
    choices = 3;
    await store.go("back");
    expect(store.selectedTabId()).toBe("P");
    expect(store.landing("P")).not.toBe(opened);
    expect(store.landing("P")).toEqual({ settled: false, choices: 3 });
  });

  it("brings a Diffs tab forward without the comparison it showed before, for the session's own", async () => {
    const { store } = makeStore();
    const diffFile: WorkspacePlace = { kind: "changes", path: "a.go", fileTab: true };
    const branch = { kind: "saved", intent: "branch-point" } as const;
    const opened = await store.open(
      { place: { ...diffFile, comparison: branch } },
      { opener: "user", disposition: "new" },
    );
    store.select("F");
    await store.open(
      { place: diffFile },
      { opener: "user", disposition: "focus", reuse: "same-place" },
    );
    expect(store.selectedTab()).toMatchObject({ id: opened.ok && opened.tabId });
    expect(store.selectedTab()?.place).toEqual(diffFile);
    expect(store.landing(store.selectedTabId()!)).toEqual({ settled: false, choices: 0 });
    expectInvariant(store);
  });

  it("reuses the tab that already shows a page", async () => {
    const { store } = makeStore();
    const result = await store.open({ place: { kind: "pulls" } }, { opener: "user" });
    expect(result).toMatchObject({ ok: true, tabId: "P", created: false });
    expect(store.selectedTabId()).toBe("P");
    expectInvariant(store);
  });

  it("binds a new browser tab to a fresh view and a new shell tab to its terminal", async () => {
    const { store } = makeStore();
    await store.open({ place: { kind: "web" } }, { opener: "user", disposition: "new" });
    await store.open({ place: { kind: "terminal", key: "k1" } }, { opener: "user" });
    expect(
      store
        .tabs()
        .slice(1, 3)
        .map((tab) => ("binding" in tab ? tab.binding : null)),
    ).toEqual([
      { kind: "browser-view", viewKey: "view1" },
      { kind: "terminal", key: "k1" },
    ]);
  });

  it("refuses past the tab cap and changes nothing", async () => {
    const many = Array.from({ length: MAX_WORKSPACE_TABS }, (_, index) => ({
      id: `t${index}`,
      place: file(`f${index}.ts`),
      openedBy: "restore" as const,
    }));
    const { store, save } = makeStore({ initial: initial(many, "t0") });
    const result = await store.open({ place: { kind: "pulls" } }, { opener: "user" });
    expect(result).toEqual({ ok: false, reason: "Close some workspace tabs to open more." });
    expect(store.tabs()).toHaveLength(MAX_WORKSPACE_TABS);
    expect(save).not.toHaveBeenCalled();
  });

  it("hands another session's link back without touching this session", async () => {
    const { store } = makeStore();
    const result = await store.open(
      { place: { kind: "pulls" }, session: "s2" },
      { opener: "link" },
    );
    expect(result).toMatchObject({ ok: false, otherSession: { sessionId: "s2" } });
    expect(store.revision()).toBe(0);
  });

  it("lets an agent open in the background without selecting", async () => {
    const { store } = makeStore();
    const result = await store.open({ place: file("a.ts") }, { opener: "agent" });
    expect(result).toMatchObject({ ok: true, selected: false });
    expect(store.selectedTabId()).toBe("F");
    expect(store.findTab("n1")?.unseen).toBe(true);
    expect(store.history.serialize().visits).toHaveLength(1);
    expectInvariant(store);
  });

  it("marks nothing new when an agent opens a restored tab's place, whatever order its fields were saved in", async () => {
    // Saved tabs come back with their fields in the order storage wrote them.
    const restored = {
      id: "C",
      place: { path: "src/a.ts", kind: "changes" } as WorkspacePlace,
      openedBy: "restore",
    } satisfies WorkspaceTab;
    const { store } = makeStore({ initial: initial([files, restored], "F") });
    const result = await store.open(
      { place: { kind: "changes", path: "src/a.ts" } },
      { opener: "agent" },
    );
    expect(result).toMatchObject({ ok: true, tabId: "C", created: false });
    expect(store.findTab("C")?.unseen).toBeUndefined();
  });

  it("asks before an agent moves the tab the user is looking at", async () => {
    const tab: WorkspaceTab = { id: "A", place: file("a.ts"), openedBy: "user" };
    const { store } = makeStore({ initial: initial([files, tab], "A") });
    await store.open({ place: file("b.ts"), tab: "A" }, { opener: "agent" });
    expect(store.findTab("A")).toMatchObject({
      place: file("a.ts"),
      attention: { place: file("b.ts") },
    });
    await store.showAttention("A");
    expect(store.findTab("A")).toMatchObject({ place: file("b.ts") });
    expect(store.findTab("A")?.attention).toBeUndefined();
    expectInvariant(store);
  });
});

describe("the unsaved-changes guard", () => {
  it("leaves tabs, selection and history as they were on Keep editing", async () => {
    const tab: WorkspaceTab = { id: "A", place: file("a.ts"), openedBy: "user" };
    const { store, dirty, requestLeave } = makeStore({ initial: initial([files, tab], "A") });
    dirty.add("A");
    requestLeave.mockResolvedValue("cancel");
    const before = store.serialize();
    await expect(store.navigate("A", file("b.ts"))).resolves.toEqual({
      ok: false,
      cancelled: true,
    });
    await expect(store.close("A")).resolves.toEqual({ ok: false, cancelled: true });
    expect(store.serialize()).toEqual(before);
    expect(requestLeave).toHaveBeenCalledTimes(2);
  });

  it("lets the user's newer action win while the guard waits", async () => {
    const tab: WorkspaceTab = { id: "A", place: file("a.ts"), openedBy: "user" };
    const answer = deferred<"leave" | "cancel">();
    const { store, dirty } = makeStore({
      initial: initial([files, tab, pulls], "A"),
      requestLeave: () => answer.promise,
    });
    dirty.add("A");
    const navigation = store.navigate("A", file("b.ts"));
    store.select("P");
    answer.resolve("leave");
    await expect(navigation).resolves.toEqual({ ok: false, stale: true });
    expect(store.findTab("A")?.place).toEqual(file("a.ts"));
    expect(store.selectedTabId()).toBe("P");
    expectInvariant(store);
  });

  it("never asks to select another tab", () => {
    const tab: WorkspaceTab = { id: "A", place: file("a.ts"), openedBy: "user" };
    const { store, dirty, requestLeave } = makeStore({ initial: initial([files, tab], "A") });
    dirty.add("A");
    store.select("F");
    expect(requestLeave).not.toHaveBeenCalled();
    expectInvariant(store);
  });
});

describe("closing and reopening", () => {
  it("lands on the nearest earlier visit when the selected tab closes", async () => {
    const { store } = makeStore();
    store.select("P");
    await store.open({ place: file("a.ts") }, { opener: "user", disposition: "new" });
    await store.close("n1");
    expect(store.selectedTabId()).toBe("P");
    expectInvariant(store);
  });

  it("selects a neighbour when no visit of another tab is left", async () => {
    const { store } = makeStore();
    await store.close("F");
    expect(store.selectedTabId()).toBe("P");
    expectInvariant(store);
  });

  it("leaves one selected empty New tab, saved, when the last tab closes", async () => {
    const { store, save } = makeStore({ initial: initial([files], "F") });
    await store.close("F");
    expect(store.tabs()).toEqual([{ id: "n1", place: { kind: "new" }, openedBy: "restore" }]);
    expect(store.selectedTabId()).toBe("n1");
    expectInvariant(store);
    expect(save).toHaveBeenLastCalledWith(
      expect.objectContaining({ tabs: store.tabs(), selectedTabId: "n1" }),
    );
  });

  describe("a tab that arrives in an empty sidebar takes its New tab's place and shows", () => {
    const empty = () =>
      makeStore({
        initial: {
          tabs: [],
          selectedTabId: null,
          timeline: [],
          cursor: -1,
          closed: [],
        },
      });

    it("selects a file an agent opens in the background", async () => {
      const { store } = empty();
      const result = await store.open({ place: file("a.ts") }, { opener: "agent" });
      expect(result).toMatchObject({ ok: true, selected: true });
      expect(store.tabs().map((tab) => (tab as WorkspaceTab).place)).toEqual([file("a.ts")]);
      expect(store.history.serialize().visits.map((visit) => visit.place.kind)).toEqual(["files"]);
      expectInvariant(store);
    });

    it("selects a browser view opened without selecting it", () => {
      const { store } = empty();
      const tabId = store.openBrowserView("v1", { select: false, opener: "agent" });
      expect(store.tabs().map((tab) => tab.id)).toEqual([tabId]);
      expect(store.selectedTabId()).toBe(tabId);
      expectInvariant(store);
    });

    it("keeps a New tab the empty one doesn't need to give way to", async () => {
      const { store } = empty();
      await store.open(
        { place: { kind: "new" } },
        { opener: "user", disposition: "new", position: "end" },
      );
      expect(store.tabs().map((tab) => (tab as WorkspaceTab).place)).toEqual([
        { kind: "new" },
        { kind: "new" },
      ]);
      expectInvariant(store);
    });
  });

  it("reopens the last tab closed from an empty sidebar, and records it as a step", async () => {
    const { store } = makeStore({ initial: initial([files], "F") });
    await store.close("F");
    const reopened = store.reopenClosed();
    expect(reopened).not.toBeNull();
    expect(store.selectedTab()?.place).toEqual(files.place);
    expectInvariant(store);
  });

  it("opens a file into an emptied sidebar in its New tab's place", async () => {
    const { store } = makeStore({ initial: initial([files], "F") });
    await store.close("F");
    const result = await store.open({ place: file("a.ts") }, { opener: "user" });
    expect(result).toMatchObject({ ok: true, created: true, selected: true });
    expect(store.tabs().map((tab) => (tab as WorkspaceTab).place)).toEqual([file("a.ts")]);
    expectInvariant(store);
  });

  it("keeps a tab whose resource refused to close", async () => {
    const { store } = makeStore();
    const result = await store.close("P", { confirm: () => Promise.resolve(false) });
    expect(result).toEqual({ ok: false, cancelled: true });
    expect(store.findTab("P")).toBeDefined();
  });

  it("reopens the last closed tab at its position, but never a shell", async () => {
    const shell: WorkspaceTab = {
      id: "T",
      place: { kind: "terminal", key: "k" },
      binding: { kind: "terminal", key: "k" },
      openedBy: "user",
    };
    const { store } = makeStore({ initial: initial([files, pulls, shell], "F") });
    await store.close("P");
    await store.close("T", { confirm: () => Promise.resolve(true) });
    expect(store.snapshot().canReopen).toBe(true);
    const reopened = store.reopenClosed();
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F", reopened]);
    expect(store.findTab(reopened!)?.place).toEqual({ kind: "pulls" });
    expect(store.reopenClosed()).toBeNull();
    expectInvariant(store);
  });

  it("reopens a browser tab as a new view of its last page", async () => {
    const web: WorkspaceTab = {
      id: "W",
      place: { kind: "web", url: "https://example.com/" },
      binding: { kind: "browser-view", viewKey: "old" },
      openedBy: "user",
    };
    const { store } = makeStore({ initial: initial([files, web], "F") });
    await store.close("W");
    const reopened = store.findTab(store.reopenClosed()!);
    expect(reopened?.place).toEqual({ kind: "web", url: "https://example.com/" });
    expect(reopened?.binding).not.toEqual({ kind: "browser-view", viewKey: "old" });
  });
});

/** A shell's tab the user opened, `id` showing shell `key`. */
const userShellTab = (id: string, key: string): WorkspaceTab => ({
  id,
  place: { kind: "terminal", key },
  binding: { kind: "terminal", key },
  openedBy: "user",
});

describe("shells", () => {
  const shell: WorkspaceTab = {
    id: "T",
    place: { kind: "terminal", key: "k" },
    binding: { kind: "terminal", key: "k" },
    openedBy: "user",
  };

  it("shows a just-opened shell as connecting until its terminal appears", async () => {
    const { store } = makeStore();
    const opened = await store.open({ place: { kind: "terminal", key: "k" } }, { opener: "user" });
    const tabId = opened.ok ? opened.tabId : "";
    store.setTerminalInventory({ status: "loaded", keys: [] });
    expect(store.resourceState(tabId)).toBe("unknown");
    store.setTerminalInventory({ status: "loaded", keys: ["k"] });
    expect(store.resourceState(tabId)).toBe("live");
  });

  it("drops a shell's tab once a loaded list no longer has it, and refuses to reopen it", async () => {
    const { store } = makeStore({ initial: initial([files, shell], "T") });
    store.setTerminalInventory({ status: "loaded", keys: ["k"] });
    store.setTerminalInventory({ status: "unknown" });
    expect(store.findTab("T")).toBeDefined();
    store.setTerminalInventory({ status: "loaded", keys: [] });
    expect(store.findTab("T")).toBeUndefined();
    expect(store.selectedTabId()).toBe("F");
    expectInvariant(store);
    await expect(
      store.open({ place: { kind: "terminal", key: "k" } }, { opener: "link" }),
    ).resolves.toEqual({ ok: false, reason: "That shell has ended." });
  });

  describe("moving between a split's shells", () => {
    const [left, right, other] = [
      userShellTab("A", "a"),
      userShellTab("B", "b"),
      userShellTab("C", "c"),
    ];
    const split = ["A", "B"];

    it("selects the other shell's tab in place of the current step", async () => {
      const { store } = makeStore({ initial: initial([files, left, right], "F") });
      expect(store.select("A")).toBe(true);
      expect(store.selectAmong("B", split)).toBe(true);
      expect(store.selectedTabId()).toBe("B");
      expectInvariant(store);
      // The plain select's step stays one step: Back goes where it went from A.
      expect(store.history.state.visits.map((visit) => visit.tabId)).toEqual(["F", "B"]);
      await store.go("back");
      expect(store.selectedTabId()).toBe("F");
    });

    it("keeps a plain select made meanwhile, of a tab outside the split", () => {
      const { store } = makeStore({ initial: initial([files, left, right, other], "A") });
      expect(store.select("C")).toBe(true);
      expect(store.selectAmong("B", split)).toBe(false);
      expect(store.selectedTabId()).toBe("C");
      expect(store.history.state.visits.map((visit) => visit.tabId)).toEqual(["A", "C"]);
    });

    it("keeps Forward", async () => {
      const { store } = makeStore({ initial: initial([files, left, right], "F") });
      store.select("A");
      store.select("F");
      await store.go("back");
      expect(store.selectAmong("B", split)).toBe(true);
      expect(store.history.state.visits.map((visit) => visit.tabId)).toEqual(["F", "B", "F"]);
      await store.go("forward");
      expect(store.selectedTabId()).toBe("F");
    });

    it("refuses a tab outside the split", () => {
      const { store } = makeStore({ initial: initial([files, left, right, other], "A") });
      expect(store.selectAmong("C", split)).toBe(false);
      expect(store.selectedTabId()).toBe("A");
    });
  });

  it("drops a restored shell missing from the first loaded list", () => {
    const restored = { ...shell, openedBy: "restore" as const };
    const { store } = makeStore({ initial: initial([files, restored], "F") });
    store.setTerminalInventory({ status: "loaded", keys: [] });
    expect(store.findTab("T")).toBeUndefined();
  });
});

describe("split groups of shells", () => {
  const shells = [
    files,
    userShellTab("T1", "k1"),
    userShellTab("T2", "k2"),
    userShellTab("T2b", "k2"),
  ];

  it("asks once, ends each shell once in order, then removes all their tabs", async () => {
    const { store } = makeStore({ initial: initial(shells, "T1") });
    const confirm = vi.fn().mockResolvedValue(true);
    const ended: string[] = [];
    const result = await store.closeTerminalGroup(["T1", "T2", "T2b"], {
      confirm,
      teardownFor: async (key) => {
        ended.push(key);
        return true;
      },
    });
    expect(result).toEqual({ ok: true });
    expect(confirm).toHaveBeenCalledOnce();
    expect(ended).toEqual(["k1", "k2"]);
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F"]);
    expect(store.snapshot().canReopen).toBe(false);
    expectInvariant(store);
  });

  it("ends every shell the split names, including one without a tab", async () => {
    const { store } = makeStore({ initial: initial(shells, "F") });
    const ended: string[] = [];
    const result = await store.closeTerminalGroup(["T1"], {
      terminalKeys: ["k1", "k9"],
      teardownFor: async (key) => {
        ended.push(key);
        return true;
      },
    });
    expect(result).toEqual({ ok: true });
    expect(ended).toEqual(["k1", "k9"]);
    expect(store.findTab("T1")).toBeUndefined();
  });

  it("keeps the tabs of a shell that failed to end, and names it", async () => {
    const { store } = makeStore({ initial: initial(shells, "F") });
    const result = await store.closeTerminalGroup(["T1", "T2", "T2b"], {
      teardownFor: async (key) => key !== "k2",
    });
    expect(result).toEqual({ ok: false, failed: ["k2"] });
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F", "T2", "T2b"]);
  });

  it("leaves a consistent strip when the selected shell ends and another won't", async () => {
    const { store } = makeStore({ initial: initial(shells, "T1") });
    store.select("T2");
    store.select("T1");
    const result = await store.closeTerminalGroup(["T1", "T2", "T2b"], {
      teardownFor: async (key) => key !== "k2",
    });
    expect(result).toEqual({ ok: false, failed: ["k2"] });
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F", "T2", "T2b"]);
    // The selection lands on a tab that's still there, with history agreeing.
    expect(["F", "T2", "T2b"]).toContain(store.selectedTabId());
    expectInvariant(store);
    expect(store.history.state.visits.some((visit) => visit.tabId === "T1")).toBe(false);
  });

  it("stops as stale when the person acts during a teardown, after removing that shell's tabs", async () => {
    const { store } = makeStore({ initial: initial(shells, "F") });
    const first = deferred<boolean>();
    const ended: string[] = [];
    const closing = store.closeTerminalGroup(["T1", "T2"], {
      teardownFor: (key) => {
        ended.push(key);
        return key === "k1" ? first.promise : Promise.resolve(true);
      },
    });
    await Promise.resolve();
    await store.open({ place: { kind: "pulls" } }, { opener: "user" });
    first.resolve(true);
    await expect(closing).resolves.toEqual({ ok: false, stale: true });
    expect(ended).toEqual(["k1"]);
    expect(store.findTab("T1")).toBeUndefined();
    expect(store.findTab("T2")).toBeDefined();
  });

  it("ends nothing when the confirm is cancelled", async () => {
    const { store } = makeStore({ initial: initial(shells, "F") });
    const teardownFor = vi.fn().mockResolvedValue(true);
    await expect(
      store.closeTerminalGroup(["T1", "T2"], { confirm: async () => false, teardownFor }),
    ).resolves.toEqual({ ok: false, cancelled: true });
    expect(teardownFor).not.toHaveBeenCalled();
  });

  it("ends nothing when the session or design changes while the confirm is open", async () => {
    const { store } = makeStore({ initial: initial(shells, "F") });
    const answer = deferred<boolean>();
    const teardownFor = vi.fn().mockResolvedValue(true);
    const closing = store.closeTerminalGroup(["T1", "T2"], {
      confirm: () => answer.promise,
      teardownFor,
    });
    await Promise.resolve();
    store.invalidate();
    answer.resolve(true);
    await expect(closing).resolves.toEqual({ ok: false, stale: true });
    expect(teardownFor).not.toHaveBeenCalled();
    expect(store.findTab("T1")).toBeDefined();
  });

  it("keeps closing when the shell list catches up on its own deletions", async () => {
    const { store } = makeStore({ initial: initial(shells, "F") });
    store.setTerminalInventory({ status: "loaded", keys: ["k1", "k2"] });
    const ended: string[] = [];
    const result = await store.closeTerminalGroup(["T1", "T2", "T2b"], {
      teardownFor: async (key) => {
        ended.push(key);
        // The deletion's update arrives before the request returns.
        store.setTerminalInventory({
          status: "loaded",
          keys: ["k1", "k2"].filter((live) => !ended.includes(live)),
        });
        return true;
      },
    });
    expect(result).toEqual({ ok: true });
    expect(ended).toEqual(["k1", "k2"]);
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F"]);
    expectInvariant(store);
  });

  it("stops when the shell list drops a shell outside the split while it closes", async () => {
    const other = userShellTab("T3", "k3");
    const { store } = makeStore({ initial: initial([...shells, other], "F") });
    store.setTerminalInventory({ status: "loaded", keys: ["k1", "k2", "k3"] });
    const ended: string[] = [];
    const result = await store.closeTerminalGroup(["T1", "T2"], {
      teardownFor: async (key) => {
        ended.push(key);
        store.setTerminalInventory({ status: "loaded", keys: ["k2"] });
        return true;
      },
    });
    expect(result).toEqual({ ok: false, stale: true });
    expect(ended).toEqual(["k1"]);
  });

  it("refuses a second close of the same shells while the first is still asking", async () => {
    const { store } = makeStore({ initial: initial(shells, "F") });
    const answer = deferred<boolean>();
    const firstTeardown = vi.fn().mockResolvedValue(true);
    const closing = store.closeTerminalGroup(["T1", "T2"], {
      confirm: () => answer.promise,
      teardownFor: firstTeardown,
    });
    await Promise.resolve();
    const teardownFor = vi.fn().mockResolvedValue(true);
    await expect(store.closeTerminalGroup(["T1", "T2"], { teardownFor })).resolves.toEqual({
      ok: false,
      reason: ALREADY_CLOSING_REASON,
    });
    answer.resolve(true);
    await expect(closing).resolves.toEqual({ ok: true });
    expect(teardownFor).not.toHaveBeenCalled();
    expect(firstTeardown.mock.calls).toEqual([["k1"], ["k2"]]);
  });

  it("still names a shell that failed to end when a later change stops the close", async () => {
    const { store } = makeStore({ initial: initial(shells, "F") });
    const second = deferred<boolean>();
    const closing = store.closeTerminalGroup(["T1", "T2"], {
      teardownFor: (key) => (key === "k1" ? Promise.resolve(false) : second.promise),
    });
    await vi.waitFor(() => expect(store.findTab("T1")).toBeDefined());
    await Promise.resolve();
    await store.open({ place: { kind: "pulls" } }, { opener: "user" });
    second.resolve(true);
    await expect(closing).resolves.toEqual({ ok: false, stale: true, failed: ["k1"] });
  });

  it("stops when another shell starts running while it closes, with nothing removed", async () => {
    const { store } = makeStore({ initial: initial(shells, "F") });
    store.setTerminalInventory({ status: "loaded", keys: ["k1", "k2"] });
    // A shell just launched: its tab shows it connecting until the list has it.
    await store.open({ place: { kind: "terminal", key: "k3" } }, { opener: "user" });
    const ended: string[] = [];
    const result = await store.closeTerminalGroup(["T1", "T2"], {
      teardownFor: async (key) => {
        ended.push(key);
        // The other shell shows up as running, before this close's own deletion lands.
        store.setTerminalInventory({ status: "loaded", keys: ["k1", "k2", "k3"] });
        return true;
      },
    });
    expect(result).toEqual({ ok: false, stale: true });
    expect(ended).toEqual(["k1"]);
  });

  it("stops when another shell starts running in the update that drops a closing one", async () => {
    const { store } = makeStore({ initial: initial(shells, "F") });
    store.setTerminalInventory({ status: "loaded", keys: ["k1", "k2"] });
    await store.open({ place: { kind: "terminal", key: "k3" } }, { opener: "user" });
    const ended: string[] = [];
    const result = await store.closeTerminalGroup(["T1", "T2"], {
      teardownFor: async (key) => {
        ended.push(key);
        // One list both drops the shell this close ended and shows the other one running.
        store.setTerminalInventory({ status: "loaded", keys: ["k2", "k3"] });
        return true;
      },
    });
    expect(result).toEqual({ ok: false, stale: true });
    expect(ended).toEqual(["k1"]);
  });

  it("refuses a second close of the same shells while the first is ending them", async () => {
    const { store } = makeStore({ initial: initial(shells, "F") });
    const first = deferred<boolean>();
    const closing = store.closeTerminalGroup(["T1", "T2"], {
      teardownFor: (key) => (key === "k1" ? first.promise : Promise.resolve(true)),
    });
    await Promise.resolve();
    const teardownFor = vi.fn().mockResolvedValue(true);
    await expect(store.closeTerminalGroup(["T2"], { teardownFor })).resolves.toEqual({
      ok: false,
      reason: ALREADY_CLOSING_REASON,
    });
    expect(teardownFor).not.toHaveBeenCalled();
    first.resolve(true);
    await expect(closing).resolves.toEqual({ ok: true });
  });
});

describe("side chat drafts", () => {
  it("fills in the side chat's ID once, when its first message created it", async () => {
    const { store } = makeStore();
    const result = await store.open({ place: { kind: "chat" } }, { opener: "user" });
    const tabId = result.ok ? result.tabId : "";
    expect(store.findTab(tabId)?.binding).toEqual({ kind: "chat", draftKey: "pending:1" });
    expect(store.completeDraftChat(tabId, "child")).toBe(true);
    expect(store.completeDraftChat(tabId, "other")).toBe(false);
    expect(store.findTab(tabId)).toMatchObject({
      place: { kind: "chat", id: "child" },
      binding: { kind: "chat", id: "child", draftKey: "pending:1" },
    });
    expectInvariant(store);
  });

  it("keeps an existing draft's composer key", async () => {
    const { store } = makeStore();
    const result = await store.open(
      { place: { kind: "chat" } },
      { opener: "user", draftKey: "pending:asked" },
    );
    expect(store.findTab(result.ok ? result.tabId : "")?.binding).toEqual({
      kind: "chat",
      draftKey: "pending:asked",
    });
  });
});

describe("the new-tab page", () => {
  const blank: WorkspaceTab = { id: "N", place: { kind: "new" }, openedBy: "user" };

  it("turns into an unbound app in place, and Back never returns to it", () => {
    const { store } = makeStore({ initial: initial([pulls, blank], "N") });
    expect(store.fillNewTab("N", { kind: "changes" })).toBe("N");
    expect(store.findTab("N")?.place).toEqual({ kind: "changes" });
    expect(store.history.serialize().visits).toEqual([{ tabId: "N", place: { kind: "changes" } }]);
    expectInvariant(store);
  });

  it("gives its slot to a bound app", () => {
    const { store } = makeStore({ initial: initial([pulls, blank], "N") });
    const shellTab = store.fillNewTab("N", { kind: "terminal", key: "k" });
    expect(store.tabs().map((tab) => tab.id)).toEqual(["P", shellTab]);
    expectInvariant(store);
  });

  it("still opens a fresh blank page beside another browser tab", () => {
    const browser: WorkspaceTab = {
      id: "B",
      place: { kind: "web" },
      binding: { kind: "browser-view", viewKey: "v1" },
      openedBy: "user",
    };
    const { store } = makeStore({ initial: initial([browser, blank], "N") });
    const page = store.fillNewTab("N", { kind: "web" });
    expect(page).not.toBe("B");
    expect(store.tabs()).toHaveLength(2);
    expect(store.selectedTabId()).toBe(page);
    expectInvariant(store);
  });

  it("selects a single-instance app that is already open and drops the empty tab", () => {
    const { store } = makeStore({ initial: initial([files, pulls, blank], "N") });
    expect(store.fillNewTab("N", { kind: "files" })).toBe("F");
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F", "P"]);
    expectInvariant(store);
  });

  it("leaves another tab where an agent moved it when picking an app that is already open", async () => {
    const tab: WorkspaceTab = { id: "A", place: file("a.ts"), openedBy: "user" };
    const { store } = makeStore({ initial: initial([tab, pulls, blank], "A") });
    store.select("N");
    // The agent moves the clean, unselected A without a history step.
    await store.open({ place: file("b.ts"), tab: "A" }, { opener: "agent" });
    expect(store.findTab("A")?.place).toEqual(file("b.ts"));

    expect(store.fillNewTab("N", { kind: "pulls" })).toBe("P");

    expect(store.selectedTabId()).toBe("P");
    expect(store.findTab("A")?.place).toEqual(file("b.ts"));
    expect(store.tabs().map((candidate) => candidate.id)).toEqual(["A", "P"]);
    expectInvariant(store);
  });
});

describe("saving", () => {
  it("reports a failed save once", async () => {
    const onSaveFailed = vi.fn();
    const { store } = makeStore({ save: () => "failed", onSaveFailed });
    store.select("P");
    store.select("F");
    expect(onSaveFailed).toHaveBeenCalledOnce();
    expect(store.selectedTabId()).toBe("F");
  });

  it("doesn't save a session a newer app owns", () => {
    const { store } = makeStore({ save: null });
    expect(store.select("P")).toBe(true);
  });
});

describe("moving", () => {
  it("reorders without a history step", () => {
    const { store } = makeStore();
    store.move("F", 1);
    expect(store.tabs().map((tab) => tab.id)).toEqual(["P", "F"]);
    expect(store.history.serialize().visits).toHaveLength(1);
  });

  it("puts the tabs in a dragged order once, keeping the selected tab and saving it", () => {
    const { store, save } = makeStore();
    const selected = store.selectedTabId();
    const listener = vi.fn();
    store.subscribe(listener);
    expect(store.reorder(["P", "F"])).toBe(true);
    expect(store.tabs().map((tab) => tab.id)).toEqual(["P", "F"]);
    expect(store.selectedTabId()).toBe(selected);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ tabs: store.tabs() }));
  });

  it("ignores an order that isn't exactly the open tabs, or that changes nothing", () => {
    const { store } = makeStore();
    expect(store.reorder(["F"])).toBe(false);
    expect(store.reorder(["F", "F"])).toBe(false);
    expect(store.reorder(["F", "X"])).toBe(false);
    expect(store.reorder(["F", "P"])).toBe(false);
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F", "P"]);
  });
});

describe("pinned tabs", () => {
  const a: WorkspaceTab = { id: "A", place: file("a.ts"), openedBy: "user" };
  const b: WorkspaceTab = { id: "B", place: file("b.ts"), openedBy: "user" };
  const order = (store: ReturnType<typeof makeStore>["store"]) => store.tabs().map((tab) => tab.id);
  const pinnedIds = (store: ReturnType<typeof makeStore>["store"]) =>
    store
      .tabs()
      .filter((tab) => "pinned" in tab && tab.pinned)
      .map((tab) => tab.id);

  it("pins a tab after the pinned ones, and unpins it to just after them", () => {
    const { store, save } = makeStore({ initial: initial([files, a, b, pulls], "A") });
    expect(store.setPinned(["B"], true)).toBe(true);
    expect(order(store)).toEqual(["B", "F", "A", "P"]);
    expect(store.setPinned(["P"], true)).toBe(true);
    expect(order(store)).toEqual(["B", "P", "F", "A"]);
    expect(pinnedIds(store)).toEqual(["B", "P"]);
    expect(store.setPinned(["B"], false)).toBe(true);
    expect(order(store)).toEqual(["P", "B", "F", "A"]);
    // Pinning a pinned tab changes nothing and saves nothing.
    save.mockClear();
    expect(store.setPinned(["P"], true)).toBe(false);
    expect(save).not.toHaveBeenCalled();
    expect(store.selectedTabId()).toBe("A");
    expectInvariant(store);
  });

  it("stops a move at the pinned tabs' edge, from either side", () => {
    const { store, save } = makeStore({ initial: initial([files, a, b, pulls], "A") });
    store.setPinned(["F", "A"], true);
    save.mockClear();
    store.move("B", 1);
    store.move("A", 2);
    expect(order(store)).toEqual(["F", "A", "B", "P"]);
    expect(save).not.toHaveBeenCalled();
    // Within each group a tab still moves.
    store.move("A", 0);
    store.move("P", 2);
    expect(order(store)).toEqual(["A", "F", "P", "B"]);
  });

  it("lands a drop across the edge at it, and ignores one that leaves nothing moved", () => {
    const { store } = makeStore({ initial: initial([files, a, b, pulls], "A") });
    store.setPinned(["F"], true);
    expect(store.reorder(["P", "F", "A", "B"])).toBe(true);
    expect(order(store)).toEqual(["F", "P", "A", "B"]);
    // Dropped before the pinned tab, it lands back where it was.
    expect(store.reorder(["P", "F", "A", "B"])).toBe(false);
    expect(order(store)).toEqual(["F", "P", "A", "B"]);
  });

  it("opens a tab next to a pinned one after the pinned tabs", async () => {
    const { store } = makeStore({ initial: initial([files, a, pulls], "F") });
    store.setPinned(["F", "A"], true);
    await store.open({ place: file("c.ts") }, { opener: "user", disposition: "new" });
    expect(order(store)).toEqual(["F", "A", "n1", "P"]);
  });

  it.each([
    ["an app in place", { kind: "changes" } as const],
    ["a shell in its slot", { kind: "terminal", key: "k" } as const],
  ])("keeps a pinned New tab pinned once it turns into %s", async (_name, place) => {
    const blank: WorkspaceTab = { id: "N", place: { kind: "new" }, openedBy: "user" };
    const { store } = makeStore({ initial: initial([a, blank], "N") });
    store.setPinned(["N"], true);
    const filled = store.fillNewTab("N", place)!;
    expect(pinnedIds(store)).toEqual([filled]);
    await store.closeOthers("A", () => undefined);
    expect(order(store)).toEqual([filled, "A"]);
    expectInvariant(store);
  });

  it("keeps pinned tabs through Close other tabs", async () => {
    const { store } = makeStore({ initial: initial([files, a, b, pulls], "B") });
    store.setPinned(["P"], true);
    await store.closeOthers("B", () => undefined);
    expect(order(store)).toEqual(["P", "B"]);
    expectInvariant(store);
  });

  it("reopens an unpinned tab after the pinned ones when its old place is now among them", async () => {
    const { store } = makeStore({ initial: initial([files, a, b, pulls], "F") });
    await store.close("A");
    store.setPinned(["B", "P"], true);
    const reopened = store.reopenClosed()!;
    expect(order(store)).toEqual(["B", "P", reopened, "F"]);
    expect(store.findTab(reopened)?.pinned).toBeUndefined();
  });

  it("reopens a pinned tab pinned, among the pinned tabs, even past unpinned ones", async () => {
    const { store } = makeStore({ initial: initial([files, a, b, pulls], "F") });
    store.setPinned(["A", "B"], true);
    await store.close("B");
    store.setPinned(["A"], false);
    const reopened = store.reopenClosed()!;
    expect(order(store)).toEqual([reopened, "A", "F", "P"]);
    expect(store.findTab(reopened)?.pinned).toBe(true);
  });

  it("keeps a saved pin across a reload, and reads a tab saved without one as unpinned", () => {
    const { store, save } = makeStore({ initial: initial([files, a, pulls], "A") });
    store.setPinned(["P"], true);
    const saved = parseSessionState(JSON.parse(JSON.stringify(save.mock.lastCall![0])));
    expect(saved?.tabs.map((tab) => tab.id)).toEqual(["P", "F", "A"]);
    expect(saved?.tabs.map((tab) => ("pinned" in tab ? tab.pinned : undefined))).toEqual([
      true,
      undefined,
      undefined,
    ]);
    // A saved order with a pin out of place, as an older or edited save might have, is mended.
    const mended = makeStore({ initial: initial([files, { ...pulls, pinned: true }], "F") });
    expect(order(mended.store)).toEqual(["P", "F"]);
  });
});

describe("closing races", () => {
  const shell: WorkspaceTab = {
    id: "T",
    place: { kind: "terminal", key: "k" },
    binding: { kind: "terminal", key: "k" },
    openedBy: "user",
  };

  it("ends nothing when the user acts while a shell's close dialog is open", async () => {
    const answer = deferred<boolean>();
    const teardown = vi.fn().mockResolvedValue(true);
    const { store } = makeStore({ initial: initial([files, shell], "T") });
    const closing = store.close("T", { confirm: () => answer.promise, teardown });
    store.select("F");
    answer.resolve(true);
    await expect(closing).resolves.toEqual({ ok: false, stale: true });
    expect(teardown).not.toHaveBeenCalled();
    expect(store.findTab("T")).toBeDefined();
  });

  it("ends the shell the user confirmed when an unrelated shell list arrives during the dialog", async () => {
    const answer = deferred<boolean>();
    const teardown = vi.fn().mockResolvedValue(true);
    const { store } = makeStore({ initial: initial([files, shell], "T") });
    store.setTerminalInventory({ status: "loaded", keys: ["k"] });
    const closing = store.close("T", { confirm: () => answer.promise, teardown });
    // Another shell started elsewhere; nothing about this tab changed.
    store.setTerminalInventory({ status: "loaded", keys: ["k", "other"] });
    answer.resolve(true);
    await expect(closing).resolves.toMatchObject({ ok: true });
    expect(teardown).toHaveBeenCalledTimes(1);
    expect(store.findTab("T")).toBeUndefined();
  });

  it("keeps a confirmed navigation when an unrelated shell list arrives during the question", async () => {
    const tab: WorkspaceTab = { id: "A", place: file("a.ts"), openedBy: "user" };
    const answer = deferred<"leave" | "cancel">();
    const { store, dirty } = makeStore({
      initial: initial([files, tab], "A"),
      requestLeave: () => answer.promise,
    });
    dirty.add("A");
    const navigation = store.navigate("A", file("b.ts"));
    store.setTerminalInventory({ status: "loaded", keys: ["other"] });
    answer.resolve("leave");
    await expect(navigation).resolves.toMatchObject({ ok: true });
    expect(store.findTab("A")?.place).toEqual(file("b.ts"));
  });

  it("goes stale when the session or design switches during the dialog", async () => {
    const answer = deferred<boolean>();
    const teardown = vi.fn().mockResolvedValue(true);
    const { store } = makeStore({ initial: initial([files, shell], "T") });
    const closing = store.close("T", { confirm: () => answer.promise, teardown });
    store.invalidate();
    answer.resolve(true);
    await expect(closing).resolves.toEqual({ ok: false, stale: true });
    expect(teardown).not.toHaveBeenCalled();
  });

  it("keeps the tab when its resource couldn't be torn down", async () => {
    const { store } = makeStore({ initial: initial([files, shell], "T") });
    await expect(store.close("T", { teardown: () => Promise.resolve(false) })).resolves.toEqual({
      ok: false,
      cancelled: true,
    });
    expect(store.findTab("T")).toBeDefined();
  });

  it("closes other tabs as one action: Keep editing on any of them closes none", async () => {
    const a: WorkspaceTab = { id: "A", place: file("a.ts"), openedBy: "user" };
    const b: WorkspaceTab = { id: "B", place: file("b.ts"), openedBy: "user" };
    const { store, dirty, requestLeave } = makeStore({ initial: initial([files, a, b], "F") });
    dirty.add("B");
    requestLeave.mockResolvedValue("cancel");
    const before = store.serialize();
    await expect(store.closeOthers("F", () => undefined)).resolves.toEqual({
      ok: false,
      cancelled: true,
    });
    expect(store.serialize()).toEqual(before);
    expect(requestLeave).toHaveBeenCalledWith(["B"], "close");
  });

  it("closes other tabs but keeps shells", async () => {
    const a: WorkspaceTab = { id: "A", place: file("a.ts"), openedBy: "user" };
    const { store } = makeStore({ initial: initial([files, a, shell, pulls], "A") });
    await store.closeOthers("A", () => undefined);
    expect(store.tabs().map((tab) => tab.id)).toEqual(["A", "T"]);
    expectInvariant(store);
  });
});

describe("closing a group of tabs", () => {
  const a: WorkspaceTab = { id: "A", place: file("a.ts"), openedBy: "user" };
  const b: WorkspaceTab = { id: "B", place: file("b.ts"), openedBy: "user" };
  const shell: WorkspaceTab = {
    id: "T",
    place: { kind: "terminal", key: "k" },
    binding: { kind: "terminal", key: "k" },
    openedBy: "user",
  };

  it("closes the asked tabs in one commit but keeps pinned tabs and shells", async () => {
    const { store } = makeStore({ initial: initial([files, a, b, shell, pulls], "F") });
    store.setPinned(["B"], true);
    const notified = vi.fn();
    store.subscribe(notified);
    await expect(store.closeMany(["A", "B", "T", "P"], () => undefined)).resolves.toEqual({
      ok: true,
    });
    expect(notified).toHaveBeenCalledTimes(1);
    expect(store.tabs().map((tab) => tab.id)).toEqual(["B", "F", "T"]);
    expectInvariant(store);
  });

  it("asks once about all unsaved edits, and Keep editing closes none", async () => {
    const { store, dirty, requestLeave } = makeStore({ initial: initial([files, a, b], "F") });
    dirty.add("A");
    dirty.add("B");
    requestLeave.mockResolvedValue("cancel");
    const before = store.serialize();
    await expect(store.closeMany(["A", "B"], () => undefined)).resolves.toEqual({
      ok: false,
      cancelled: true,
    });
    expect(requestLeave).toHaveBeenCalledOnce();
    expect(requestLeave).toHaveBeenCalledWith(["A", "B"], "close");
    expect(store.serialize()).toEqual(before);
  });

  it("closes nothing when a tab is pinned while it asks about unsaved edits", async () => {
    const { store, dirty, requestLeave } = makeStore({ initial: initial([files, a, b], "F") });
    dirty.add("A");
    requestLeave.mockImplementation(async () => {
      store.setPinned(["B"], true);
      return "leave";
    });
    await expect(store.closeMany(["A", "B"], () => undefined)).resolves.toEqual({
      ok: false,
      stale: true,
    });
    expect(store.findTab("A")).toBeDefined();
    expect(store.findTab("B")).toBeDefined();
    expectInvariant(store);
  });

  it("selects a neighbour when the selected tab closes, and reopens each one", async () => {
    const { store } = makeStore({ initial: initial([files, a, b], "A") });
    await store.closeMany(["A", "B"], () => undefined);
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F"]);
    expect(store.selectedTabId()).toBe("F");
    const reopened = [store.reopenClosed(), store.reopenClosed()].map(
      (tabId) => store.findTab(tabId!)?.place,
    );
    expect(reopened).toEqual(expect.arrayContaining([a.place, b.place]));
    expectInvariant(store);
  });

  it("does nothing for tabs that are gone or can't close as a group", async () => {
    const { store, requestLeave } = makeStore({ initial: initial([files, shell], "F") });
    const notified = vi.fn();
    store.subscribe(notified);
    await expect(store.closeMany(["T", "missing"], () => undefined)).resolves.toEqual({
      ok: true,
    });
    expect(notified).not.toHaveBeenCalled();
    expect(requestLeave).not.toHaveBeenCalled();
  });

  it("asks once in Close other tabs too", async () => {
    const { store, dirty, requestLeave } = makeStore({ initial: initial([files, a, b], "F") });
    dirty.add("A");
    requestLeave.mockResolvedValue("leave");
    await expect(store.closeOthers("F", () => undefined)).resolves.toEqual({ ok: true });
    expect(requestLeave).toHaveBeenCalledOnce();
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F"]);
  });
});

describe("the tab cap with tabs from a newer app", () => {
  it("counts kept tabs it can't show", async () => {
    const opaque = Array.from({ length: MAX_WORKSPACE_TABS - 1 }, (_, index) => ({
      id: `u${index}`,
      unknown: { id: `u${index}`, place: { kind: "issue" } },
    }));
    const { store } = makeStore({
      initial: { ...initial([files], "F"), tabs: [files, ...opaque] },
    });
    await expect(store.open({ place: { kind: "pulls" } }, { opener: "user" })).resolves.toEqual({
      ok: false,
      reason: "Close some workspace tabs to open more.",
    });
    expect(store.tabs()).toHaveLength(MAX_WORKSPACE_TABS);
  });
});

describe("restored resources", () => {
  it("drops a user-opened shell saved last run once the loaded list lacks it", () => {
    const shell: WorkspaceTab = {
      id: "T",
      place: { kind: "terminal", key: "k" },
      binding: { kind: "terminal", key: "k" },
      openedBy: "user",
    };
    const saved = makeStore({ initial: initial([files, shell], "T") }).store.serialize();
    const raw = JSON.parse(JSON.stringify({ id: "s1", ...saved }));
    const restored = parseSessionState(raw)!;
    expect(restored.tabs.find((tab) => tab.id === "T")).toMatchObject({ openedBy: "user" });
    const { store } = makeStore({ initial: restored });
    store.setTerminalInventory({ status: "loaded", keys: [] });
    expect(store.findTab("T")).toBeUndefined();
    expectInvariant(store);
  });

  const chat: WorkspaceTab = {
    id: "C",
    place: { kind: "chat", id: "child" },
    binding: { kind: "chat", id: "child", draftKey: "child" },
    openedBy: "user",
  };
  const agent: WorkspaceTab = {
    id: "G",
    place: { kind: "agent", id: "sub" },
    binding: { kind: "agent", id: "sub" },
    openedBy: "user",
  };

  it("drops side-chat and agent tabs whose session no longer exists, landing as a close", () => {
    const { store } = makeStore({ initial: initial([files, chat, agent], "C") });
    store.setSessionState("child", "gone");
    store.setSessionState("sub", "gone");
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F"]);
    expectInvariant(store);
  });

  it("keeps them when the lookup failed for another reason", () => {
    const { store } = makeStore({ initial: initial([files, chat, agent], "C") });
    store.setSessionState("child", "unknown");
    expect(store.findTab("C")).toBeDefined();
    expect(store.resourceState("C")).toBe("unknown");
  });

  it("closing a side-chat tab only hides it; it reopens while the chat exists", async () => {
    const { store } = makeStore({ initial: initial([files, chat], "C") });
    store.setSessionState("child", "live");
    await store.close("C");
    expect(store.findTab(store.reopenClosed()!)?.place).toEqual({ kind: "chat", id: "child" });
  });

  it("doesn't reopen a side chat whose session is gone", async () => {
    const { store } = makeStore({ initial: initial([files, chat], "C") });
    await store.close("C");
    store.setSessionState("child", "gone");
    expect(store.reopenClosed()).toBeNull();
  });
});

describe("closing other tabs releases browser views after the tabs went", () => {
  const web: WorkspaceTab = {
    id: "W",
    place: { kind: "web" },
    binding: { kind: "browser-view", viewKey: "v" },
    openedBy: "user",
  };
  const a: WorkspaceTab = { id: "A", place: file("a.ts"), openedBy: "user" };

  it("removes every tab in one commit, before any view is released", async () => {
    const release = deferred<boolean>();
    const { store } = makeStore({ initial: initial([files, a, web], "A") });
    const notified = vi.fn();
    store.subscribe(notified);
    const releaseView = vi.fn(() => release.promise);

    await expect(
      store.closeOthers("F", (tab) => (tab.id === "W" ? releaseView : undefined)),
    ).resolves.toEqual({ ok: true });

    // Published at once, while W's view is still being released.
    expect(notified).toHaveBeenCalledTimes(1);
    expect(store.tabs().map((tab) => tab.id)).toEqual(["F"]);
    expect(store.selectedTabId()).toBe("F");
    expect(releaseView).toHaveBeenCalledOnce();
    expectInvariant(store);
    release.resolve(true);
  });

  it("keeps a tab closed when its view couldn't be released", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { store } = makeStore({ initial: initial([files, web], "W") });
    await store.closeOthers("F", () => () => Promise.reject(new Error("no view")));
    await Promise.resolve();
    await Promise.resolve();
    expect(store.findTab("W")).toBeUndefined();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it("closes a single browser tab at once and releases its view afterwards", async () => {
    const release = deferred<boolean>();
    const { store } = makeStore({ initial: initial([files, web], "W") });
    const closing = store.close("W", { release: () => release.promise });
    await expect(closing).resolves.toEqual({ ok: true });
    expect(store.findTab("W")).toBeUndefined();
    release.resolve(false);
    await Promise.resolve();
    expect(store.findTab("W")).toBeUndefined();
  });
});

describe("the sidebar's last tab", () => {
  const shellTab = (key: string): WorkspaceTab => ({
    id: `S-${key}`,
    place: { kind: "terminal", key },
    binding: { kind: "terminal", key },
    openedBy: "restore",
  });
  const sideChat: WorkspaceTab = {
    id: "C",
    place: { kind: "chat", id: "child" },
    binding: { kind: "chat", id: "child", draftKey: "child" },
    openedBy: "restore",
  };
  const onlyNewTab = (store: ReturnType<typeof makeStore>["store"]) => {
    expect(store.tabs()).toHaveLength(1);
    expect(store.selectedTab()?.place).toEqual({ kind: "new" });
    expect(isOnlyEmptyNewTab(store.tabs(), store.selectedTabId()!)).toBe(true);
    expectInvariant(store);
  };

  it("lands on a New tab after closing every tab one by one", async () => {
    const { store } = makeStore();
    await store.close("F");
    expect(store.tabs().map((tab) => tab.id)).toEqual(["P"]);
    await store.close("P");
    onlyNewTab(store);
  });

  it("lands on a New tab when a shell close, an ended shell or a gone chat takes the last one", async () => {
    const closed = makeStore({ initial: initial([shellTab("a")], "S-a") });
    closed.store.setTerminalInventory({ status: "loaded", keys: ["a"] });
    await closed.store.close("S-a", { teardown: async () => true });
    onlyNewTab(closed.store);

    const ended = makeStore({ initial: initial([shellTab("b")], "S-b") });
    ended.store.setTerminalInventory({ status: "loaded", keys: ["b"] });
    ended.store.setTerminalInventory({ status: "loaded", keys: [] });
    onlyNewTab(ended.store);

    const gone = makeStore({ initial: initial([sideChat], "C") });
    gone.store.setSessionState("child", "gone");
    onlyNewTab(gone.store);
  });

  it("starts a session restored with no tabs on a New tab", () => {
    const { store } = makeStore({
      initial: {
        tabs: [],
        selectedTabId: null,
        timeline: [],
        cursor: -1,
        closed: [],
      },
    });
    onlyNewTab(store);
  });

  it("keeps the tab, with no New tab, when unsaved edits or a shell's close is cancelled", async () => {
    const dirtyStore = makeStore({ initial: initial([files], "F") });
    dirtyStore.dirty.add("F");
    dirtyStore.requestLeave.mockResolvedValueOnce("cancel");
    expect(await dirtyStore.store.close("F")).toEqual({ ok: false, cancelled: true });
    expect(dirtyStore.store.tabs().map((tab) => tab.id)).toEqual(["F"]);

    const shell = makeStore({ initial: initial([shellTab("a")], "S-a") });
    expect(await shell.store.close("S-a", { confirm: async () => false })).toEqual({
      ok: false,
      cancelled: true,
    });
    expect(shell.store.tabs().map((tab) => tab.id)).toEqual(["S-a"]);
  });

  it("can't close an only empty New tab, and changes nothing trying", async () => {
    const { store, save } = makeStore({ initial: initial([files], "F") });
    await store.close("F");
    const before = store.serialize();
    const saves = save.mock.calls.length;
    expect(await store.close(store.selectedTabId()!)).toEqual({
      ok: false,
      reason: LAST_NEW_TAB_REASON,
    });
    expect(store.serialize()).toEqual(before);
    expect(save.mock.calls.length).toBe(saves);
  });

  it("closes an only New tab on its shells page, landing on a plain New tab", async () => {
    const shells: WorkspaceTab = {
      id: "N",
      place: { kind: "new", app: "terminal" },
      openedBy: "user",
    };
    const { store } = makeStore({ initial: initial([shells], "N") });
    expect(isOnlyEmptyNewTab(store.tabs())).toBe(false);
    expect(await store.close("N")).toEqual({ ok: true });
    onlyNewTab(store);
    expect(store.selectedTabId()).not.toBe("N");
  });

  it("doesn't take an only tab it can't read for an empty New tab", () => {
    const unknown = parseSessionState({
      tabs: [{ id: "U", place: { kind: "from-a-newer-app" }, openedBy: "user" }],
      selectedTabId: "U",
      timeline: [],
      cursor: -1,
      closed: [],
    });
    expect(unknown?.tabs).toHaveLength(1);
    expect(isOnlyEmptyNewTab(unknown!.tabs)).toBe(false);
  });

  it("reopens the last closed tab in the New tab's place, in one saved step", async () => {
    const { store, save } = makeStore({ initial: initial([files], "F") });
    await store.close("F");
    const saves = save.mock.calls.length;
    const reopened = store.reopenClosed();
    expect(store.tabs().map((tab) => tab.id)).toEqual([reopened]);
    expect(store.selectedTab()?.place).toEqual(files.place);
    expect(store.history.serialize().visits.some((visit) => visit.place.kind === "new")).toBe(
      false,
    );
    expect(save.mock.calls.length).toBe(saves + 1);
    expectInvariant(store);
  });

  it("opens a recent place from the New tab without leaving another New tab", async () => {
    const { store } = makeStore({ initial: initial([files], "F") });
    await store.close("F");
    const result = store.openRecent(store.selectedTabId()!, "files");
    expect(result).toMatchObject({ ok: true });
    expect(store.tabs().map((tab) => (tab as WorkspaceTab).place)).toEqual([files.place]);
    expectInvariant(store);
  });

  it("turns the New tab into a shell launched from it, leaving no New visit", async () => {
    const { store } = makeStore({ initial: initial([files], "F") });
    await store.close("F");
    const shellTabId = store.fillNewTab(store.selectedTabId()!, { kind: "terminal", key: "k" });
    expect(store.tabs().map((tab) => tab.id)).toEqual([shellTabId]);
    expect(store.history.serialize().visits.map((visit) => visit.place.kind)).not.toContain("new");
    expectInvariant(store);
  });
});

describe("a sidebar that rests empty", () => {
  const pull = (url: string, view: "summary" | "changes" = "summary"): WorkspacePlace => ({
    kind: "pull",
    url,
    view,
  });
  const first = "https://github.com/example/repo/pull/1";
  const second = "https://github.com/example/repo/pull/2";
  const emptyInitial = (): WorkspaceTabsSessionState => ({
    tabs: [],
    selectedTabId: null,
    timeline: [],
    cursor: 0,
    closed: [],
  });

  it("starts with no tabs and nothing selected", () => {
    const { store } = makeStore({ resting: "empty", initial: emptyInitial() });
    expect(store.tabs()).toEqual([]);
    expect(store.selectedTabId()).toBeNull();
  });

  it("opens one tab per pull request, and the same one again for another of its views", async () => {
    const { store } = makeStore({ resting: "empty", initial: emptyInitial() });
    await store.open({ place: pull(first) }, { opener: "link" });
    await store.open({ place: pull(second) }, { opener: "link" });
    const again = await store.open({ place: pull(first, "changes") }, { opener: "link" });
    expect(again).toMatchObject({ ok: true, tabId: "n1", created: false, selected: true });
    expect(store.tabs().map((tab) => ("place" in tab ? tab.place : null))).toEqual([
      pull(first, "changes"),
      pull(second),
    ]);
    expectInvariant(store);
  });

  it("leaves no tab when the last one closes, and reopening brings it back", async () => {
    const { store } = makeStore({ resting: "empty", initial: emptyInitial() });
    await store.open({ place: pull(first) }, { opener: "link" });
    expect(await store.close("n1")).toEqual({ ok: true });
    expect(store.tabs()).toEqual([]);
    expect(store.selectedTabId()).toBeNull();
    expect(store.snapshot().canReopen).toBe(true);
    const reopened = store.reopenClosed();
    expect(reopened).not.toBeNull();
    expect(store.selectedTab()?.place).toEqual(pull(first));
    expectInvariant(store);
  });

  it("keeps a session's New tab when the last tab closes, by default", async () => {
    const { store } = makeStore({ initial: initial([pulls], "P") });
    await store.close("P");
    expect(isOnlyEmptyNewTab(store.tabs())).toBe(true);
  });
});
