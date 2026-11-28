import { describe, expect, it, vi } from "vitest";

import { unavailableBecause } from "@/store/paletteCommands";
import { currentChoiceLabel } from "@/store/paletteSettings";
import { chooseSettingValue, settingIn } from "@/test/paletteCommands";

import {
  PROJECT_ORDER_LOADING_REASON,
  type ProjectOrderingFacts,
} from "./projectCommands/ordering";
import {
  type ProjectListFacts,
  projectListControls,
  type SelectSessionsFacts,
  selectSessionsEntry,
  sidebarEntryPaletteCommands,
} from "./sidebarEntryCommands";

const READY: ProjectOrderingFacts = {
  orderLoaded: true,
  saving: false,
  selecting: false,
  editing: false,
};
const LIST: ProjectListFacts = {
  selectionMode: false,
  sectionShown: true,
  selectProjectSessions: false,
  manualOrder: false,
  ordering: READY,
  projectNames: ["Alpha"],
  expandedProjects: [],
  sectionCollapsed: false,
};
const IDLE: SelectSessionsFacts = {
  selectionMode: false,
  unifiedSelection: false,
  projectsSectionShown: true,
  hasLooseSessions: false,
  hasProjectSessions: false,
};

const handlers = () => ({
  changeSort: vi.fn(),
  runExpansion: vi.fn(),
  enterSelection: vi.fn(),
});

describe("selectSessionsEntry", () => {
  it("shows the Sessions header's button even with nothing listed, and ⌘K matches it", () => {
    expect(selectSessionsEntry(IDLE)).toEqual({
      sessionsHeaderButton: true,
      projectsHeaderButton: false,
      projectMenuItem: false,
      paletteScope: "sessions",
    });
  });

  it("moves the button to the Projects header in a unified selection", () => {
    expect(
      selectSessionsEntry({ ...IDLE, unifiedSelection: true, hasProjectSessions: true }),
    ).toEqual({
      sessionsHeaderButton: false,
      projectsHeaderButton: true,
      projectMenuItem: false,
      paletteScope: "sessions",
    });
  });

  it("adds the project list's item when folders hold sessions", () => {
    expect(selectSessionsEntry({ ...IDLE, hasProjectSessions: true }).projectMenuItem).toBe(true);
  });

  it("selects project sessions from ⌘K when only folders hold any", () => {
    expect(selectSessionsEntry({ ...IDLE, hasProjectSessions: true }).paletteScope).toBe(
      "projects",
    );
    expect(
      selectSessionsEntry({ ...IDLE, hasProjectSessions: true, hasLooseSessions: true })
        .paletteScope,
    ).toBe("sessions");
  });

  it("drops the project entry points while the Projects section is hidden", () => {
    const grouped = { ...IDLE, projectsSectionShown: false, hasProjectSessions: true };
    expect(selectSessionsEntry(grouped)).toMatchObject({
      projectMenuItem: false,
      paletteScope: "sessions",
    });
  });

  it("offers nothing while already selecting", () => {
    expect(selectSessionsEntry({ ...IDLE, selectionMode: true, hasProjectSessions: true })).toEqual(
      {
        sessionsHeaderButton: false,
        projectsHeaderButton: false,
        projectMenuItem: false,
        paletteScope: null,
      },
    );
  });
});

describe("projectListControls", () => {
  it("withdraws the header's controls while selecting", () => {
    expect(projectListControls({ ...LIST, selectionMode: true })).toEqual({
      offered: false,
      menuShown: false,
      sort: [],
      expansion: [],
    });
  });

  it("withdraws them while sessions are grouped, which hides the Projects section", () => {
    expect(projectListControls({ ...LIST, sectionShown: false }).offered).toBe(false);
  });

  it("keeps New project but no menu, so no sorting, with no projects to organize", () => {
    expect(projectListControls({ ...LIST, projectNames: [] })).toEqual({
      offered: true,
      menuShown: false,
      sort: [],
      expansion: [],
    });
    const labels = sidebarEntryPaletteCommands(
      projectListControls({ ...LIST, projectNames: [] }),
      selectSessionsEntry(IDLE),
      handlers(),
    ).map((command) => command.label);
    expect(labels).toEqual(["Select Sessions"]);
  });

  it("offers the sort choices and the expansion that would change something", () => {
    const controls = projectListControls(LIST);
    expect(controls.offered).toBe(true);
    expect(controls.sort.map((choice) => [choice.value, choice.selected])).toEqual([
      ["alphabetical", true],
      ["manual", false],
    ]);
    expect(controls.expansion.map((command) => command.id)).toEqual(["expand-all"]);
  });
});

describe("sidebarEntryPaletteCommands", () => {
  it("greys the whole sort setting with the shared reason while the order loads", () => {
    const commands = sidebarEntryPaletteCommands(
      projectListControls({ ...LIST, ordering: { ...LIST.ordering, orderLoaded: false } }),
      selectSessionsEntry(IDLE),
      handlers(),
    );
    expect(commands.find((command) => command.label === "Sort Projects")?.availability).toEqual(
      unavailableBecause(PROJECT_ORDER_LOADING_REASON),
    );
  });

  const labels = (commands: { label: string }[]) => commands.map((command) => command.label);

  it("offers the controls' rows while offered, running their handlers", () => {
    const run = handlers();
    const commands = sidebarEntryPaletteCommands(
      projectListControls(LIST),
      selectSessionsEntry(IDLE),
      run,
    );
    expect(labels(commands)).toEqual(["Select Sessions", "Sort Projects", "Expand All Projects"]);
    commands[0]!.run();
    expect(run.enterSelection).toHaveBeenCalledWith("sessions");
    const sort = settingIn(commands, "Sort Projects");
    expect(currentChoiceLabel(sort)).toBe("Alphabetically");
    expect(sort.choices.map((choice) => choice.label)).toEqual(["Alphabetically", "Manual order"]);
    void chooseSettingValue(sort, "Manual order");
    expect(run.changeSort).toHaveBeenCalledWith("manual");
    commands.find((command) => command.label === "Expand All Projects")!.run();
    expect(run.runExpansion).toHaveBeenCalledWith("expand-all");
  });

  it("offers none of the project controls, nor Select Sessions, while selecting", () => {
    expect(
      sidebarEntryPaletteCommands(
        projectListControls({ ...LIST, selectionMode: true }),
        selectSessionsEntry({ ...IDLE, selectionMode: true }),
        handlers(),
      ),
    ).toEqual([]);
  });
});
