import { describe, expect, it } from "vitest";

import type { DefaultWorkspaceTab } from "@/lib/workspaceTabPreferences";

import { parseAppliedTabSettings, tabSettingsChange } from "./workspaceTabSettings";

const showsAll = () => true;
/** A strip on its empty New tab, with room for every page. */
const fresh = { canShow: showsAll, canOpen: showsAll, untouched: true };
const settings = (pinned: DefaultWorkspaceTab[], initial: DefaultWorkspaceTab | null) => ({
  pinned,
  initial,
});

describe("tabSettingsChange", () => {
  it("pins every page and selects the first page on a strip that never took the settings", () => {
    expect(
      tabSettingsChange(settings(["github", "subagents"], "subagents"), undefined, fresh),
    ).toEqual({
      pin: ["github", "subagents"],
      unpin: [],
      select: "subagents",
      applied: { pinned: ["github", "subagents"], initial: "subagents" },
    });
  });

  it("does nothing once a strip took the settings as they are", () => {
    const applied = {
      pinned: ["github", "subagents"] as DefaultWorkspaceTab[],
      initial: "github" as const,
    };
    expect(tabSettingsChange(settings(["github", "subagents"], "github"), applied, fresh)).toEqual({
      pin: [],
      unpin: [],
      select: null,
      applied,
    });
  });

  it("pins a newly pinned page and unpins a newly unpinned one, keeping the selection", () => {
    const applied = {
      pinned: ["files", "github"] as DefaultWorkspaceTab[],
      initial: "files" as const,
    };
    expect(tabSettingsChange(settings(["files", "tasks"], "files"), applied, fresh)).toEqual({
      pin: ["tasks"],
      unpin: ["github"],
      select: null,
      applied: { pinned: ["files", "tasks"], initial: "files" },
    });
  });

  it("selects a changed first page once", () => {
    const applied = {
      pinned: ["files", "tasks"] as DefaultWorkspaceTab[],
      initial: "files" as const,
    };
    const change = tabSettingsChange(settings(["files", "tasks"], "tasks"), applied, fresh);
    expect(change.select).toBe("tasks");
    expect(
      tabSettingsChange(settings(["files", "tasks"], "tasks"), change.applied, fresh).select,
    ).toBeNull();
  });

  it("pins a page the session can't show once it can, but never selects it later", () => {
    const noPullRequests = (page: DefaultWorkspaceTab) => page !== "github";
    const first = tabSettingsChange(settings(["github", "tasks"], "github"), undefined, {
      ...fresh,
      canShow: noPullRequests,
    });
    expect(first).toEqual({
      pin: ["tasks"],
      unpin: [],
      select: null,
      applied: { pinned: ["tasks"], initial: "github" },
    });
    expect(
      tabSettingsChange(settings(["github", "tasks"], "github"), first.applied, fresh),
    ).toMatchObject({ pin: ["github"], select: null });
  });

  it("takes a changed first page the session can't show without selecting it", () => {
    const applied = { pinned: ["tasks"] as DefaultWorkspaceTab[], initial: "tasks" as const };
    const change = tabSettingsChange(settings(["tasks"], "github"), applied, {
      ...fresh,
      canShow: (page) => page !== "github",
    });
    expect(change).toMatchObject({ select: null, applied: { initial: "github" } });
    expect(
      tabSettingsChange(settings(["tasks"], "github"), change.applied, fresh).select,
    ).toBeNull();
  });

  it("keeps what a strip shows when it first takes the settings, taking the first page", () => {
    const change = tabSettingsChange(settings(["files", "tasks"], "tasks"), undefined, {
      ...fresh,
      untouched: false,
    });
    expect(change).toMatchObject({ pin: ["files", "tasks"], select: null });
    expect(change.applied.initial).toBe("tasks");
  });

  it("leaves a pinned page with no room to open for later", () => {
    const change = tabSettingsChange(settings(["files", "tasks"], "tasks"), undefined, {
      ...fresh,
      canOpen: (page) => page === "files",
    });
    expect(change).toEqual({
      pin: ["files"],
      unpin: [],
      select: null,
      applied: { pinned: ["files"], initial: "tasks" },
    });
  });

  it("takes no pins and no first page when nothing is pinned", () => {
    expect(tabSettingsChange(settings([], null), undefined, fresh)).toEqual({
      pin: [],
      unpin: [],
      select: null,
      applied: { pinned: [], initial: null },
    });
  });
});

describe("parseAppliedTabSettings", () => {
  it("keeps known pages once and drops the rest", () => {
    expect(
      parseAppliedTabSettings({ pinned: ["tasks", "nope", 3, "tasks", "files"], initial: "files" }),
    ).toEqual({ pinned: ["tasks", "files"], initial: "files" });
    expect(parseAppliedTabSettings({ pinned: [], initial: "nope" })).toEqual({
      pinned: [],
      initial: null,
    });
  });

  it("reads a missing or malformed record as none", () => {
    expect(parseAppliedTabSettings(undefined)).toBeUndefined();
    expect(parseAppliedTabSettings({ initial: "files" })).toBeUndefined();
    expect(parseAppliedTabSettings(["files"])).toBeUndefined();
  });
});
