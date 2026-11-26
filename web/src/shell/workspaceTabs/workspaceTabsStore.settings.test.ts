import { describe, expect, it, vi } from "vitest";

import type { DefaultWorkspaceTab } from "@/lib/workspaceTabPreferences";

import type { WorkspaceTab } from "./workspaceTab";
import {
  MAX_WORKSPACE_TABS,
  parseSessionState,
  type WorkspaceTabsSessionState,
} from "./workspaceTabsPersistence";
import { createWorkspaceTabsStore } from "./workspaceTabsStore";

const EMPTY: WorkspaceTabsSessionState = {
  tabs: [],
  selectedTabId: null,
  timeline: [],
  cursor: -1,
  closed: [],
};

function makeStore(initial: WorkspaceTabsSessionState = EMPTY) {
  let next = 0;
  const save = vi.fn().mockReturnValue("saved");
  const store = createWorkspaceTabsStore({
    scopeId: "s1",
    initial,
    save,
    requestLeave: vi.fn().mockResolvedValue("leave"),
    isTabDirty: () => false,
    newTabId: () => `n${++next}`,
  });
  return { store, save };
}

const showsAll = () => true;
const settings = (pinned: DefaultWorkspaceTab[], initial: DefaultWorkspaceTab | null) => ({
  pinned,
  initial,
});

function strip(store: ReturnType<typeof makeStore>["store"]) {
  return store
    .tabs()
    .map((tab) =>
      "place" in tab ? `${tab.place.kind}${tab.pinned ? " (pinned)" : ""}` : "unknown",
    );
}

function selectedKind(store: ReturnType<typeof makeStore>["store"]) {
  return store.selectedTab()?.place.kind;
}

describe("a strip following the Workspace settings", () => {
  it("starts with the pinned pages in Settings order, on the first page, instead of a New tab", () => {
    const { store } = makeStore();
    expect(strip(store)).toEqual(["new"]);
    store.applyTabSettings(settings(["files", "github", "subagents"], "subagents"), showsAll);
    expect(strip(store)).toEqual(["files (pinned)", "pulls (pinned)", "agents (pinned)"]);
    expect(selectedKind(store)).toBe("agents");
    expect(store.findTab(store.tabs()[0].id)?.role).toBe("files");
    expect(store.history.state.visits.map((visit) => visit.place.kind)).toEqual(["agents"]);
  });

  it("keeps the New tab when nothing is pinned", () => {
    const { store } = makeStore();
    store.applyTabSettings(settings([], null), showsAll);
    expect(strip(store)).toEqual(["new"]);
  });

  it("leaves a pinned page the user closed closed until its setting changes", async () => {
    const { store } = makeStore();
    store.applyTabSettings(settings(["github", "tasks"], "github"), showsAll);
    const tasks = store.tabs().find((tab) => "place" in tab && tab.place.kind === "tasks")!;
    await store.close(tasks.id);
    expect(store.applyTabSettings(settings(["github", "tasks"], "github"), showsAll)).toBe(false);
    expect(strip(store)).toEqual(["pulls (pinned)"]);
    // Unpinning and pinning it again in Settings brings it back.
    store.applyTabSettings(settings(["github"], "github"), showsAll);
    store.applyTabSettings(settings(["github", "tasks"], "github"), showsAll);
    expect(strip(store)).toEqual(["pulls (pinned)", "tasks (pinned)"]);
  });

  it("reopens a pinned page the user closed pinned, once it's chosen as the first page", async () => {
    const { store } = makeStore();
    store.applyTabSettings(settings(["github", "tasks"], "github"), showsAll);
    const tasks = store.tabs().find((tab) => "place" in tab && tab.place.kind === "tasks")!;
    await store.close(tasks.id);
    store.applyTabSettings(settings(["github", "tasks"], "tasks"), showsAll);
    expect(strip(store)).toEqual(["pulls (pinned)", "tasks (pinned)"]);
    expect(selectedKind(store)).toBe("tasks");
  });

  it("opens an unpinned first page without a pin", () => {
    const { store } = makeStore();
    store.applyTabSettings(settings(["github"], "github"), showsAll);
    store.applyTabSettings(settings(["github"], "tasks"), showsAll);
    expect(strip(store)).toEqual(["pulls (pinned)", "tasks"]);
    expect(selectedKind(store)).toBe("tasks");
  });

  it("pins an open page, and keeps an unpinned page open without its pin", () => {
    const tasks: WorkspaceTab = { id: "T", place: { kind: "tasks" }, openedBy: "user" };
    const pulls: WorkspaceTab = {
      id: "P",
      place: { kind: "pulls" },
      openedBy: "user",
      pinned: true,
    };
    const { store } = makeStore({
      tabs: [pulls, tasks],
      selectedTabId: "T",
      timeline: [{ tabId: "T", place: tasks.place }],
      cursor: 0,
      closed: [],
      tabSettings: { pinned: ["github"], initial: "github" },
    });
    store.applyTabSettings(settings(["tasks"], "tasks"), showsAll);
    expect(strip(store)).toEqual(["tasks (pinned)", "pulls"]);
    expect(selectedKind(store)).toBe("tasks");
  });

  it("selects a changed first page once, then leaves the user's selection alone", () => {
    const { store } = makeStore();
    store.applyTabSettings(settings(["github", "tasks"], "github"), showsAll);
    store.applyTabSettings(settings(["github", "tasks"], "tasks"), showsAll);
    expect(selectedKind(store)).toBe("tasks");
    store.select(store.tabs()[0].id);
    store.applyTabSettings(settings(["github", "tasks"], "tasks"), showsAll);
    expect(selectedKind(store)).toBe("pulls");
  });

  it("pins a first page the session couldn't show once it can, without switching to it", () => {
    const { store } = makeStore();
    let pullRequests = false;
    const canShow = (page: DefaultWorkspaceTab) => page !== "github" || pullRequests;
    store.applyTabSettings(settings(["github", "tasks"], "github"), canShow);
    expect(strip(store)).toEqual(["tasks (pinned)"]);
    expect(selectedKind(store)).toBe("tasks");
    pullRequests = true;
    store.applyTabSettings(settings(["github", "tasks"], "github"), canShow);
    expect(strip(store)).toEqual(["tasks (pinned)", "pulls (pinned)"]);
    expect(selectedKind(store)).toBe("tasks");
  });

  it("keeps the New tab and its selection when the only pin arrives late", () => {
    const { store } = makeStore();
    let pullRequests = false;
    const canShow = (page: DefaultWorkspaceTab) => page !== "github" || pullRequests;
    store.applyTabSettings(settings(["github"], "github"), canShow);
    expect(strip(store)).toEqual(["new"]);
    pullRequests = true;
    store.applyTabSettings(settings(["github"], "github"), canShow);
    expect(strip(store)).toEqual(["pulls (pinned)", "new"]);
    expect(selectedKind(store)).toBe("new");
  });

  it("keeps the New tab and its selection when a setting pins a page later", () => {
    const { store } = makeStore();
    store.applyTabSettings(settings([], null), showsAll);
    expect(strip(store)).toEqual(["new"]);
    // Another window or a settings import pins a page, without changing the first page.
    store.applyTabSettings(settings(["tasks"], null), showsAll);
    expect(strip(store)).toEqual(["tasks (pinned)", "new"]);
    expect(selectedKind(store)).toBe("new");
  });

  it("selects a first page chosen while another waited, after the user opened a file", async () => {
    const { store } = makeStore();
    const canShow = (page: DefaultWorkspaceTab) => page !== "github";
    store.applyTabSettings(settings(["files", "tasks", "github"], "github"), canShow);
    const filesTab = store.selectedTab()!;
    await store.navigate(filesTab.id, { kind: "files", path: "README.md" });
    store.applyTabSettings(settings(["files", "tasks", "github"], "tasks"), canShow);
    expect(selectedKind(store)).toBe("tasks");
  });

  it("saves what it took with the strip, and a reloaded strip doesn't take it again", () => {
    const { store, save } = makeStore();
    store.applyTabSettings(settings(["tasks"], "tasks"), showsAll);
    const saved: WorkspaceTabsSessionState = save.mock.calls.at(-1)![0];
    expect(saved.tabSettings).toEqual({ pinned: ["tasks"], initial: "tasks" });
    const reloaded = makeStore(parseSessionState(JSON.parse(JSON.stringify(saved)))!);
    expect(reloaded.store.applyTabSettings(settings(["tasks"], "tasks"), showsAll)).toBe(false);
  });

  it("gives a strip saved before this record the current settings once", () => {
    const files: WorkspaceTab = {
      id: "F",
      place: { kind: "files" },
      role: "files",
      openedBy: "user",
    };
    const { store } = makeStore({
      tabs: [files],
      selectedTabId: "F",
      timeline: [{ tabId: "F", place: files.place }],
      cursor: 0,
      closed: [],
    });
    store.applyTabSettings(settings(["files", "tasks"], "files"), showsAll);
    expect(strip(store)).toEqual(["files (pinned)", "tasks (pinned)"]);
    expect(selectedKind(store)).toBe("files");
  });
});

describe("a strip following the Workspace settings, at its edges", () => {
  it("keeps what a link opened before the sidebar first showed, and still pins the pages", () => {
    const linked: WorkspaceTab = {
      id: "L",
      place: { kind: "files", path: "src/linked.ts", line: { start: 7 } },
      openedBy: "link",
    };
    const { store } = makeStore({
      tabs: [linked],
      selectedTabId: "L",
      timeline: [{ tabId: "L", place: linked.place }],
      cursor: 0,
      closed: [],
    });
    store.applyTabSettings(settings(["files", "tasks"], "files"), showsAll);
    expect(store.selectedTab()?.place).toEqual(linked.place);
    expect(strip(store)).toEqual(["files (pinned)", "tasks (pinned)", "files"]);
    // The first page counts as taken, so it doesn't take the selection later either.
    store.applyTabSettings(settings(["files", "tasks"], "files"), showsAll);
    expect(store.selectedTab()?.place).toEqual(linked.place);
  });

  it("adds only the pinned pages that fit, keeping every tab through a reload, and the rest later", async () => {
    const full: WorkspaceTab[] = Array.from({ length: MAX_WORKSPACE_TABS }, (_, index) => ({
      id: `kept-${index}`,
      place: { kind: "files", path: `src/file-${index}.ts` },
      openedBy: "user",
    }));
    const { store, save } = makeStore({
      tabs: full,
      selectedTabId: full[0].id,
      timeline: [{ tabId: full[0].id, place: full[0].place }],
      cursor: 0,
      closed: [],
    });
    store.applyTabSettings(settings(["tasks"], "tasks"), showsAll);
    expect(store.tabs()).toHaveLength(MAX_WORKSPACE_TABS);
    const saved: WorkspaceTabsSessionState = save.mock.calls.at(-1)![0];
    const reloaded = parseSessionState(JSON.parse(JSON.stringify(saved)))!;
    expect(reloaded.tabs.map((tab) => tab.id)).toContain(full.at(-1)!.id);
    // Once a tab closes there is room, and the page the strip couldn't take comes then.
    await store.close(full.at(-1)!.id);
    store.applyTabSettings(settings(["tasks"], "tasks"), showsAll);
    expect(strip(store)[0]).toBe("tasks (pinned)");
    expect(store.tabs()).toHaveLength(MAX_WORKSPACE_TABS);
  });

  it("gives the room to a newly pinned page, not to a pinned page the user closed", () => {
    const full: WorkspaceTab[] = Array.from({ length: MAX_WORKSPACE_TABS - 1 }, (_, index) => ({
      id: `kept-${index}`,
      place: { kind: "files", path: `src/file-${index}.ts` },
      openedBy: "user",
    }));
    const { store } = makeStore({
      tabs: full,
      selectedTabId: full[0].id,
      timeline: [{ tabId: full[0].id, place: full[0].place }],
      cursor: 0,
      closed: [],
      tabSettings: { pinned: ["files"], initial: "files" },
    });
    store.applyTabSettings(settings(["files", "tasks"], "files"), showsAll);
    expect(strip(store)[0]).toBe("tasks (pinned)");
    expect(store.tabs()).toHaveLength(MAX_WORKSPACE_TABS);
    expect(store.tabs().some((tab) => "place" in tab && tab.role === "files")).toBe(false);
  });

  it("gives no room to a pinned page the session can't show", () => {
    const full: WorkspaceTab[] = Array.from({ length: MAX_WORKSPACE_TABS - 1 }, (_, index) => ({
      id: `kept-${index}`,
      place: { kind: "files", path: `src/file-${index}.ts` },
      openedBy: "user",
    }));
    const { store } = makeStore({
      tabs: full,
      selectedTabId: full[0].id,
      timeline: [{ tabId: full[0].id, place: full[0].place }],
      cursor: 0,
      closed: [],
    });
    store.applyTabSettings(settings(["github", "tasks"], null), (page) => page !== "github");
    expect(strip(store)[0]).toBe("tasks (pinned)");
    expect(store.tabs()).toHaveLength(MAX_WORKSPACE_TABS);
  });
});
