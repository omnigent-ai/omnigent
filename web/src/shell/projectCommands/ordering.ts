// The project list's order and expansion actions, defined once for the sidebar's
// menus and ⌘K: moving a project, how projects sort, and expanding or collapsing
// them all. Each decides its labels and availability from plain facts; it holds no React.

import { AVAILABLE, type CommandAvailability, unavailableBecause } from "@/store/paletteCommands";

export type ProjectMoveDestination = "up" | "down" | "top" | "bottom";

export const PROJECT_MOVE_DESTINATIONS: readonly ProjectMoveDestination[] = [
  "up",
  "down",
  "top",
  "bottom",
];

/** What changing the project order waits on. */
export interface ProjectOrderingFacts {
  /** The saved order has loaded. */
  orderLoaded: boolean;
  /** A change to the order is saving. */
  saving: boolean;
  /** Sessions are being selected, which a reorder would disturb. */
  selecting: boolean;
  /** A session or project title is being edited in place. */
  editing: boolean;
}

export const PROJECT_ORDER_LOADING_REASON = "Loading the project order…";
export const PROJECT_ORDER_SAVING_REASON = "Saving the project order…";
export const PROJECT_ORDER_SELECTING_REASON = "Finish selecting sessions to reorder projects.";
export const PROJECT_ORDER_EDITING_REASON = "Finish renaming to reorder projects.";
export const FIRST_PROJECT_REASON = "Already the first project.";
export const LAST_PROJECT_REASON = "Already the last project.";

/** Why sorting can't change now, or undefined; it waits only on the saved order. */
export function projectSortReason(facts: ProjectOrderingFacts): string | undefined {
  if (!facts.orderLoaded) return PROJECT_ORDER_LOADING_REASON;
  if (facts.saving) return PROJECT_ORDER_SAVING_REASON;
  return undefined;
}

/** Why a project can't move now, or undefined; selecting and renaming also hold moves. */
export function projectMoveReason(facts: ProjectOrderingFacts): string | undefined {
  return (
    projectSortReason(facts) ??
    (facts.selecting
      ? PROJECT_ORDER_SELECTING_REASON
      : facts.editing
        ? PROJECT_ORDER_EDITING_REASON
        : undefined)
  );
}

export interface ProjectMoveCommand {
  destination: ProjectMoveDestination;
  /** The label in the folder's Move submenu. */
  menuLabel: string;
  /** The label in ⌘K, which names the project. */
  label: string;
  keywords: readonly string[];
  availability: CommandAvailability;
}

/** The folder menu's Move submenu labels. */
export const PROJECT_MOVE_MENU_LABELS: Record<ProjectMoveDestination, string> = {
  up: "Move up",
  down: "Move down",
  top: "Move to top",
  bottom: "Move to bottom",
};

/**
 * Moving the project at `index` of `count`. The edges are greyed rather than hidden,
 * as the menu has always shown them; moving in alphabetical order switches to manual.
 */
export function projectMoveCommands(
  projectName: string,
  index: number,
  count: number,
  facts: ProjectOrderingFacts,
): ProjectMoveCommand[] {
  const blocked = projectMoveReason(facts);
  return PROJECT_MOVE_DESTINATIONS.map((destination) => {
    const towardStart = destination === "up" || destination === "top";
    const atEdge = towardStart ? index <= 0 : index >= count - 1;
    const reason =
      blocked ?? (atEdge ? (towardStart ? FIRST_PROJECT_REASON : LAST_PROJECT_REASON) : undefined);
    const where =
      destination === "top"
        ? "to the Top"
        : destination === "bottom"
          ? "to the Bottom"
          : destination === "up"
            ? "Up"
            : "Down";
    return {
      destination,
      menuLabel: PROJECT_MOVE_MENU_LABELS[destination],
      label: `Move Project ${projectName} ${where}`,
      keywords: ["project", "folder", "reorder", "order", projectName],
      availability: reason ? unavailableBecause(reason) : AVAILABLE,
    };
  });
}

export type ProjectSortOrder = "alphabetical" | "manual";

export interface ProjectSortChoice {
  value: ProjectSortOrder;
  /** The menu's label, and the value's name on ⌘K's Sort Projects page. */
  menuLabel: string;
  selected: boolean;
  availability: CommandAvailability;
}

/** How projects sort: the menu checks the current order, and ⌘K shows it as a setting. */
export function projectSortChoices(
  manualOrder: boolean,
  facts: ProjectOrderingFacts,
): ProjectSortChoice[] {
  const reason = projectSortReason(facts);
  const availability = reason ? unavailableBecause(reason) : AVAILABLE;
  return [
    {
      value: "alphabetical",
      menuLabel: "Alphabetically",
      selected: !manualOrder,
      availability,
    },
    {
      value: "manual",
      menuLabel: "Manual order",
      selected: manualOrder,
      availability,
    },
  ];
}

/** ⌘K's name for the sort, a setting whose values are the menu's choices. */
export const SORT_PROJECTS_LABEL = "Sort Projects";

export type ProjectExpansionId = "expand-all" | "collapse-all";

export interface ProjectExpansionCommand {
  id: ProjectExpansionId;
  menuLabel: string;
  label: string;
}

/**
 * Expand all and collapse all, each only when it would change something: both
 * for a mix, and neither while the Projects section is collapsed or empty.
 */
export function projectExpansionCommands({
  projectNames,
  expandedProjects,
  sectionCollapsed,
}: {
  projectNames: readonly string[];
  expandedProjects: readonly string[];
  sectionCollapsed: boolean;
}): ProjectExpansionCommand[] {
  if (sectionCollapsed || projectNames.length === 0) return [];
  const expanded = projectNames.filter((name) => expandedProjects.includes(name)).length;
  const commands: ProjectExpansionCommand[] = [];
  if (expanded < projectNames.length) {
    commands.push({ id: "expand-all", menuLabel: "Expand all", label: "Expand All Projects" });
  }
  if (expanded > 0) {
    commands.push({
      id: "collapse-all",
      menuLabel: "Collapse all",
      label: "Collapse All Projects",
    });
  }
  return commands;
}
