import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_SIDEBAR_VIEW,
  readSidebarViewPreferences,
  writeSidebarViewPreferences,
} from "./sidebarViewPreferences";

afterEach(() => {
  localStorage.clear();
});

describe("sidebarViewPreferences", () => {
  it("defaults when nothing is stored", () => {
    expect(readSidebarViewPreferences()).toEqual(DEFAULT_SIDEBAR_VIEW);
  });

  it.each(["status", "created", "updated"] as const)("round-trips %s grouping", (grouping) => {
    const view = { grouping, ordering: "created", show: ["updated", "branch"] } as const;
    writeSidebarViewPreferences(view);
    expect(readSidebarViewPreferences()).toEqual(view);
  });

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

  it("normalizes show fields to menu order", () => {
    localStorage.setItem(
      "omnigent:sidebar-view",
      JSON.stringify({ show: ["branch", "updated", "branch"] }),
    );
    expect(readSidebarViewPreferences().show).toEqual(["updated", "branch"]);
  });
});
