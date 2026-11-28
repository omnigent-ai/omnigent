// With quick_pick_palette on, ⌘K offers the Automations list's actions: New automation, the
// filter tabs as a setting, and each of your automations' row menu actions, greyed by the same
// rules as an automation's header. Delete stays on the row menu, which deletes without asking.

import {
  CopyIcon,
  ListFilterIcon,
  type LucideIcon,
  PauseIcon,
  PencilIcon,
  PlayIcon,
  PlusIcon,
  ShareIcon,
} from "lucide-react";
import { useMemo, useRef } from "react";

import type { ScheduledTask } from "@/lib/scheduledTasksApi";
import { hashText } from "@/lib/textHash";
import { useQuickPickPaletteCommands } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import {
  AVAILABLE,
  type CommandAvailability,
  isCommandListed,
  type PaletteCommand,
  unavailableBecause,
} from "@/store/paletteCommands";
import { settingCommand } from "@/store/paletteSettings";

import {
  AUTOMATION_SAVING,
  type AutomationDetailActionId,
  automationDetailActions,
} from "./automationDetailActions";
import { isSharedWithViewer, OWNER_ONLY_REASONS } from "./ownerOnlyActions";
import { isTriggerPrompt } from "./triggerTemplates";

export type AutomationListFilter = "all" | "active" | "paused";

/** The list's filter tabs, as ⌘K's filter setting lists them. */
export const AUTOMATION_LIST_FILTERS: readonly { value: AutomationListFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
];

export const NEW_AUTOMATION_LABEL = "New automation…";
export const AUTOMATIONS_FILTER_LABEL = "Automations filter";

/** A row menu action ⌘K offers for a listed automation. */
export type AutomationListActionId = Exclude<AutomationDetailActionId, "delete"> | "clone";

const ICONS: Record<AutomationListActionId, LucideIcon> = {
  "run-now": PlayIcon,
  edit: PencilIcon,
  share: ShareIcon,
  clone: CopyIcon,
  pause: PauseIcon,
  resume: PlayIcon,
};

// Edit and Share open a dialog, so they run once the palette has closed.
const OPENS_DIALOG: ReadonlySet<AutomationListActionId> = new Set(["edit", "share"]);

/** How ⌘K names `id` for the automation called `name`. */
export function automationListActionLabel(id: AutomationListActionId, name: string): string {
  const quoted = `“${name}”`;
  switch (id) {
    case "run-now":
      return `Run ${quoted} now`;
    case "edit":
      return `Edit ${quoted}…`;
    case "share":
      return `Share ${quoted}…`;
    case "clone":
      return `Clone ${quoted}`;
    case "pause":
      return `Pause ${quoted}`;
    case "resume":
      return `Resume ${quoted}`;
  }
}

/** What the list page knows, as its tabs, rows and row menus read it. */
export interface AutomationListState {
  /** The tab the page shows; only Your automations offers row actions. */
  view: "yours" | "shared" | "templates";
  /** Your loaded automations, or undefined while they load. */
  tasks: readonly ScheduledTask[] | undefined;
  loadFailed: boolean;
  filter: AutomationListFilter;
  /** The automation with a change or run in flight, whose row menu is disabled. */
  busyTaskId: string | undefined;
  canShare: (task: ScheduledTask) => boolean;
  shareDisabledReason: (task: ScheduledTask) => string | undefined;
  /** The server clones automations. */
  cloneAvailable: boolean;
}

export interface AutomationListAction {
  task: ScheduledTask;
  id: AutomationListActionId;
  label: string;
  availability: CommandAvailability;
}

function cloneAvailability(readOnly: boolean, busy: boolean): CommandAvailability {
  if (readOnly) return unavailableBecause(OWNER_ONLY_REASONS.clone);
  if (busy) return unavailableBecause(AUTOMATION_SAVING);
  return AVAILABLE;
}

/** Each listed automation's row menu actions ⌘K offers, with why each can't run now. */
export function automationListActions(state: AutomationListState): AutomationListAction[] {
  if (state.view !== "yours" || state.loadFailed || !state.tasks) return [];
  return state.tasks.flatMap((task) => {
    const readOnly = isSharedWithViewer(task);
    const busy = state.busyTaskId === task.id;
    const actions: AutomationListAction[] = automationDetailActions({
      readOnly,
      busy,
      triggerMode: task.sessionMode === "continue" && isTriggerPrompt(task.prompt),
      runStarting: false,
      paused: task.state === "paused",
      canShare: state.canShare(task),
      shareDisabledReason: state.shareDisabledReason(task),
    }).flatMap((action) =>
      action.id === "delete" || !isCommandListed(action.availability)
        ? []
        : [
            {
              task,
              id: action.id,
              label: automationListActionLabel(action.id, task.name),
              availability: action.availability,
            },
          ],
    );
    if (state.cloneAvailable) {
      // Clone follows Share, or Edit where Share isn't offered, as in the row menu.
      const after = actions.findIndex(({ id }) => id === "share");
      const position = (after >= 0 ? after : actions.findIndex(({ id }) => id === "edit")) + 1;
      actions.splice(position, 0, {
        task,
        id: "clone",
        label: automationListActionLabel("clone", task.name),
        availability: cloneAvailability(readOnly, busy),
      });
    }
    return actions;
  });
}

/** Whether the page shows its filter tabs, which ⌘K's filter setting follows. */
export function automationFiltersShown(state: AutomationListState): boolean {
  return state.view === "yours" && (state.tasks?.length ?? 0) > 0;
}

export interface AutomationListHandlers {
  newAutomation: () => void;
  changeFilter: (filter: AutomationListFilter) => void;
  runNow: (task: ScheduledTask) => void;
  edit: (task: ScheduledTask) => void;
  share: (task: ScheduledTask) => void;
  clone: (task: ScheduledTask) => void;
  pauseToggle: (task: ScheduledTask) => void;
}

function runAction(handlers: AutomationListHandlers, action: AutomationListAction): void {
  switch (action.id) {
    case "run-now":
      return handlers.runNow(action.task);
    case "edit":
      return handlers.edit(action.task);
    case "share":
      return handlers.share(action.task);
    case "clone":
      return handlers.clone(action.task);
    case "pause":
    case "resume":
      return handlers.pauseToggle(action.task);
  }
}

/**
 * Everything the list loaded for `task`. Any edit changes it, unlike `updatedAt`, which
 * counts whole seconds and so stays put for an edit in the same second.
 */
function taskFingerprint(task: ScheduledTask): string {
  return hashText(JSON.stringify(task));
}

/** The list's ⌘K commands; `handlers` is read when a command runs. */
export function automationListPaletteCommands(
  state: AutomationListState,
  handlers: { current: AutomationListHandlers },
): PaletteCommand[] {
  const commands: PaletteCommand[] = [
    {
      id: "automations-list.new",
      label: NEW_AUTOMATION_LABEL,
      icon: PlusIcon,
      keywords: ["automation", "scheduled", "create", "add", "task"],
      movesFocus: true,
      run: () => handlers.current.newAutomation(),
    },
  ];
  if (automationFiltersShown(state)) {
    commands.push(
      settingCommand({
        id: "automations-filter",
        owner: "automations-list",
        label: AUTOMATIONS_FILTER_LABEL,
        icon: ListFilterIcon,
        keywords: ["automation", "filter", "scheduled", "show"],
        current: state.filter,
        choices: AUTOMATION_LIST_FILTERS.map(({ value, label }) => ({ value, label })),
        choose: (value) => {
          const choice = AUTOMATION_LIST_FILTERS.find((candidate) => candidate.value === value);
          if (choice) handlers.current.changeFilter(choice.value);
        },
      }),
    );
  }
  for (const action of automationListActions(state)) {
    const { task } = action;
    commands.push({
      // A new version of the automation gets new rows, so a stale row never acts on it.
      id: `automations-list.${action.id}:${task.id}:${taskFingerprint(task)}`,
      label: action.label,
      icon: ICONS[action.id],
      keywords: ["automation", "task", "schedule", task.name],
      availability: action.availability,
      movesFocus: OPENS_DIALOG.has(action.id),
      run: () => runAction(handlers.current, action),
    });
  }
  return commands;
}

/** What the commands show, so a re-render with the same list reuses them. */
function listSignature(state: AutomationListState): string {
  return JSON.stringify([
    automationFiltersShown(state) ? state.filter : null,
    automationListActions(state).map(({ task, id, label, availability }) => [
      task.id,
      taskFingerprint(task),
      id,
      label,
      availability,
    ]),
  ]);
}

/** Renders nothing; registers the list's commands while the page shows. */
export function AutomationListPaletteCommands({
  handlers,
  ...state
}: AutomationListState & { handlers: AutomationListHandlers }): null {
  // The page builds its handlers and predicates each render; commands use the newest.
  const latestState = useRef(state);
  latestState.current = state;
  const latestHandlers = useRef(handlers);
  latestHandlers.current = handlers;
  const signature = listSignature(state);
  const commands = useMemo(
    () => automationListPaletteCommands(latestState.current, latestHandlers),
    // The signature captures everything the commands show.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [signature],
  );
  useQuickPickPaletteCommands("automations-list", "Automations", commands);
  return null;
}
