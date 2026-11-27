import { describe, expect, it } from "vitest";

import {
  NO_SUCH_TAB_REASON,
  TAB_CAP_REASON,
  canShow,
  planOpen,
  type OpenOptions,
  type OpenState,
  type PlanTab,
  type ResourceState,
} from "./planOpen";
import type { WorkspaceLink, WorkspacePlace } from "./workspacePlace";

const files: PlanTab = { id: "F", place: { kind: "files" }, role: "files", dirty: false };
const changes: PlanTab = { id: "C", place: { kind: "changes" }, dirty: false };
const pullTab: PlanTab = {
  id: "P",
  place: { kind: "pull", url: "https://github.com/o/r/pull/12", view: "summary" },
  dirty: false,
};
const fileTab: PlanTab = { id: "A", place: { kind: "files", path: "a.ts" }, dirty: false };
const shell: PlanTab = {
  id: "T",
  place: { kind: "terminal", key: "k1" },
  binding: { kind: "terminal", key: "k1" },
  dirty: false,
};
const browser: PlanTab = {
  id: "W",
  place: { kind: "web", url: "https://example.com/" },
  binding: { kind: "browser-view", viewKey: "v1" },
  dirty: false,
};

function state(
  tabs: PlanTab[],
  selected: string | null,
  overrides: Partial<OpenState> = {},
): OpenState {
  return {
    sessionId: "s1",
    tabs,
    selectedTabId: selected,
    recentlySelected: selected ? [selected] : [],
    resourceState: () => "live",
    maxTabs: 200,
    ...overrides,
  };
}

const user: OpenOptions = { opener: "user" };
const agent: OpenOptions = { opener: "agent" };

function plan(
  snapshot: OpenState,
  place: WorkspacePlace,
  options: OpenOptions = user,
  extra: Partial<WorkspaceLink> = {},
) {
  return planOpen(snapshot, { place, ...extra }, options);
}

describe("planOpen", () => {
  describe("sessions and explicit tabs", () => {
    it("hands another session's link to that session, switching only for foreground opens", () => {
      const link: WorkspaceLink = { place: { kind: "pulls" }, session: "s2", tab: "X" };
      expect(planOpen(state([], null), link, user)).toEqual({
        ok: true,
        plan: {
          action: "other-session",
          sessionId: "s2",
          switchSession: true,
          link: { place: { kind: "pulls" }, tab: "X" },
          options: { opener: "user", disposition: "auto" },
        },
      });
      const background = planOpen(state([], null), link, agent);
      expect(
        background.ok &&
          background.plan.action === "other-session" &&
          background.plan.switchSession,
      ).toBe(false);
      expect(plan(state([files], "F"), { kind: "pulls" }, user, { session: "s1" })).toMatchObject({
        ok: true,
        plan: { action: "create" },
      });
    });

    it("refuses a missing tab and changes nothing", () => {
      expect(
        plan(state([files], "F"), { kind: "files", path: "b.ts" }, user, { tab: "nope" }),
      ).toEqual({
        ok: false,
        reason: NO_SUCH_TAB_REASON,
      });
    });

    it("refuses a tab that can't show the place", () => {
      expect(
        plan(state([files, shell], "F"), { kind: "files", path: "b.ts" }, user, { tab: "T" }),
      ).toEqual({
        ok: false,
        reason: "That tab shows a shell.",
      });
      expect(
        plan(state([files, shell], "F"), { kind: "terminal", key: "k1" }, user, { tab: "F" }),
      ).toEqual({
        ok: false,
        reason: "That tab can't show this.",
      });
      expect(
        plan(state([files], "F"), { kind: "web", url: "https://a.example/" }, user, { tab: "F" }),
      ).toEqual({
        ok: false,
        reason: "Web pages open in a browser tab.",
      });
    });

    it("navigates a named tab, selecting it for the user", () => {
      expect(
        plan(state([files, fileTab], "F"), { kind: "files", path: "b.ts" }, user, { tab: "A" }),
      ).toEqual({
        ok: true,
        plan: {
          action: "navigate",
          tabId: "A",
          place: { kind: "files", path: "b.ts" },
          select: true,
          guard: [],
          history: "append",
        },
      });
    });

    it("lets an agent navigate a named unselected, clean file tab silently", () => {
      expect(
        plan(state([files, fileTab, pullTab], "P"), { kind: "files", path: "b.ts" }, agent, {
          tab: "A",
        }),
      ).toEqual({
        ok: true,
        plan: {
          action: "navigate",
          tabId: "A",
          place: { kind: "files", path: "b.ts" },
          select: false,
          guard: [],
          history: "none",
        },
      });
    });

    it("never lets an agent move the Files tab, even by name", () => {
      expect(
        plan(state([files, pullTab], "P"), { kind: "files", path: "b.ts" }, agent, { tab: "F" }),
      ).toEqual({
        ok: true,
        plan: { action: "attention", tabId: "F", place: { kind: "files", path: "b.ts" } },
      });
      expect(
        plan(state([files, pullTab], "P"), { kind: "files" }, agent, { tab: "F" }),
      ).toMatchObject({
        plan: { action: "navigate", tabId: "F", select: false, history: "none" },
      });
    });

    it("turns an agent's open into attention on a selected or dirty named tab", () => {
      expect(
        plan(state([files, pullTab], "F"), { kind: "files", path: "b.ts" }, agent, { tab: "F" }),
      ).toEqual({
        ok: true,
        plan: { action: "attention", tabId: "F", place: { kind: "files", path: "b.ts" } },
      });
      const dirty = { ...fileTab, dirty: true };
      expect(
        plan(state([files, dirty], "F"), { kind: "files", path: "b.ts" }, agent, { tab: "A" }),
      ).toMatchObject({
        plan: { action: "attention", tabId: "A" },
      });
    });
  });

  describe("resources", () => {
    it.each<[WorkspacePlace, string]>([
      [{ kind: "terminal", key: "k9" }, "That shell has ended."],
      [{ kind: "agent", id: "x" }, "That agent is no longer available."],
      [{ kind: "chat", id: "c" }, "That side chat is no longer available."],
    ])("refuses a gone %o", (place, reason) => {
      expect(plan(state([files], "F", { resourceState: () => "gone" }), place)).toEqual({
        ok: false,
        reason,
      });
    });

    it("opens a resource that is still loading", () => {
      const unknown = state([files], "F", { resourceState: (): ResourceState => "unknown" });
      expect(plan(unknown, { kind: "terminal", key: "k9" })).toMatchObject({
        ok: true,
        plan: { action: "create", binding: { kind: "terminal", key: "k9" } },
      });
    });

    it("selects the bound tab of an open shell", () => {
      expect(plan(state([files, shell], "F"), { kind: "terminal", key: "k1" })).toMatchObject({
        plan: { action: "navigate", tabId: "T", select: true, history: "append" },
      });
    });

    it("always opens a new draft side chat", () => {
      const draft: PlanTab = {
        id: "D",
        place: { kind: "chat" },
        binding: { kind: "chat" },
        dirty: false,
      };
      expect(plan(state([files, draft], "D"), { kind: "chat" })).toMatchObject({
        plan: { action: "create", binding: { kind: "chat" } },
      });
    });
  });

  describe("dispositions", () => {
    describe("focus", () => {
      const focus: OpenOptions = { opener: "user", disposition: "focus" };
      const pulls: PlanTab = { id: "L", place: { kind: "pulls" }, dirty: false };
      const commits: PlanTab = {
        ...pullTab,
        place: { kind: "pull", url: "https://github.com/o/r/pull/12", view: "commits" },
      };
      const summary: WorkspacePlace = {
        kind: "pull",
        url: "https://github.com/o/r/pull/12",
        view: "summary",
      };

      it("selects the place's tab as it is, keeping its sub-view: one step", () => {
        expect(plan(state([pulls, commits], "L"), summary, focus)).toEqual({
          ok: true,
          plan: {
            action: "navigate",
            tabId: "P",
            place: commits.place,
            select: true,
            guard: [],
            history: "append",
          },
        });
      });

      it("is no step when the place's tab is already selected", () => {
        expect(plan(state([pulls, commits], "P"), summary, focus)).toMatchObject({
          ok: true,
          plan: { action: "navigate", tabId: "P", place: commits.place, history: "none" },
        });
        expect(plan(state([pulls, commits], "L"), { kind: "pulls" }, focus)).toMatchObject({
          ok: true,
          plan: { action: "navigate", tabId: "L", history: "none" },
        });
      });

      it("opens a new tab after the selected one when none shows the place", () => {
        expect(plan(state([files, pullTab], "F"), { kind: "pulls" }, focus)).toEqual({
          ok: true,
          plan: {
            action: "create",
            place: { kind: "pulls" },
            after: "F",
            select: true,
            history: "append",
          },
        });
      });

      it("with keepSelection, opens the place behind the current tab, or leaves its tab as it is", () => {
        const behind: OpenOptions = { ...focus, keepSelection: true };
        expect(plan(state([files, pulls], "L"), summary, behind)).toEqual({
          ok: true,
          plan: {
            action: "create",
            place: summary,
            after: "L",
            select: false,
            history: "none",
          },
        });
        expect(plan(state([pulls, commits], "L"), summary, behind)).toEqual({
          ok: true,
          plan: {
            action: "navigate",
            tabId: "P",
            place: commits.place,
            select: false,
            guard: [],
            history: "none",
          },
        });
      });

      describe("with same-place reuse", () => {
        const url = "https://github.com/o/r/pull/12";
        const commit: WorkspacePlace = { kind: "pull", url, view: "commits", commit: "abc1234" };
        const file: WorkspacePlace = { kind: "pull", url, view: "changes", path: "a.ts" };
        const commitTab: PlanTab = { id: "K", place: commit, dirty: false };
        const behind: OpenOptions = { ...focus, reuse: "same-place", keepSelection: true };

        it("opens a commit of the selected pull request in a new tab behind it", () => {
          expect(plan(state([pullTab, files], "P"), commit, behind)).toEqual({
            ok: true,
            plan: { action: "create", place: commit, after: "P", select: false, history: "none" },
          });
        });

        it("reuses only a tab showing exactly that place, left as it is", () => {
          expect(plan(state([pullTab, commitTab], "P"), commit, behind)).toEqual({
            ok: true,
            plan: {
              action: "navigate",
              tabId: "K",
              place: commit,
              select: false,
              guard: [],
              history: "none",
            },
          });
          const other: WorkspacePlace = { ...commit, commit: "def5678" };
          expect(plan(state([pullTab, commitTab], "P"), other, behind)).toMatchObject({
            ok: true,
            plan: { action: "create", place: other, after: "P", select: false },
          });
          expect(plan(state([pullTab, commitTab], "P"), file, behind)).toMatchObject({
            ok: true,
            plan: { action: "create", place: file, after: "P" },
          });
        });

        it("never counts the tab you're in, even when it shows that place", () => {
          expect(plan(state([commitTab, pullTab], "K"), commit, behind)).toEqual({
            ok: true,
            plan: { action: "create", place: commit, after: "K", select: false, history: "none" },
          });
          const copy: PlanTab = { ...commitTab, id: "K2" };
          expect(plan(state([commitTab, copy], "K"), commit, behind)).toMatchObject({
            ok: true,
            plan: { action: "navigate", tabId: "K2", select: false },
          });
        });

        it("switches to the place's tab without keepSelection", () => {
          const front: OpenOptions = { ...focus, reuse: "same-place" };
          expect(plan(state([pullTab], "P"), file, front)).toEqual({
            ok: true,
            plan: { action: "create", place: file, after: "P", select: true, history: "append" },
          });
          expect(plan(state([pullTab, commitTab], "P"), commit, front)).toMatchObject({
            ok: true,
            plan: { action: "navigate", tabId: "K", select: true, history: "append" },
          });
        });

        it("reuses a matching tab at the cap, past a dirty unrelated tab, and refuses a new one", () => {
          const dirty: PlanTab = { ...fileTab, dirty: true };
          const full = state([pullTab, dirty, commitTab], "P", { maxTabs: 3 });
          expect(plan(full, commit, behind)).toMatchObject({
            ok: true,
            plan: { action: "navigate", tabId: "K", guard: [], select: false },
          });
          expect(plan(full, file, behind)).toEqual({ ok: false, reason: TAB_CAP_REASON });
        });
      });

      it("reuses a Diffs file tab whatever comparison its visit recorded, and drops a stale one", () => {
        const diffFile: WorkspacePlace = { kind: "changes", path: "a.go", fileTab: true };
        const shown: PlanTab = {
          id: "D",
          place: { ...diffFile, comparison: { kind: "saved", intent: "branch-point" } },
          dirty: false,
        };
        const reuse: OpenOptions = { ...focus, reuse: "same-place" };
        // Bringing it forward keeps the session's comparison, unless the open asks for one.
        expect(plan(state([changes, shown], "C"), diffFile, reuse)).toEqual({
          ok: true,
          plan: expect.objectContaining({ action: "navigate", tabId: "D", place: diffFile }),
        });
        const head = { ...diffFile, comparison: { kind: "saved", intent: "head" } } as const;
        expect(plan(state([changes, shown], "C"), head, reuse)).toEqual({
          ok: true,
          plan: expect.objectContaining({ action: "navigate", tabId: "D", place: head }),
        });
        // The folder that showed a comparison travels with it, and a stale one goes.
        const inWorktree = { ...head, comparisonRoot: "/repo/.worktrees/feature" } as const;
        const stale: PlanTab = {
          ...shown,
          place: {
            ...diffFile,
            comparison: { kind: "saved", intent: "branch-point" },
            comparisonRoot: "/repo/.worktrees/other",
          },
        };
        for (const [tab, asked] of [
          [shown, inWorktree],
          [stale, inWorktree],
          [stale, head],
          [stale, diffFile],
        ] as const) {
          expect(plan(state([changes, tab], "C"), asked, reuse)).toEqual({
            ok: true,
            plan: expect.objectContaining({ action: "navigate", tabId: "D", place: asked }),
          });
        }
      });

      it("refuses at the tab cap", () => {
        expect(plan(state([files], "F", { maxTabs: 1 }), { kind: "pulls" }, focus)).toEqual({
          ok: false,
          reason: TAB_CAP_REASON,
        });
      });

      it("leaves an agent's open to the background rules", () => {
        expect(
          plan(state([pulls, commits], "P"), summary, { opener: "agent", disposition: "focus" }),
        ).toMatchObject({ ok: true, plan: { action: "attention", tabId: "P", place: summary } });
      });

      it("keeps a link's navigation to the sub-view it names", () => {
        expect(plan(state([pulls, commits], "L"), summary, { opener: "link" })).toMatchObject({
          ok: true,
          plan: { action: "navigate", tabId: "P", place: summary },
        });
      });
    });

    it("new skips reuse and inserts after the selected tab", () => {
      expect(
        plan(
          state([files, fileTab, changes], "A"),
          { kind: "files", path: "a.ts" },
          { opener: "user", disposition: "new" },
        ),
      ).toEqual({
        ok: true,
        plan: {
          action: "create",
          place: { kind: "files", path: "a.ts" },
          after: "A",
          select: true,
          history: "append",
        },
      });
    });

    it("current navigates the selected tab and runs the guard when it is dirty", () => {
      const dirty = { ...fileTab, dirty: true };
      expect(
        plan(
          state([files, dirty], "A"),
          { kind: "pulls" },
          { opener: "link", disposition: "current" },
        ),
      ).toEqual({
        ok: true,
        plan: {
          action: "navigate",
          tabId: "A",
          place: { kind: "pulls" },
          select: true,
          guard: ["A"],
          history: "append",
        },
      });
    });

    it("current never converts a binding; it opens a new tab instead (trace 9)", () => {
      expect(
        plan(
          state([files], "F"),
          { kind: "terminal", key: "k1" },
          { opener: "user", disposition: "current" },
        ),
      ).toMatchObject({
        plan: { action: "create", binding: { kind: "terminal", key: "k1" }, after: "F" },
      });
      expect(
        plan(
          state([files, shell], "T"),
          { kind: "pulls" },
          { opener: "user", disposition: "current" },
        ),
      ).toMatchObject({
        plan: { action: "create", place: { kind: "pulls" } },
      });
    });

    it('adds a tab at the far end for position end, as "+" does, and after the selected tab otherwise', () => {
      const snapshot = state([files, fileTab, changes], "F");
      expect(
        plan(snapshot, { kind: "new" }, { opener: "user", disposition: "new", position: "end" }),
      ).toMatchObject({ plan: { action: "create", after: null } });
      expect(plan(snapshot, { kind: "new" }, { opener: "user", disposition: "new" })).toMatchObject(
        {
          plan: { action: "create", after: "F" },
        },
      );
    });

    it("refuses to create past the tab cap", () => {
      expect(plan(state([files], "F", { maxTabs: 1 }), { kind: "pulls" })).toEqual({
        ok: false,
        reason: TAB_CAP_REASON,
      });
    });
  });

  describe("opening files (navigate in place)", () => {
    it("navigates the Files tab in place from its own tree", () => {
      expect(
        plan(
          state([files, changes], "F"),
          { kind: "files", path: "b.ts" },
          { opener: "user", origin: "files-tree" },
        ),
      ).toEqual({
        ok: true,
        plan: {
          action: "navigate",
          tabId: "F",
          place: { kind: "files", path: "b.ts" },
          select: true,
          guard: [],
          history: "append",
        },
      });
    });

    it("asks before a tree click replaces a dirty file in the Files tab", () => {
      const dirtyFiles = {
        ...files,
        place: { kind: "files", path: "a.ts" } as WorkspacePlace,
        dirty: true,
      };
      expect(
        plan(
          state([dirtyFiles], "F"),
          { kind: "files", path: "b.ts" },
          { opener: "user", origin: "files-tree" },
        ),
      ).toMatchObject({
        plan: { action: "navigate", tabId: "F", guard: ["F"] },
      });
    });

    it("reuses a tab already showing the file, preferring the selected then the most recent", () => {
      const other: PlanTab = { id: "A2", place: { kind: "files", path: "a.ts" }, dirty: false };
      const snapshot = state([files, fileTab, other, changes], "C", {
        recentlySelected: ["C", "A2", "A"],
      });
      expect(plan(snapshot, { kind: "files", path: "a.ts", line: { start: 3 } })).toMatchObject({
        plan: { action: "navigate", tabId: "A2", select: true, history: "append" },
      });
      expect(
        plan({ ...snapshot, selectedTabId: "A" }, { kind: "files", path: "a.ts" }),
      ).toMatchObject({
        plan: { action: "navigate", tabId: "A", history: "none" },
      });
    });

    it("records a line move in the selected file as a replacement", () => {
      const at10 = {
        ...fileTab,
        place: { kind: "files", path: "a.ts", line: { start: 10 } } as WorkspacePlace,
      };
      expect(
        plan(state([files, at10], "A"), { kind: "files", path: "a.ts", line: { start: 40 } }),
      ).toMatchObject({
        plan: { action: "navigate", tabId: "A", guard: [], history: "replace" },
      });
    });

    it("asks before any move of a dirty file, even to a line in the same file", () => {
      const dirty = {
        ...fileTab,
        place: { kind: "files", path: "a.md" } as WorkspacePlace,
        dirty: true,
      };
      expect(
        plan(state([files, dirty], "A"), { kind: "files", path: "a.md", line: { start: 5 } }),
      ).toMatchObject({
        plan: { action: "navigate", tabId: "A", guard: ["A"], history: "replace" },
      });
      expect(
        plan(
          state([files, dirty], "F"),
          { kind: "files", path: "a.md", line: { start: 5 } },
          agent,
        ),
      ).toEqual({
        ok: true,
        plan: {
          action: "attention",
          tabId: "A",
          place: { kind: "files", path: "a.md", line: { start: 5 } },
        },
      });
    });

    it("compares places by meaning, not property order", () => {
      const dirty: PlanTab = {
        id: "A",
        place: { kind: "files", path: "a.md", line: { start: 2, end: 4 } },
        dirty: true,
      };
      const reordered = {
        line: { end: 4, start: 2 },
        path: "a.md",
        kind: "files",
      } as WorkspacePlace;
      expect(plan(state([files, dirty], "A"), reordered)).toEqual({
        ok: true,
        plan: {
          action: "navigate",
          tabId: "A",
          place: reordered,
          select: true,
          guard: [],
          history: "none",
        },
      });
      const moved = { line: { start: 9 }, path: "a.md", kind: "files" } as WorkspacePlace;
      expect(plan(state([files, { ...dirty, dirty: false }], "A"), moved)).toMatchObject({
        plan: { history: "replace", guard: [] },
      });
    });

    it("navigates the selected Files tab when no tab shows the file, else opens a new file tab", () => {
      expect(plan(state([files, changes], "F"), { kind: "files", path: "b.ts" })).toMatchObject({
        plan: { action: "navigate", tabId: "F", select: true },
      });
      expect(plan(state([files, changes], "C"), { kind: "files", path: "b.ts" })).toMatchObject({
        plan: {
          action: "create",
          place: { kind: "files", path: "b.ts" },
          after: "C",
          select: true,
        },
      });
    });

    it("selects the Files tab for the tree and creates one when missing", () => {
      const showingFile = { ...files, place: { kind: "files", path: "a.ts" } as WorkspacePlace };
      expect(plan(state([showingFile, changes], "C"), { kind: "files" })).toMatchObject({
        plan: { action: "navigate", tabId: "F", place: { kind: "files" } },
      });
      expect(plan(state([changes], "C"), { kind: "files" })).toMatchObject({
        plan: { action: "create", role: "files" },
      });
      expect(
        plan(
          state([changes], "C"),
          { kind: "files", path: "a.ts" },
          { opener: "user", origin: "files-tree" },
        ),
      ).toMatchObject({
        plan: { action: "create", role: "files" },
      });
    });

    it("never moves the Files tab for an agent", () => {
      const showingFile = { ...files, place: { kind: "files", path: "a.ts" } as WorkspacePlace };
      expect(
        plan(
          state([showingFile, changes], "C"),
          { kind: "files", path: "a.ts", line: { start: 9 } },
          agent,
        ),
      ).toEqual({
        ok: true,
        plan: {
          action: "attention",
          tabId: "F",
          place: { kind: "files", path: "a.ts", line: { start: 9 } },
        },
      });
      expect(plan(state([files, changes], "F"), { kind: "files", path: "b.ts" }, agent)).toEqual({
        ok: true,
        plan: {
          action: "create",
          place: { kind: "files", path: "b.ts" },
          after: "F",
          select: false,
          history: "none",
        },
      });
      expect(
        plan(
          state([files], "F"),
          { kind: "files", path: "b.ts" },
          { opener: "agent", origin: "files-tree" },
        ),
      ).toMatchObject({
        plan: { action: "create", select: false },
      });
    });
  });

  describe("reuse by identity", () => {
    it("moves an open pull request tab to the new sub-view", () => {
      const place: WorkspacePlace = {
        kind: "pull",
        url: "https://github.com/o/r/pull/12",
        view: "changes",
        path: "a.ts",
      };
      expect(plan(state([files, pullTab], "F"), place)).toMatchObject({
        plan: { action: "navigate", tabId: "P", place },
      });
    });

    it("moves the Changes cursor and replaces the search query", () => {
      expect(plan(state([files, changes], "C"), { kind: "changes", path: "x.go" })).toMatchObject({
        plan: { action: "navigate", tabId: "C", history: "append" },
      });
      const search: PlanTab = { id: "S", place: { kind: "search", query: "a" }, dirty: false };
      expect(plan(state([search], "S"), { kind: "search", query: "b" })).toMatchObject({
        plan: { action: "navigate", tabId: "S" },
      });
    });

    it("reuses a browser tab on the same page, else opens a new one", () => {
      expect(
        plan(state([files, browser], "F"), { kind: "web", url: "https://example.com/" }),
      ).toMatchObject({
        plan: { action: "navigate", tabId: "W" },
      });
      expect(
        plan(state([files, browser], "F"), { kind: "web", url: "https://other.example/" }),
      ).toMatchObject({
        plan: { action: "create", binding: { kind: "browser-view" } },
      });
      expect(plan(state([files, browser], "W"), { kind: "web" })).toMatchObject({
        plan: { action: "create" },
      });
    });

    it("always creates a new-tab page", () => {
      expect(plan(state([files], "F"), { kind: "new" })).toMatchObject({
        plan: { action: "create", place: { kind: "new" } },
      });
    });

    it("navigates an agent's reuse silently only when the tab is unselected and clean", () => {
      expect(
        plan(
          state([files, pullTab], "F"),
          { ...pullTab.place, view: "commits" } as WorkspacePlace,
          agent,
        ),
      ).toMatchObject({
        plan: { action: "navigate", tabId: "P", select: false, history: "none" },
      });
      expect(
        plan(
          state([files, pullTab], "P"),
          { ...pullTab.place, view: "commits" } as WorkspacePlace,
          agent,
        ),
      ).toMatchObject({
        plan: { action: "attention", tabId: "P" },
      });
    });
  });
});

describe("canShow", () => {
  it.each<[PlanTab, WorkspacePlace, boolean]>([
    [files, { kind: "pulls" }, true],
    [files, { kind: "terminal", key: "k1" }, false],
    [shell, { kind: "terminal", key: "k1" }, true],
    [shell, { kind: "terminal", key: "k2" }, false],
    [browser, { kind: "web" }, true],
    [browser, { kind: "files" }, false],
    [
      { id: "D", place: { kind: "chat" }, binding: { kind: "chat" }, dirty: false },
      { kind: "chat", id: "c" },
      false,
    ],
    [
      {
        id: "D",
        place: { kind: "chat", id: "c" },
        binding: { kind: "chat", id: "c" },
        dirty: false,
      },
      { kind: "chat", id: "c" },
      true,
    ],
  ])("%#", (tab, place, expected) => {
    expect(canShow(tab, place)).toBe(expected);
  });
});
