import { describe, expect, it, vi } from "vitest";

import { DEFAULT_SIDEBAR_VIEW, type SidebarViewPreferences } from "@/lib/sidebarViewPreferences";

import { chooseSettingValue, settingIn, settingValues } from "@/test/paletteCommands";

import { sidebarViewChoices, sidebarViewCommands } from "./sidebarViewCommands";

function choices(
  overrides: {
    view?: SidebarViewPreferences | null;
    multiUser?: boolean;
    filter?: "all" | "mine";
  } = {},
) {
  const onFilterChange = vi.fn();
  const onViewChange = vi.fn();
  const result = sidebarViewChoices({
    filter: overrides.filter ?? "all",
    multiUser: overrides.multiUser ?? true,
    view: overrides.view === undefined ? DEFAULT_SIDEBAR_VIEW : overrides.view,
    onFilterChange,
    onViewChange,
  });
  return { result, onFilterChange, onViewChange };
}

describe("sidebarViewChoices", () => {
  it("marks the current grouping, display and ordering as selected", () => {
    const { result } = choices({
      view: { ...DEFAULT_SIDEBAR_VIEW, grouping: "status", ordering: "created" },
      filter: "mine",
    });
    expect(result.grouping.find((choice) => choice.selected)?.value).toBe("status");
    expect(result.display.find((choice) => choice.selected)?.value).toBe("mine");
    expect(result.ordering.find((choice) => choice.selected)?.value).toBe("created");
  });

  it("offers only Display while grouping isn't available", () => {
    const { result } = choices({ view: null });
    expect(result.grouping).toEqual([]);
    expect(result.ordering).toEqual([]);
    expect(result.show).toEqual([]);
    expect(result.display.length).toBeGreaterThan(0);
  });

  it("leaves Shared out for a single user", () => {
    expect(choices({ multiUser: false }).result.display.map((choice) => choice.value)).toEqual([
      "all",
      "mine",
      "archived",
    ]);
  });

  it("runs the owner's handlers", () => {
    const { result, onFilterChange, onViewChange } = choices();
    result.grouping.find((choice) => choice.value === "harness")?.select();
    expect(onViewChange).toHaveBeenCalledWith({ grouping: "harness" });
    result.display.find((choice) => choice.value === "archived")?.select();
    expect(onFilterChange).toHaveBeenCalledWith("archived");
  });

  it("toggles one field and keeps the others", () => {
    const view: SidebarViewPreferences = { ...DEFAULT_SIDEBAR_VIEW, show: ["updated", "repo"] };
    const { result, onViewChange } = choices({ view });
    const repo = result.show.find((toggle) => toggle.field === "repo")!;
    expect(repo.shown).toBe(true);
    repo.toggle();
    expect(onViewChange).toHaveBeenCalledWith({ show: ["updated"] });
    result.show.find((toggle) => toggle.field === "branch")!.toggle();
    expect(onViewChange).toHaveBeenLastCalledWith({ show: ["updated", "repo", "branch"] });
  });
});

describe("sidebarViewCommands", () => {
  it("offers each menu section and row field as a setting showing its choice", () => {
    const { result, onViewChange } = choices({
      view: { ...DEFAULT_SIDEBAR_VIEW, grouping: "default", ordering: "updated", show: ["repo"] },
    });
    const commands = sidebarViewCommands(result);
    expect(settingValues(commands)).toEqual({
      "Session List: Group by": "Default",
      "Session List: Display": "All sessions",
      "Session List: Sort by": "Updated",
      "Session List: Show Last Updated Time": "Off",
      "Session List: Show Environment": "Off",
      "Session List: Show Harness": "Off",
      "Session List: Show Project": "Off",
      "Session List: Show Repo": "On",
      "Session List: Show Branch": "Off",
    });
    expect(
      settingIn(commands, "Session List: Group by").choices.map((choice) => choice.label),
    ).toEqual(["Default", "Status", "Harness", "Created", "Updated"]);

    void chooseSettingValue(settingIn(commands, "Session List: Show Repo"), "Off");
    expect(onViewChange).toHaveBeenCalledWith({ show: [] });
    void chooseSettingValue(settingIn(commands, "Session List: Group by"), "Status");
    expect(onViewChange).toHaveBeenLastCalledWith({ grouping: "status" });
  });

  it("offers only Display while grouping isn't available", () => {
    const commands = sidebarViewCommands(choices({ view: null, filter: "mine" }).result);
    expect(settingValues(commands)).toEqual({ "Session List: Display": "My sessions" });
    expect(
      settingIn(commands, "Session List: Display").choices.map((choice) => choice.label),
    ).toEqual(["All sessions", "My sessions", "Shared with me", "Archived sessions"]);
  });
});
