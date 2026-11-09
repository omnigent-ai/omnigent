import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_SIDEBAR_VIEW,
  readSidebarViewPreferences,
  writeSidebarViewPreferences,
} from "./sidebarViewPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("sidebarViewPreferences", () => {
  it("defaults when nothing is stored", () => {
    expect(readSidebarViewPreferences()).toEqual(DEFAULT_SIDEBAR_VIEW);
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
    expect(readSidebarViewPreferences()).toEqual(DEFAULT_SIDEBAR_VIEW);
  });

  it("round-trips a Custom order and drops a malformed one", () => {
    const custom = { base: "created", since: 100, ids: ["b", "a"] } as const;
    writeSidebarViewPreferences({ grouping: "status", ordering: "custom", show: [], custom });
    expect(readSidebarViewPreferences()).toMatchObject({ ordering: "custom", custom });

    localStorage.setItem(
      "omnigent:sidebar-view",
      JSON.stringify({ ordering: "custom", custom: { base: "custom", since: 1, ids: [] } }),
    );
    expect(readSidebarViewPreferences()).toEqual(DEFAULT_SIDEBAR_VIEW);
    localStorage.setItem(
      "omnigent:sidebar-view",
      JSON.stringify({
        ordering: "custom",
        custom: { base: "updated", since: 1, ids: ["a", 2, "a"] },
      }),
    );
    expect(readSidebarViewPreferences().custom?.ids).toEqual(["a"]);
  });

  it("treats unreadable or full storage as defaults without throwing", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Storage is disabled", "SecurityError");
    });
    expect(readSidebarViewPreferences()).toEqual(DEFAULT_SIDEBAR_VIEW);
    expect(getItem).toHaveBeenCalledWith("omnigent:sidebar-view");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Quota exceeded", "QuotaExceededError");
    });
    expect(() => writeSidebarViewPreferences(DEFAULT_SIDEBAR_VIEW)).not.toThrow();
  });

  it("normalizes show fields to menu order", () => {
    localStorage.setItem(
      "omnigent:sidebar-view",
      JSON.stringify({ show: ["branch", "updated", "branch"] }),
    );
    expect(readSidebarViewPreferences().show).toEqual(["updated", "branch"]);
  });
});
