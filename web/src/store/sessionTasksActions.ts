import { create } from "zustand";

import type { TaskAction } from "@/lib/workflow";

interface SessionTasksActionsState {
  /** Per session: how its composer starts a workflow, when it offers one. */
  startWorkflow: Readonly<Record<string, () => void>>;
  /** Per session: how its composer saves the plan as a workflow, given the plan's name. */
  saveWorkflow: Readonly<Partial<Record<string, (planName: string) => void>>>;
  /** Per session: how its composer asks the agent to continue the plan, given its name. */
  continuePlan: Readonly<Partial<Record<string, (planName: string | null) => void>>>;
  /** Per session: how its composer stops the agent, offered only while it works. */
  stopWork: Readonly<Partial<Record<string, () => void>>>;
  /** Per session: how its composer asks the agent to run, continue or retry a task, given its name. */
  taskAction: Readonly<Partial<Record<string, (action: TaskAction, taskName: string) => void>>>;
  /** A session whose Tasks tab should open; the shell clears it once open. */
  tasksTabToOpen: string | null;
  /** The task the Tasks tab should scroll to and highlight; the tab clears it once shown. */
  taskToShow: { conversationId: string; stepId: string } | null;
  /** True when the agent, not a click, asked for the tab: a phone's drawer then stays shut. */
  tasksTabAutoOpen: boolean;
}

/**
 * Actions that cross surfaces for a session's tasks: the composer registers
 * how to start a workflow, save one, run or continue a task, continue the plan
 * and stop the agent,
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
  tasksTabAutoOpen: false,
}));

/**
 * Offer a session's "start a workflow" action to other surfaces.
 *
 * @returns A function that withdraws the action, unless another took its place.
 */
export function registerStartWorkflow(conversationId: string, start: () => void): () => void {
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
 * Offer a session's "stop the agent" action to its Tasks tab while the agent works.
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
    tasksTabAutoOpen: false,
  });
}

/**
 * Show a session's Tasks tab beside the chat, not over it on a phone: when the
 * agent makes the session's first plan, or a session with tasks opens.
 */
export function showTasksTabBesideChat(conversationId: string): void {
  useSessionTasksActions.setState({
    tasksTabToOpen: conversationId,
    taskToShow: null,
    tasksTabAutoOpen: true,
  });
}
