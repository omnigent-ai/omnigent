import { create } from "zustand";

import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import type { TaskAction } from "@/lib/workflow";

/** Opens the composer's workflow dialog, to describe one from `description` or to run `savedWorkflow`. */
export type StartWorkflow = (description?: string, savedWorkflow?: SavedWorkflow | null) => void;

interface SessionTasksActionsState {
  /** Per session: how its composer starts a workflow, when it offers one. */
  startWorkflow: Readonly<Record<string, StartWorkflow>>;
  /** Per session: how its composer saves the plan as a workflow, given the plan's name. */
  saveWorkflow: Readonly<Partial<Record<string, (planName: string) => void>>>;
  /** Per session: how its composer asks the agent to continue the plan, given its name. */
  continuePlan: Readonly<Partial<Record<string, (planName: string | null) => void>>>;
  /** Per session: how its composer asks the main agent to stop the workflow and its workers. */
  stopWork: Readonly<Partial<Record<string, () => void>>>;
  /** Per session: how its composer asks the agent to run, continue or retry a task, given its name. */
  taskAction: Readonly<Partial<Record<string, (action: TaskAction, taskName: string) => void>>>;
  /** A session whose Tasks tab should open; the shell clears it once open. */
  tasksTabToOpen: string | null;
  /** The task the Tasks tab should scroll to and highlight; the tab clears it once shown. */
  taskToShow: { conversationId: string; stepId: string } | null;
  /**
   * Who asked for the tab: a click opens it; the agent's first plan keeps a phone's drawer
   * shut; a session opening only selects it, leaving the rail as the session restored it.
   */
  tasksTabRequestedBy: "click" | "first-plan" | "session-open";
}

/**
 * Actions that cross surfaces for a session's tasks: the composer registers
 * how to start a workflow, save one, run or continue a task, continue the plan
 * and ask the main agent to stop the workflow's workers,
 * so the Tasks tab can offer them, and the chat's task preview asks the shell to
 * open the Tasks tab.
 */
export const useSessionTasksActions = create<SessionTasksActionsState>(() => ({
  startWorkflow: {},
  saveWorkflow: {},
  taskAction: {},
  continuePlan: {},
  stopWork: {},
  tasksTabToOpen: null,
  taskToShow: null,
  tasksTabRequestedBy: "click",
}));

/**
 * Offer a session's "start a workflow" action to other surfaces.
 *
 * @returns A function that withdraws the action, unless another took its place.
 */
export function registerStartWorkflow(conversationId: string, start: StartWorkflow): () => void {
  return registerSessionAction("startWorkflow", conversationId, start);
}

/**
 * Offer a session's "save the plan as a workflow" action to its Tasks tab.
 *
 * @returns A function that withdraws the action, unless another took its place.
 */
export function registerSaveWorkflow(
  conversationId: string,
  save: (planName: string) => void,
): () => void {
  return registerSessionAction("saveWorkflow", conversationId, save);
}

/**
 * Offer a session's "run or continue a task" action to its Tasks tab.
 *
 * @returns A function that withdraws the action, unless another took its place.
 */
export function registerTaskAction(
  conversationId: string,
  taskAction: (action: TaskAction, taskName: string) => void,
): () => void {
  return registerSessionAction("taskAction", conversationId, taskAction);
}

/**
 * Offer a session's "continue the plan" action to its Tasks tab.
 *
 * @returns A function that withdraws the action, unless another took its place.
 */
export function registerContinuePlan(
  conversationId: string,
  continuePlan: (planName: string | null) => void,
): () => void {
  return registerSessionAction("continuePlan", conversationId, continuePlan);
}

/**
 * Offer coordinated stopping even when only background tasks or other agents are working.
 *
 * @returns A function that withdraws the action, unless another took its place.
 */
export function registerStopWork(conversationId: string, stop: () => void): () => void {
  return registerSessionAction("stopWork", conversationId, stop);
}

type SessionActionKey =
  "startWorkflow" | "saveWorkflow" | "taskAction" | "continuePlan" | "stopWork";

function registerSessionAction<Key extends SessionActionKey>(
  key: Key,
  conversationId: string,
  action: NonNullable<SessionTasksActionsState[Key][string]>,
): () => void {
  useSessionTasksActions.setState(
    (state) =>
      ({ [key]: { ...state[key], [conversationId]: action } }) as Partial<SessionTasksActionsState>,
  );
  return () =>
    useSessionTasksActions.setState((state) => {
      if (state[key][conversationId] !== action) return state;
      const rest = Object.fromEntries(
        Object.entries(state[key]).filter(([id]) => id !== conversationId),
      );
      return { [key]: rest } as Partial<SessionTasksActionsState>;
    });
}

/**
 * Ask the shell to show a session's Tasks tab: the rail, or the drawer on a phone.
 *
 * @param stepId - A task to scroll to and highlight there, e.g. from its chat marker.
 */
export function openTasksTab(conversationId: string, stepId?: string): void {
  useSessionTasksActions.setState({
    tasksTabToOpen: conversationId,
    taskToShow: stepId ? { conversationId, stepId } : null,
    tasksTabRequestedBy: "click",
  });
}

/**
 * Show a session's Tasks tab beside the chat, not over it on a phone: open it for
 * the agent's first plan, or select it when a session with tasks opens.
 * A session opening keeps a request already waiting for it, e.g. a click that navigated there.
 */
export function showTasksTabBesideChat(
  conversationId: string,
  requestedBy: "first-plan" | "session-open",
): void {
  useSessionTasksActions.setState((state) =>
    requestedBy === "session-open" && state.tasksTabToOpen === conversationId
      ? state
      : { tasksTabToOpen: conversationId, taskToShow: null, tasksTabRequestedBy: requestedBy },
  );
}
