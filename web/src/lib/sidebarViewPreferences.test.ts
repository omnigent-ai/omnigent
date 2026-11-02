import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_SIDEBAR_VIEW,
  readSidebarViewPreferences,
  removeSidebarCustomOrderIds,
  resetSidebarViewPreferencesForTests,
  sidebarBaseOrdering,
  subscribeSidebarViewPreferences,
  writeSidebarViewPreferences,
} from "./sidebarViewPreferences";

beforeEach(resetSidebarViewPreferencesForTests);

afterEach(() => {
  localStorage.clear();
});

describe("sidebarViewPreferences", () => {
  it("defaults when nothing is stored", () => {
    expect(readSidebarViewPreferences()).toEqual(DEFAULT_SIDEBAR_VIEW);
  });

  it("keeps other tabs' preferences out of the current snapshot and subsequent writes", () => {
    const view = { ...DEFAULT_SIDEBAR_VIEW, grouping: "status" as const };
    writeSidebarViewPreferences(view);
    const otherTab = { ...DEFAULT_SIDEBAR_VIEW, ordering: "created" as const };
    localStorage.setItem("omnigent:sidebar-view", JSON.stringify(otherTab));
    window.dispatchEvent(new StorageEvent("storage", { key: "omnigent:sidebar-view" }));
    expect(readSidebarViewPreferences()).toBe(view);
    writeSidebarViewPreferences({ ...readSidebarViewPreferences(), show: ["branch"] });
    expect(JSON.parse(localStorage.getItem("omnigent:sidebar-view")!)).toEqual({
      ...view,
      show: ["branch"],
    });
  });

  it("keeps untouched history in memory without growing or rewriting the saved order", () => {
    const view = {
      ...DEFAULT_SIDEBAR_VIEW,
      ordering: "custom" as const,
      customOrder: { baseOrdering: "updated" as const, ids: ["saved"], startedAt: 100 },
    };
    writeSidebarViewPreferences(view);
    const raw = localStorage.getItem("omnigent:sidebar-view");
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    try {
      const capturedIds = [
        "saved",
        ...Array.from({ length: 10_000 }, (_, index) => `old-${index}`),
      ];
      const captured = { ...view, customOrder: { ...view.customOrder, capturedIds } };
      writeSidebarViewPreferences(captured);
      expect(readSidebarViewPreferences()).toBe(captured);
      expect(setItem.mock.calls).toHaveLength(0);
      expect(localStorage.getItem("omnigent:sidebar-view")).toBe(raw);
      removeSidebarCustomOrderIds(["old-9999"]);
      expect(readSidebarViewPreferences().customOrder?.capturedIds).not.toContain("old-9999");
      expect(setItem.mock.calls).toHaveLength(0);
      writeSidebarViewPreferences({ ...readSidebarViewPreferences(), grouping: "status" });
      const stored = JSON.parse(localStorage.getItem("omnigent:sidebar-view")!);
      expect(stored.customOrder).toEqual(view.customOrder);
      resetSidebarViewPreferencesForTests();
      expect(readSidebarViewPreferences()).toEqual(stored);
    } finally {
      setItem.mockRestore();
    }
  });

  it.each(["status", "created", "updated", "harness"] as const)(
    "round-trips %s grouping",
    (grouping) => {
      const view = { grouping, ordering: "created", show: ["updated", "branch"] } as const;
      writeSidebarViewPreferences(view);
      expect(readSidebarViewPreferences()).toEqual(view);
    },
  );

  it("restores saved Status grouping while preserving ordering and metadata", () => {
    localStorage.setItem(
      "omnigent:sidebar-view",
      JSON.stringify({ grouping: "status", ordering: "created", show: ["branch"] }),
    );
    expect(readSidebarViewPreferences()).toEqual({
      grouping: "status",
      ordering: "created",
      show: ["branch"],
    });
  });

  it("falls back per field on unknown or corrupt values", () => {
    localStorage.setItem(
      "omnigent:sidebar-view",
      JSON.stringify({ grouping: "project", ordering: "status", show: ["pr", "branch", 3] }),
    );
    expect(readSidebarViewPreferences()).toEqual({
      grouping: "default",
      ordering: "updated",
      show: ["branch"],
    });

    localStorage.setItem("omnigent:sidebar-view", "{not json");
    resetSidebarViewPreferencesForTests();
    expect(readSidebarViewPreferences()).toEqual(DEFAULT_SIDEBAR_VIEW);
  });

  it("normalizes show fields to menu order", () => {
    localStorage.setItem(
      "omnigent:sidebar-view",
      JSON.stringify({ show: ["branch", "updated", "branch"] }),
    );
    expect(readSidebarViewPreferences().show).toEqual(["updated", "branch"]);
  });

  it("persists Custom and its base order, normalizing saved session ids", () => {
    localStorage.setItem(
      "omnigent:sidebar-view",
      JSON.stringify({
        ordering: "custom",
        customOrder: { baseOrdering: "created", ids: ["b", "a", "b", 42, ""], startedAt: 100 },
      }),
    );
    const view = readSidebarViewPreferences();
    expect(view.customOrder).toEqual({ baseOrdering: "created", ids: ["b", "a"], startedAt: 100 });
    expect(sidebarBaseOrdering(view)).toBe("created");
    writeSidebarViewPreferences(view);
    expect(readSidebarViewPreferences()).toEqual(view);
    const changed = vi.fn();
    const unsubscribe = subscribeSidebarViewPreferences(changed);
    removeSidebarCustomOrderIds(["missing"]);
    expect(changed).not.toHaveBeenCalled();
    removeSidebarCustomOrderIds(["b"]);
    expect(readSidebarViewPreferences().customOrder?.ids).toEqual(["a"]);
    expect(changed).toHaveBeenCalledExactlyOnceWith();
    expect(readSidebarViewPreferences()).toBe(readSidebarViewPreferences());
    unsubscribe();
    removeSidebarCustomOrderIds(["a"]);
    expect(readSidebarViewPreferences().customOrder?.ids).toEqual([]);
    expect(changed).toHaveBeenCalledOnce();
  });

  it.each(["quota", "access"])(
    "keeps in-session changes through a %s failure and pruning",
    (failure) => {
      writeSidebarViewPreferences({ ...DEFAULT_SIDEBAR_VIEW, grouping: "status" });
      const fail = () => {
        throw new DOMException("Storage unavailable");
      };
      const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(fail);
      const getItem =
        failure === "access"
          ? vi.spyOn(Storage.prototype, "getItem").mockImplementation(fail)
          : undefined;
      const next = {
        ...DEFAULT_SIDEBAR_VIEW,
        grouping: "harness" as const,
        ordering: "custom" as const,
        customOrder: { baseOrdering: "created" as const, ids: ["removed", "kept"], startedAt: 100 },
      };
      try {
        writeSidebarViewPreferences(next);
        removeSidebarCustomOrderIds(["removed"]);
        const expected = { ...next, customOrder: { ...next.customOrder, ids: ["kept"] } };
        expect(readSidebarViewPreferences()).toEqual(expected);
        getItem?.mockRestore();
        setItem.mockRestore();
        // Recovery must not replace the in-session order with the older disk value.
        expect(readSidebarViewPreferences()).toEqual(expected);
        writeSidebarViewPreferences(readSidebarViewPreferences());
        expect(JSON.parse(localStorage.getItem("omnigent:sidebar-view")!)).toEqual(expected);
      } finally {
        getItem?.mockRestore();
        setItem.mockRestore();
        writeSidebarViewPreferences(DEFAULT_SIDEBAR_VIEW);
      }
    },
  );

  it.each([null, {}, { ids: [], startedAt: "bad" }, { ids: [], startedAt: -1 }])(
    "ignores invalid custom order %j",
    (customOrder) => {
      localStorage.setItem(
        "omnigent:sidebar-view",
        JSON.stringify({ ordering: "custom", customOrder }),
      );
      expect(readSidebarViewPreferences()).toEqual(DEFAULT_SIDEBAR_VIEW);
    },
  );
});
