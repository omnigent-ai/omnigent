// The new-tab page's recent places, and going to one: closed tabs and other open
// tabs, each once, and a closed entry used up only when going there worked.

import { describe, expect, it, vi } from "vitest";

import type { ClosedTab, WorkspaceTab } from "./workspaceTab";
import { createWorkspaceTabsStore, RECENT_GONE_REASON } from "./workspaceTabsStore";

const page: WorkspaceTab = { id: "N", place: { kind: "new" }, openedBy: "user" };
const fileTab = (id: string, path: string): WorkspaceTab => ({
  id,
  place: { kind: "files", path },
  openedBy: "user",
});
const shell: WorkspaceTab = {
  id: "S",
  place: { kind: "terminal", key: "t1" },
  binding: { kind: "terminal", key: "t1" },
  openedBy: "user",
};
const browser: WorkspaceTab = {
  id: "W",
  place: { kind: "web" },
  binding: { kind: "browser-view", viewKey: "v1" },
  openedBy: "user",
};
const closedEntry = (tab: WorkspaceTab, index = 0): ClosedTab => ({ tab, index });

function makeStore(tabs: WorkspaceTab[], closed: ClosedTab[] = [], selected = "N") {
  let next = 0;
  return createWorkspaceTabsStore({
    scopeId: "s1",
    initial: {
      tabs,
      selectedTabId: selected,
      timeline: [{ tabId: selected, place: tabs.find((tab) => tab.id === selected)!.place }],
      cursor: 0,
      closed,
    },
    save: vi.fn().mockReturnValue("saved"),
    requestLeave: vi.fn().mockResolvedValue("leave"),
    newTabId: () => `n${++next}`,
    newViewKey: () => `view${next}`,
  });
}

const identities = (store: ReturnType<typeof makeStore>) =>
  store.recentPlaces("N").map((recent) => recent.identity);
const tabIds = (store: ReturnType<typeof makeStore>) => store.tabs().map((tab) => tab.id);

describe("recentPlaces", () => {
  it("lists closed tabs newest first, then other open tabs, each place once", () => {
    const store = makeStore(
      [page, fileTab("A", "a.ts"), fileTab("B", "b.ts")],
      [closedEntry(fileTab("x", "old.ts")), closedEntry(fileTab("y", "b.ts"))],
      "A",
    );
    store.select("B");
    store.select("N");
    // Newest closed first; b.ts is closed and open, so it's listed once.
    expect(identities(store)).toEqual(["files:b.ts", "files:old.ts", "files:a.ts"]);
  });

  it("leaves out the page itself, shells, new-tab pages and places without an identity", () => {
    const store = makeStore([page, shell, browser, { ...page, id: "N2" }, fileTab("A", "a.ts")]);
    expect(identities(store)).toEqual(["files:a.ts"]);
  });

  it("is empty for a fresh session", () => {
    expect(makeStore([page]).recentPlaces("N")).toEqual([]);
  });
});

describe("openRecent", () => {
  it("reopens a closed place as Reopen would, using up its entry, and the page gives way", () => {
    const store = makeStore(
      [fileTab("A", "a.ts"), page],
      [closedEntry(fileTab("x", "older.ts"), 0), closedEntry(fileTab("y", "old.ts"), 1)],
    );
    const result = store.openRecent("N", "files:old.ts");
    expect(result).toEqual({ ok: true, tabId: "n1" });
    expect(store.selectedTab()?.place).toEqual({ kind: "files", path: "old.ts" });
    expect(tabIds(store)).toEqual(["A", "n1"]);
    // Reopen now brings back the next one, never a second old.ts.
    store.reopenClosed();
    expect(store.selectedTab()?.place).toEqual({ kind: "files", path: "older.ts" });
  });

  it("selects the open tab when the place is both open and closed, without a duplicate", () => {
    const store = makeStore([fileTab("A", "a.ts"), page], [closedEntry(fileTab("x", "a.ts"))]);
    expect(store.openRecent("N", "files:a.ts")).toEqual({ ok: true, tabId: "A" });
    expect(tabIds(store)).toEqual(["A"]);
    expect(store.selectedTabId()).toBe("A");
    expect(store.snapshot().canReopen).toBe(false);
  });

  it("goes to the tab its row stands for: the most recently selected of two showing it", () => {
    const store = makeStore([fileTab("A", "a.ts"), fileTab("B", "a.ts"), page], [], "A");
    store.select("B");
    store.select("N");
    expect(store.recentPlaces("N")).toEqual([
      expect.objectContaining({ identity: "files:a.ts", openTabId: "B" }),
    ]);
    expect(store.openRecent("N", "files:a.ts")).toEqual({ ok: true, tabId: "B" });
    expect(store.selectedTabId()).toBe("B");
  });

  it("selects an unseen background tab as any selection does, marking it seen", () => {
    const unseen: WorkspaceTab = { ...fileTab("U", "agent.ts"), openedBy: "agent", unseen: true };
    const store = makeStore([page, unseen]);
    expect(store.openRecent("N", "files:agent.ts")).toEqual({ ok: true, tabId: "U" });
    expect(store.findTab("U")?.unseen).toBeUndefined();
    expect(store.history.serialize().visits.at(-1)).toEqual({
      tabId: "U",
      place: { kind: "files", path: "agent.ts" },
    });
  });

  it("lists a place that's closed and still open as its open tab", () => {
    const store = makeStore([fileTab("A", "a.ts"), page], [closedEntry(fileTab("x", "a.ts"))]);
    expect(store.recentPlaces("N")).toEqual([
      { identity: "files:a.ts", place: { kind: "files", path: "a.ts" }, openTabId: "A" },
    ]);
  });

  it("changes nothing when the closed place's chat is gone", () => {
    const chat: WorkspaceTab = {
      id: "C",
      place: { kind: "chat", id: "child-1" },
      binding: { kind: "chat", id: "child-1", draftKey: "child-1" },
      openedBy: "user",
    };
    const store = makeStore([page], [closedEntry(chat)]);
    store.setSessionState("child-1", "gone");
    const before = store.serialize();
    expect(store.openRecent("N", "chat:child-1")).toEqual({
      ok: false,
      reason: RECENT_GONE_REASON,
    });
    expect(store.serialize()).toEqual(before);
  });

  it("refuses a place that is neither open nor closed, changing nothing", () => {
    const store = makeStore([page]);
    const before = store.serialize();
    expect(store.openRecent("N", "files:none.ts").ok).toBe(false);
    expect(store.serialize()).toEqual(before);
  });
});
