import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_SAVED_VIEW_FILTERS,
  deleteView,
  matchingSavedView,
  parseSavedView,
  readSavedViews,
  SAVED_VIEW_LIMIT,
  type SavedViewSettings,
  savedViewFilterFields,
  savedViewListFilter,
  saveView,
  useSidebarSavedViews,
} from "./sidebarSavedViews";

const STORAGE_KEY = "omnigent:sidebar-saved-views";
const SCOPE = { server: "https://example.com", userId: "user-1" };
const SETTINGS: SavedViewSettings = {
  grouping: "status",
  ordering: "created",
  show: ["repo", "harness"],
  sessionFilter: "all",
  pullRequestFilter: "open",
  statusFilter: "all",
  harnessFilter: null,
  projectFilter: { kind: "all" },
};
const stored = () => JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
const scopeKey = `${SCOPE.server}\n${SCOPE.userId}`;

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("parseSavedView", () => {
  it("reads each setting on its own, falling back for what this build doesn't know", () => {
    expect(
      parseSavedView({
        id: "v1",
        name: "  Review  ",
        grouping: "starred",
        ordering: "custom",
        show: ["branch", "colour", "harness"],
        sessionFilter: "mine",
        pullRequestFilter: "stale",
      }),
    ).toEqual({
      id: "v1",
      name: "Review",
      grouping: "default",
      ordering: "updated",
      show: ["harness", "branch"],
      sessionFilter: "mine",
      pullRequestFilter: "all",
      // A view saved before these filters existed reads with each at All.
      statusFilter: "all",
      harnessFilter: null,
      projectFilter: { kind: "all" },
    });
  });

  it("reads the status, harness and project filters, keeping a harness or project that's gone", () => {
    const view = parseSavedView({
      id: "v1",
      name: "Gone",
      statusFilter: "working",
      harnessFilter: "acp:retired",
      projectFilter: { kind: "project", id: "deleted-project" },
    });
    expect(view && savedViewListFilter(view)).toEqual({
      pullRequest: "all",
      status: "working",
      harness: "acp:retired",
      project: { kind: "project", id: "deleted-project" },
    });
    expect(parseSavedView({ id: "v2", name: "Odd", statusFilter: "asleep" })?.statusFilter).toBe(
      "all",
    );
  });

  it("can't read a view without an id or a name", () => {
    for (const entry of [null, "v1", { name: "x" }, { id: "v1", name: " " }, { id: "", name: "x" }])
      expect(parseSavedView(entry)).toBeNull();
  });
});

describe("saveView", () => {
  it("saves a view under its trimmed name, read back in the order saved", () => {
    const first = saveView(SCOPE, "  Review ", SETTINGS);
    const second = saveView(SCOPE, "Mine", { ...SETTINGS, sessionFilter: "mine" });
    expect(first).toMatchObject({ ok: true, view: { name: "Review", ...SETTINGS } });
    expect(second.ok).toBe(true);
    expect(readSavedViews(SCOPE).map((view) => view.name)).toEqual(["Review", "Mine"]);
  });

  it("saves a saved view's settings under a new name and id, never its own", () => {
    const first = saveView(SCOPE, "Review", SETTINGS);
    if (!first.ok) throw new Error("not saved");
    const second = saveView(SCOPE, "Copy", first.view);
    if (!second.ok) throw new Error("not saved");
    expect(second.view.name).toBe("Copy");
    expect(second.view.id).not.toBe(first.view.id);
    expect(readSavedViews(SCOPE).map((view) => view.name)).toEqual(["Review", "Copy"]);
    // Only the settings a view keeps are stored, whatever else the object carries.
    const extra = { ...SETTINGS, label: "stray" } as typeof SETTINGS;
    const third = saveView(SCOPE, "Third", extra);
    expect(third.ok && Object.keys(third.view).sort()).toEqual(
      ["id", "name", ...Object.keys(SETTINGS)].sort(),
    );
  });

  it("refuses an empty name or one already taken, ignoring case", () => {
    saveView(SCOPE, "Review", SETTINGS);
    expect(saveView(SCOPE, "   ", SETTINGS)).toEqual({ ok: false, problem: "empty" });
    expect(saveView(SCOPE, " review ", SETTINGS)).toEqual({ ok: false, problem: "duplicate" });
  });

  it("refuses a view past the limit, and never drops an old one", () => {
    for (let index = 0; index < SAVED_VIEW_LIMIT; index++)
      expect(saveView(SCOPE, `View ${index}`, SETTINGS).ok).toBe(true);
    expect(saveView(SCOPE, "One more", SETTINGS)).toEqual({ ok: false, problem: "limit" });
    expect(readSavedViews(SCOPE)).toHaveLength(SAVED_VIEW_LIMIT);
    expect(readSavedViews(SCOPE)[0]?.name).toBe("View 0");
  });

  it("says so when storage refuses the view", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    expect(saveView(SCOPE, "Review", SETTINGS)).toEqual({ ok: false, problem: "storage" });
  });

  it("keeps each server's and user's views apart", () => {
    const otherUser = { ...SCOPE, userId: "user-2" };
    const otherServer = { ...SCOPE, server: "https://example.org" };
    saveView(SCOPE, "Review", SETTINGS);
    saveView(otherUser, "Theirs", SETTINGS);
    expect(saveView(otherServer, "Review", SETTINGS).ok).toBe(true);
    expect(readSavedViews(SCOPE).map((view) => view.name)).toEqual(["Review"]);
    expect(readSavedViews(otherUser).map((view) => view.name)).toEqual(["Theirs"]);
    expect(readSavedViews(otherServer).map((view) => view.name)).toEqual(["Review"]);
  });

  it("keeps a stored entry it can't read through a save and a delete, and counts it", () => {
    const unreadable = { kind: "a later build's view" };
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ [scopeKey]: [unreadable] }));
    const saved = saveView(SCOPE, "Review", SETTINGS);
    expect(stored()[scopeKey][0]).toEqual(unreadable);
    if (!saved.ok) throw new Error("not saved");
    deleteView(SCOPE, saved.view.id);
    expect(stored()[scopeKey]).toEqual([unreadable]);
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ [scopeKey]: Array.from({ length: SAVED_VIEW_LIMIT }, () => unreadable) }),
    );
    expect(saveView(SCOPE, "Review", SETTINGS)).toEqual({ ok: false, problem: "limit" });
  });
});

describe("storage it can't read", () => {
  it.each([
    ["isn't JSON", "{not json"],
    ["isn't an object", "[1, 2]"],
    ["holds a scope that isn't a list", JSON.stringify({ [scopeKey]: { kind: "later" } })],
  ])("changes nothing when it %s", (_, raw) => {
    localStorage.setItem(STORAGE_KEY, raw);
    expect(saveView(SCOPE, "Review", SETTINGS)).toEqual({ ok: false, problem: "storage" });
    expect(readSavedViews(SCOPE)).toEqual([]);
    expect(localStorage.getItem(STORAGE_KEY)).toBe(raw);
  });

  it("changes nothing when storage can't be read at all", () => {
    saveView(SCOPE, "Review", SETTINGS);
    const before = localStorage.getItem(STORAGE_KEY);
    const id = readSavedViews(SCOPE)[0]!.id;
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    expect(saveView(SCOPE, "Other", SETTINGS)).toEqual({ ok: false, problem: "storage" });
    expect(deleteView(SCOPE, id)).toBeNull();
    expect(setItem).not.toHaveBeenCalled();
    getItem.mockRestore();
    expect(localStorage.getItem(STORAGE_KEY)).toBe(before);
  });

  it("keeps another scope's value it can't read through a write", () => {
    const other = "https://example.org\nuser-9";
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ [other]: { kind: "later" } }));
    expect(saveView(SCOPE, "Review", SETTINGS).ok).toBe(true);
    expect(stored()[other]).toEqual({ kind: "later" });
  });
});

describe("deleteView", () => {
  it("deletes a view, and its undo puts it back where it was, once", () => {
    const ids = ["A", "B", "C"].map((name) => {
      const result = saveView(SCOPE, name, SETTINGS);
      if (!result.ok) throw new Error("not saved");
      return result.view.id;
    });
    const restore = deleteView(SCOPE, ids[1]!);
    expect(readSavedViews(SCOPE).map((view) => view.name)).toEqual(["A", "C"]);
    expect(restore?.()).toEqual({ ok: true });
    expect(restore?.()).toEqual({ ok: true });
    expect(readSavedViews(SCOPE).map((view) => view.name)).toEqual(["A", "B", "C"]);
  });

  it("puts a view back beside its surviving neighbours, whatever else was deleted", () => {
    const ids = ["A", "B", "C"].map((name) => {
      const result = saveView(SCOPE, name, SETTINGS);
      if (!result.ok) throw new Error("not saved");
      return result.view.id;
    });
    const restoreB = deleteView(SCOPE, ids[1]!);
    const restoreA = deleteView(SCOPE, ids[0]!);
    expect(restoreB?.()).toEqual({ ok: true });
    expect(readSavedViews(SCOPE).map((view) => view.name)).toEqual(["B", "C"]);
    // A went from [A, C], so it comes back before C, its neighbour then.
    expect(restoreA?.()).toEqual({ ok: true });
    expect(readSavedViews(SCOPE).map((view) => view.name)).toEqual(["B", "A", "C"]);
  });

  it("keeps the last view in place when its predecessor alone survives", () => {
    const ids = ["A", "B", "C"].map((name) => {
      const result = saveView(SCOPE, name, SETTINGS);
      if (!result.ok) throw new Error("not saved");
      return result.view.id;
    });
    const restoreC = deleteView(SCOPE, ids[2]!);
    const restoreB = deleteView(SCOPE, ids[1]!);
    expect(restoreC?.()).toEqual({ ok: true });
    expect(restoreB?.()).toEqual({ ok: true });
    expect(readSavedViews(SCOPE).map((view) => view.name)).toEqual(["A", "B", "C"]);
  });

  it("refuses to put a view back past the limit, dropping none", () => {
    const ids = Array.from({ length: SAVED_VIEW_LIMIT }, (_, index) => {
      const result = saveView(SCOPE, `View ${index}`, SETTINGS);
      if (!result.ok) throw new Error("not saved");
      return result.view.id;
    });
    const restore = deleteView(SCOPE, ids[3]!);
    expect(saveView(SCOPE, "Another", SETTINGS).ok).toBe(true);
    expect(restore?.()).toEqual({ ok: false, problem: "limit" });
    expect(readSavedViews(SCOPE)).toHaveLength(SAVED_VIEW_LIMIT);
    expect(readSavedViews(SCOPE).map((view) => view.name)).toContain("Another");
  });

  it("refuses to put a view back once its name is taken, ignoring case", () => {
    const saved = saveView(SCOPE, "Review", SETTINGS);
    if (!saved.ok) throw new Error("not saved");
    const restore = deleteView(SCOPE, saved.view.id);
    saveView(SCOPE, "review", { ...SETTINGS, pullRequestFilter: "none" });
    expect(restore?.()).toEqual({ ok: false, problem: "duplicate" });
    expect(readSavedViews(SCOPE).map((view) => view.name)).toEqual(["review"]);
  });

  it("leaves the view deleted, and says so, when storage fails as it's put back", () => {
    const saved = saveView(SCOPE, "Review", SETTINGS);
    if (!saved.ok) throw new Error("not saved");
    const restore = deleteView(SCOPE, saved.view.id);
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    expect(restore?.()).toEqual({ ok: false, problem: "storage" });
    setItem.mockRestore();
    expect(readSavedViews(SCOPE)).toEqual([]);

    // Storage that can't be read by then changes nothing either.
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(restore?.()).toEqual({ ok: false, problem: "storage" });
    getItem.mockRestore();
    expect(readSavedViews(SCOPE)).toEqual([]);
    expect(restore?.()).toEqual({ ok: true });
  });

  it("deletes nothing for an id it doesn't keep", () => {
    saveView(SCOPE, "A", SETTINGS);
    expect(deleteView(SCOPE, "missing")).toBeNull();
    expect(readSavedViews(SCOPE)).toHaveLength(1);
  });
});

describe("matchingSavedView", () => {
  const views = [
    { id: "a", name: "A", ...SETTINGS },
    { id: "b", name: "B", ...SETTINGS, pullRequestFilter: "none" as const },
  ];
  const current = { ...SETTINGS, show: ["harness", "repo"] as const };

  it("finds the view every setting matches, whatever the row details' order", () => {
    expect(matchingSavedView(current, views)?.id).toBe("a");
    expect(matchingSavedView({ ...current, pullRequestFilter: "none" }, views)?.id).toBe("b");
  });

  it("matches a project filter by its project's id", () => {
    const inProject = {
      id: "c",
      name: "C",
      ...SETTINGS,
      projectFilter: { kind: "project" as const, id: "p-1" },
    };
    const project = (id: string) => ({
      ...current,
      projectFilter: { kind: "project" as const, id },
    });
    expect(matchingSavedView(project("p-1"), [inProject])?.id).toBe("c");
    expect(matchingSavedView(project("p-2"), [inProject])).toBeNull();
  });

  it("keeps a view filtered to sessions without a harness, not widening it to every harness", () => {
    saveView(SCOPE, "No harness", { ...SETTINGS, harnessFilter: "" });
    expect(readSavedViews(SCOPE)[0]?.harnessFilter).toBe("");
  });

  it("stores a new view's status, harness and project filters", () => {
    const saved = saveView(SCOPE, "Working Codex", {
      ...SETTINGS,
      statusFilter: "working",
      harnessFilter: "codex",
      projectFilter: { kind: "project", id: "p-1" },
    });
    expect(saved.ok).toBe(true);
    expect(readSavedViews(SCOPE)[0]).toMatchObject({
      statusFilter: "working",
      harnessFilter: "codex",
      projectFilter: { kind: "project", id: "p-1" },
    });
  });

  it("saves the sidebar's filters as a view's fields, and reads them back", () => {
    const filter = {
      pullRequest: "closed" as const,
      status: "unread" as const,
      harness: "native:codex",
      project: { kind: "none" as const },
    };
    expect(savedViewListFilter({ ...SETTINGS, ...savedViewFilterFields(filter) })).toEqual(filter);
    expect(savedViewListFilter({ ...SETTINGS, ...DEFAULT_SAVED_VIEW_FILTERS })).toEqual({
      pullRequest: "all",
      status: "all",
      harness: null,
      project: { kind: "all" },
    });
  });

  it("matches none once any setting differs, or with a Custom order", () => {
    expect(matchingSavedView({ ...current, show: ["repo"] }, views)).toBeNull();
    expect(matchingSavedView({ ...current, sessionFilter: "mine" }, views)).toBeNull();
    expect(matchingSavedView({ ...current, statusFilter: "done" }, views)).toBeNull();
    expect(matchingSavedView({ ...current, harnessFilter: "codex" }, views)).toBeNull();
    expect(matchingSavedView({ ...current, projectFilter: { kind: "none" } }, views)).toBeNull();
    expect(matchingSavedView({ ...current, ordering: "custom" }, views)).toBeNull();
  });
});

describe("useSidebarSavedViews", () => {
  it("follows saves and deletes, and shows none until the scope is known", () => {
    const { result, rerender } = renderHook(({ scope }) => useSidebarSavedViews(scope), {
      initialProps: { scope: null as typeof SCOPE | null },
    });
    expect(result.current).toEqual([]);
    rerender({ scope: SCOPE });
    act(() => void saveView(SCOPE, "Review", SETTINGS));
    expect(result.current.map((view) => view.name)).toEqual(["Review"]);
    act(() => void deleteView(SCOPE, result.current[0]!.id));
    expect(result.current).toEqual([]);
  });

  it("follows another window clearing storage", () => {
    saveView(SCOPE, "Review", SETTINGS);
    const { result } = renderHook(() => useSidebarSavedViews(SCOPE));
    expect(result.current).toHaveLength(1);
    // Another window's localStorage.clear() reaches this one as a storage event with no key.
    localStorage.clear();
    act(() => void window.dispatchEvent(new StorageEvent("storage", { key: null })));
    expect(result.current).toEqual([]);
  });
});
