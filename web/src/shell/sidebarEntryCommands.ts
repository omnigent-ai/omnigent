// The sidebar's Select sessions and project list entry points, defined once for
// their buttons and menus and ⌘K: what each is called, when it's offered, and what
// it starts. The sidebar renders from these and registers ⌘K's rows from them.

import { ArrowDownAZIcon, ListChecksIcon, Maximize2Icon, Minimize2Icon } from "lucide-react";

import type { PaletteCommand } from "@/store/paletteCommands";
import { settingCommand } from "@/store/paletteSettings";

import {
  type ProjectExpansionCommand,
  type ProjectExpansionId,
  type ProjectOrderingFacts,
  type ProjectSortChoice,
  type ProjectSortOrder,
  projectExpansionCommands,
  projectSortChoices,
  SORT_PROJECTS_LABEL,
} from "./projectCommands/ordering";

/** Which sessions a selection covers: the Sessions list's, or the project folders'. */
export type SelectionScope = "sessions" | "projects";

/** Select sessions: the buttons' and menu item's name, and ⌘K's. */
export const SELECT_SESSIONS = { menuLabel: "Select sessions", label: "Select Sessions" };

export interface SelectSessionsFacts {
  selectionMode: boolean;
  /** Grouping on and not grouped: one selection covers sessions and projects. */
  unifiedSelection: boolean;
  /** The Projects section shows: hidden while sessions are grouped some other way. */
  projectsSectionShown: boolean;
  /** Selectable sessions outside any project: shared or loose. Pinned ones can't be selected. */
  hasLooseSessions: boolean;
  hasProjectSessions: boolean;
}

export interface SelectSessionsEntry {
  /** The Sessions header's button. */
  sessionsHeaderButton: boolean;
  /** The Projects header's button, which starts a unified selection. */
  projectsHeaderButton: boolean;
  /** The project list menu's item, which selects project sessions. */
  projectMenuItem: boolean;
  /**
   * Where ⌘K's row starts: the sessions an entry point would select, preferring
   * project sessions when only folders hold any, and the header button's scope otherwise.
   */
  paletteScope: SelectionScope | null;
}

/** Which Select sessions entry points show, and the one ⌘K offers. */
export function selectSessionsEntry(facts: SelectSessionsFacts): SelectSessionsEntry {
  const offered = !facts.selectionMode;
  const sessionsHeaderButton = offered && !facts.unifiedSelection;
  const projectsHeaderButton = offered && facts.unifiedSelection && facts.projectsSectionShown;
  const projectMenuItem =
    offered && facts.projectsSectionShown && !facts.unifiedSelection && facts.hasProjectSessions;
  const paletteScope: SelectionScope | null = projectsHeaderButton
    ? "sessions"
    : sessionsHeaderButton && facts.hasLooseSessions
      ? "sessions"
      : projectMenuItem
        ? "projects"
        : sessionsHeaderButton
          ? "sessions"
          : null;
  return { sessionsHeaderButton, projectsHeaderButton, projectMenuItem, paletteScope };
}

export interface ProjectListFacts {
  /** The Projects header's controls give way to the selection while selecting. */
  selectionMode: boolean;
  /** The Projects section shows: hidden while sessions are grouped some other way. */
  sectionShown: boolean;
  /** The project list menu's Select sessions item shows (see `selectSessionsEntry`). */
  selectProjectSessions: boolean;
  manualOrder: boolean;
  ordering: ProjectOrderingFacts;
  projectNames: readonly string[];
  expandedProjects: readonly string[];
  sectionCollapsed: boolean;
}

export interface ProjectListControls {
  /** The header's controls show, with its New project button. */
  offered: boolean;
  /** The project list menu shows: only with projects or sessions to organize. */
  menuShown: boolean;
  /** In the menu, so empty while it doesn't show. */
  sort: ProjectSortChoice[];
  expansion: ProjectExpansionCommand[];
}

/** The Projects header's controls, which its button and menu render and ⌘K offers. */
export function projectListControls(facts: ProjectListFacts): ProjectListControls {
  const hidden: ProjectListControls = { offered: false, menuShown: false, sort: [], expansion: [] };
  if (facts.selectionMode || !facts.sectionShown) return hidden;
  const expansion = projectExpansionCommands({
    projectNames: facts.projectNames,
    expandedProjects: facts.expandedProjects,
    sectionCollapsed: facts.sectionCollapsed,
  });
  const menuShown =
    expansion.length > 0 || facts.selectProjectSessions || facts.projectNames.length > 0;
  if (!menuShown) return { offered: true, menuShown: false, sort: [], expansion: [] };
  return {
    offered: true,
    menuShown: true,
    sort: projectSortChoices(facts.manualOrder, facts.ordering),
    expansion,
  };
}

export interface ProjectListHandlers {
  changeSort: (order: ProjectSortOrder) => void;
  runExpansion: (id: ProjectExpansionId) => void;
  enterSelection: (scope: SelectionScope) => void;
}

/** ⌘K's rows for the same entry points, offered exactly when their controls are. */
export function sidebarEntryPaletteCommands(
  controls: ProjectListControls,
  selection: SelectSessionsEntry,
  handlers: ProjectListHandlers,
): PaletteCommand[] {
  const commands: PaletteCommand[] = [];
  const scope = selection.paletteScope;
  if (scope) {
    commands.push({
      id: `select-sessions:${scope}`,
      label: SELECT_SESSIONS.label,
      icon: ListChecksIcon,
      keywords: ["select", "multiple", "bulk", "sessions", "checkbox"],
      run: () => handlers.enterSelection(scope),
    });
  }
  // New project… comes from the header's own button, which registers it.
  if (!controls.offered) return commands;
  const sortedBy = controls.sort.find((choice) => choice.selected);
  if (sortedBy) {
    commands.push(
      settingCommand({
        id: "projects-sort",
        owner: "sidebar",
        label: SORT_PROJECTS_LABEL,
        icon: ArrowDownAZIcon,
        keywords: ["project", "projects", "sort", "order"],
        current: sortedBy.value,
        // Both orders wait on the saved one alike, so the setting carries the reason.
        availability: sortedBy.availability,
        choices: controls.sort.map((choice) => ({ value: choice.value, label: choice.menuLabel })),
        choose: (value) => {
          const choice = controls.sort.find((candidate) => candidate.value === value);
          if (choice) handlers.changeSort(choice.value);
        },
      }),
    );
  }
  for (const command of controls.expansion) {
    commands.push({
      id: `projects-${command.id}`,
      label: command.label,
      icon: command.id === "expand-all" ? Maximize2Icon : Minimize2Icon,
      keywords: ["project", "projects", "folders", command.menuLabel],
      run: () => handlers.runExpansion(command.id),
    });
  }
  return commands;
}
