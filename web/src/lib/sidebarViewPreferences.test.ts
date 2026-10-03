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

  it("round-trips a stored selection", () => {
    const view = { grouping: "status", ordering: "status", show: ["updated", "branch"] } as const;
    writeSidebarViewPreferences(view);
    expect(readSidebarViewPreferences()).toEqual(view);
  });

  it("falls back per field on unknown or corrupt values", () => {
    localStorage.setItem(
      "omnigent:sidebar-view",
      JSON.stringify({ grouping: "project", ordering: "updated", show: ["pr", "branch", 3] }),
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
