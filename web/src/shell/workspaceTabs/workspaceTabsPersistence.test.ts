import { afterEach, describe, expect, it, vi } from "vitest";

import {
  MAX_SAVED_SESSIONS,
  parsePlace,
  readWorkspaceTabsSession,
  saveWorkspaceTabsSession,
  WORKSPACE_TABS_STORAGE_KEY,
  type WorkspaceTabsSessionState,
} from "./workspaceTabsPersistence";

const state: WorkspaceTabsSessionState = {
  tabs: [
    { id: "f", place: { kind: "files" }, role: "files", openedBy: "restore" },
    {
      id: "a",
      place: { kind: "files", path: "a.ts", line: { start: 3, end: 9 } },
      openedBy: "user",
    },
    {
      id: "w",
      place: { kind: "web", url: "https://example.com/" },
      binding: { kind: "browser-view", viewKey: "v1" },
      openedBy: "user",
    },
    {
      id: "p",
      place: { kind: "pull", url: "https://github.com/o/r/pull/1", view: "changes" },
      openedBy: "user",
    },
    {
      id: "q",
      place: { kind: "pull", url: "https://github.com/o/r/pull/2", view: "changes", list: true },
      openedBy: "user",
    },
    { id: "k", place: { kind: "tasks" }, openedBy: "user" },
  ],
  selectedTabId: "a",
  timeline: [
    { tabId: "f", place: { kind: "files" } },
    { tabId: "a", place: { kind: "files", path: "a.ts", line: { start: 3, end: 9 } } },
  ],
  cursor: 1,
  closed: [{ tab: { id: "c", place: { kind: "pulls" }, openedBy: "user" }, index: 1 }],
};

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("workspace tab storage", () => {
  it("restores what it saved", () => {
    expect(saveWorkspaceTabsSession("s1", state)).toBe("saved");
    expect(readWorkspaceTabsSession("s1")).toEqual({ status: "saved", state });
    expect(readWorkspaceTabsSession("s2")).toEqual({ status: "missing" });
  });

  it("restores a Changes visit's compared folder in tabs, history and closed tabs", () => {
    const rooted = {
      kind: "changes",
      comparison: { kind: "saved", intent: "head" },
      comparisonRoot: "/repo/.worktrees/feature",
    } as const;
    const withRoots: WorkspaceTabsSessionState = {
      ...state,
      tabs: [...state.tabs, { id: "d", place: rooted, openedBy: "user" }],
      timeline: [...state.timeline, { tabId: "d", place: rooted }],
      cursor: 2,
      closed: [{ tab: { id: "e", place: rooted, openedBy: "user" }, index: 0 }],
    };
    expect(saveWorkspaceTabsSession("s1", withRoots)).toBe("saved");
    expect(readWorkspaceTabsSession("s1")).toEqual({ status: "saved", state: withRoots });
  });

  it("restores the Workspace settings a strip took, and drops a malformed record", () => {
    const tabSettings = { pinned: ["files" as const, "tasks" as const], initial: "tasks" as const };
    expect(saveWorkspaceTabsSession("s1", { ...state, tabSettings })).toBe("saved");
    expect(readWorkspaceTabsSession("s1")).toEqual({
      status: "saved",
      state: { ...state, tabSettings },
    });
    expect(
      saveWorkspaceTabsSession("s2", {
        ...state,
        tabSettings: { pinned: "files", initial: null } as never,
      }),
    ).toBe("saved");
    expect(readWorkspaceTabsSession("s2")).toEqual({ status: "saved", state });
  });

  it("never overwrites a store a newer version wrote, and reads nothing from it", () => {
    const newer = JSON.stringify({ version: 2, sessions: [{ id: "s1", tabs: [] }] });
    window.localStorage.setItem(WORKSPACE_TABS_STORAGE_KEY, newer);
    expect(readWorkspaceTabsSession("s1")).toEqual({ status: "read-only" });
    expect(saveWorkspaceTabsSession("s1", state)).toBe("read-only");
    expect(window.localStorage.getItem(WORKSPACE_TABS_STORAGE_KEY)).toBe(newer);
  });

  it("keeps a tab of an unknown kind exactly as it was, across a save", () => {
    const future = { id: "x", place: { kind: "issue", id: "I-1" }, openedBy: "agent" };
    window.localStorage.setItem(
      WORKSPACE_TABS_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        sessions: [{ id: "s1", ...state, tabs: [...state.tabs, future] }],
      }),
    );
    const read = readWorkspaceTabsSession("s1");
    expect(read.status === "saved" && read.state.tabs.at(-1)).toEqual({ id: "x", unknown: future });
    if (read.status === "saved") saveWorkspaceTabsSession("s1", read.state);
    const raw = JSON.parse(window.localStorage.getItem(WORKSPACE_TABS_STORAGE_KEY)!);
    expect(raw.sessions[0].tabs.at(-1)).toEqual(future);
  });

  it("loads a Diffs tab saved before file tabs existed as it was, and keeps a file tab", () => {
    const fileTab = { kind: "changes", path: "b.go", fileTab: true } as const;
    const saved = {
      ...state,
      tabs: [
        { id: "d", place: { kind: "changes", path: "a.go" }, openedBy: "user" },
        { id: "b", place: fileTab, openedBy: "user" },
        { id: "x", place: { kind: "changes", fileTab: true }, openedBy: "user" },
      ],
      selectedTabId: "d",
      timeline: [],
      cursor: -1,
      closed: [],
    };
    window.localStorage.setItem(
      WORKSPACE_TABS_STORAGE_KEY,
      JSON.stringify({ version: 1, sessions: [{ id: "s1", ...saved }] }),
    );
    const read = readWorkspaceTabsSession("s1");
    expect(
      read.status === "saved" && read.state.tabs.map((tab) => "place" in tab && tab.place),
    ).toEqual([{ kind: "changes", path: "a.go" }, fileTab, { kind: "changes" }]);
  });

  it("keeps a Changes visit's comparison, and drops one this app can't read", () => {
    const typed = { kind: "typed", base: "v1", head: "" } as const;
    expect(parsePlace({ kind: "changes", comparison: typed })).toEqual({
      kind: "changes",
      comparison: typed,
    });
    expect(parsePlace({ kind: "changes", comparison: { kind: "saved", intent: "head" } })).toEqual({
      kind: "changes",
      comparison: { kind: "saved", intent: "head" },
    });
    expect(
      parsePlace({ kind: "changes", comparison: { kind: "saved", intent: "sideways" } }),
    ).toEqual({ kind: "changes" });
    expect(parsePlace({ kind: "changes", comparison: { kind: "typed", base: "" } })).toEqual({
      kind: "changes",
    });
  });

  it("keeps the folder a Changes visit compared, only with its comparison and absolute", () => {
    const head = { kind: "saved", intent: "head" } as const;
    const root = "/repo/.worktrees/feature";
    expect(parsePlace({ kind: "changes", comparison: head, comparisonRoot: root })).toEqual({
      kind: "changes",
      comparison: head,
      comparisonRoot: root,
    });
    expect(parsePlace({ kind: "changes", comparisonRoot: root })).toEqual({ kind: "changes" });
    // A malformed folder goes alone, and a saved spelling reads as its link would.
    for (const malformed of ["wt", "/repo\0/wt", "/../wt"]) {
      expect(parsePlace({ kind: "changes", comparison: head, comparisonRoot: malformed })).toEqual({
        kind: "changes",
        comparison: head,
      });
    }
    expect(
      parsePlace({
        kind: "changes",
        comparison: head,
        comparisonRoot: "/repo/./x/../.worktrees/feature/",
      }),
    ).toEqual({ kind: "changes", comparison: head, comparisonRoot: root });
  });

  it.each([false, "true", 1, null, {}])(
    "restores a pull request without list mode for list: %j",
    (list) => {
      expect(
        parsePlace({ kind: "pull", url: "https://github.com/o/r/pull/1", view: "changes", list }),
      ).toEqual({ kind: "pull", url: "https://github.com/o/r/pull/1", view: "changes" });
    },
  );

  it("drops malformed fields one by one and keeps the rest", () => {
    window.localStorage.setItem(
      WORKSPACE_TABS_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        sessions: [
          {
            id: "s1",
            tabs: [
              {
                id: "a",
                place: { kind: "files", path: "a.ts", line: { start: 0 } },
                openedBy: "nobody",
              },
              { id: "", place: { kind: "files" } },
              { id: "t", place: { kind: "terminal" } },
              { id: "a", place: { kind: "pulls" } },
            ],
            selectedTabId: "missing",
            timeline: [{ tabId: "gone", place: { kind: "files" } }, "junk"],
            cursor: "x",
            closed: [{ index: "x" }],
          },
        ],
      }),
    );
    expect(readWorkspaceTabsSession("s1")).toEqual({
      status: "saved",
      state: {
        tabs: [{ id: "a", place: { kind: "files", path: "a.ts" }, openedBy: "restore" }],
        selectedTabId: null,
        timeline: [],
        cursor: -1,
        closed: [],
      },
    });
  });

  it("keeps the history cursor on its own visit when an earlier visit is dropped", () => {
    window.localStorage.setItem(
      WORKSPACE_TABS_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        sessions: [
          {
            ...state,
            id: "s1",
            // The first visit's tab is gone; the cursor is on the Files visit after it.
            timeline: [
              { tabId: "gone", place: { kind: "files" } },
              { tabId: "f", place: { kind: "files" } },
              { tabId: "k", place: { kind: "tasks" } },
            ],
            cursor: 1,
          },
        ],
      }),
    );
    const read = readWorkspaceTabsSession("s1");
    if (read.status !== "saved") throw new Error(read.status);
    expect(read.state.timeline.map((visit) => visit.tabId)).toEqual(["f", "k"]);
    expect(read.state.timeline[read.state.cursor]?.tabId).toBe("f");
  });

  it("keeps the most recently saved sessions", () => {
    for (let index = 0; index <= MAX_SAVED_SESSIONS; index += 1) {
      saveWorkspaceTabsSession(`s${index}`, state);
    }
    expect(readWorkspaceTabsSession("s0")).toEqual({ status: "missing" });
    expect(readWorkspaceTabsSession(`s${MAX_SAVED_SESSIONS}`).status).toBe("saved");
  });

  it("reports a refused write", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(saveWorkspaceTabsSession("s1", state)).toBe("failed");
  });
});
