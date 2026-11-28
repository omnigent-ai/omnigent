import { describe, expect, it } from "vitest";

import { AVAILABLE, unavailableBecause } from "@/store/paletteCommands";

import {
  FIRST_PROJECT_REASON,
  LAST_PROJECT_REASON,
  PROJECT_ORDER_EDITING_REASON,
  PROJECT_ORDER_LOADING_REASON,
  PROJECT_ORDER_SAVING_REASON,
  PROJECT_ORDER_SELECTING_REASON,
  type ProjectOrderingFacts,
  projectExpansionCommands,
  projectMoveCommands,
  projectMoveReason,
  projectSortChoices,
  projectSortReason,
} from "./ordering";

const READY: ProjectOrderingFacts = {
  orderLoaded: true,
  saving: false,
  selecting: false,
  editing: false,
};

const availability = (index: number, count: number, facts: ProjectOrderingFacts = READY) =>
  Object.fromEntries(
    projectMoveCommands("Alpha", index, count, facts).map((move) => [
      move.destination,
      move.availability,
    ]),
  );

describe("project ordering reasons", () => {
  it("names what a move waits on, loading first", () => {
    expect(projectMoveReason({ ...READY, orderLoaded: false, saving: true })).toBe(
      PROJECT_ORDER_LOADING_REASON,
    );
    expect(projectMoveReason({ ...READY, saving: true })).toBe(PROJECT_ORDER_SAVING_REASON);
    expect(projectMoveReason({ ...READY, selecting: true })).toBe(PROJECT_ORDER_SELECTING_REASON);
    expect(projectMoveReason({ ...READY, editing: true })).toBe(PROJECT_ORDER_EDITING_REASON);
    expect(projectMoveReason(READY)).toBeUndefined();
  });

  it("lets sorting change while selecting or renaming, as the menu always has", () => {
    expect(projectSortReason({ ...READY, selecting: true, editing: true })).toBeUndefined();
    expect(projectSortReason({ ...READY, orderLoaded: false })).toBe(PROJECT_ORDER_LOADING_REASON);
    expect(projectSortReason({ ...READY, saving: true })).toBe(PROJECT_ORDER_SAVING_REASON);
  });
});

describe("projectMoveCommands", () => {
  it("greys the first project's upward moves and the last's downward ones", () => {
    expect(availability(0, 3)).toEqual({
      up: unavailableBecause(FIRST_PROJECT_REASON),
      top: unavailableBecause(FIRST_PROJECT_REASON),
      down: AVAILABLE,
      bottom: AVAILABLE,
    });
    expect(availability(2, 3)).toEqual({
      up: AVAILABLE,
      top: AVAILABLE,
      down: unavailableBecause(LAST_PROJECT_REASON),
      bottom: unavailableBecause(LAST_PROJECT_REASON),
    });
    expect(Object.values(availability(1, 3)).every((each) => each.available)).toBe(true);
  });

  it("greys every move of a single project", () => {
    expect(Object.values(availability(0, 1)).every((each) => !each.available)).toBe(true);
  });

  it("greys every move with what it waits on, ahead of the edge", () => {
    expect(availability(0, 3, { ...READY, saving: true })).toEqual({
      up: unavailableBecause(PROJECT_ORDER_SAVING_REASON),
      top: unavailableBecause(PROJECT_ORDER_SAVING_REASON),
      down: unavailableBecause(PROJECT_ORDER_SAVING_REASON),
      bottom: unavailableBecause(PROJECT_ORDER_SAVING_REASON),
    });
  });

  it("labels the menu row briefly and the ⌘K row with the project", () => {
    const top = projectMoveCommands("Alpha", 1, 3, READY).find(
      (move) => move.destination === "top",
    );
    expect(top).toMatchObject({ menuLabel: "Move to top", label: "Move Project Alpha to the Top" });
  });
});

describe("projectSortChoices", () => {
  it("checks the current order and shares one availability", () => {
    expect(
      projectSortChoices(true, READY).map((choice) => [choice.value, choice.selected]),
    ).toEqual([
      ["alphabetical", false],
      ["manual", true],
    ]);
    expect(
      projectSortChoices(false, { ...READY, saving: true }).every(
        (choice) => !choice.availability.available,
      ),
    ).toBe(true);
  });
});

describe("projectExpansionCommands", () => {
  const ids = (expandedProjects: string[], sectionCollapsed = false, projectNames = ["a", "b"]) =>
    projectExpansionCommands({ projectNames, expandedProjects, sectionCollapsed }).map(
      (command) => command.id,
    );

  it("offers only what would change something", () => {
    expect(ids([])).toEqual(["expand-all"]);
    expect(ids(["a"])).toEqual(["expand-all", "collapse-all"]);
    expect(ids(["a", "b"])).toEqual(["collapse-all"]);
  });

  it("offers nothing while the section is collapsed or there are no projects", () => {
    expect(ids(["a"], true)).toEqual([]);
    expect(ids([], false, [])).toEqual([]);
  });
});
