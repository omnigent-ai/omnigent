import { create } from "zustand";

interface SessionTasksActionsState {
  /** Per session: how its composer starts a workflow, when it offers one. */
  startWorkflow: Readonly<Record<string, () => void>>;
  /** A session whose Tasks tab should open; the shell clears it once open. */
  tasksTabToOpen: string | null;
  /** The task the Tasks tab should scroll to and highlight; the tab clears it once shown. */
  taskToShow: { conversationId: string; stepId: string } | null;
  /** True when the agent, not a click, asked for the tab: a phone's drawer then stays shut. */
  tasksTabAutoOpen: boolean;
}

/**
 * Actions that cross surfaces for a session's tasks: the composer registers
 * how to start a workflow, so the Tasks tab's empty state can link to it, and
 * the chat's task preview asks the shell to open the Tasks tab.
 */
export const useSessionTasksActions = create<SessionTasksActionsState>(() => ({
  startWorkflow: {},
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
  useSessionTasksActions.setState((state) => ({
    startWorkflow: { ...state.startWorkflow, [conversationId]: start },
  }));
  return () =>
    useSessionTasksActions.setState((state) => {
      if (state.startWorkflow[conversationId] !== start) return state;
      return {
        startWorkflow: Object.fromEntries(
          Object.entries(state.startWorkflow).filter(([id]) => id !== conversationId),
        ),
      };
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

/** The agent made the session's first plan: show its Tasks tab beside the chat, not over it on a phone. */
export function showTasksTabForNewPlan(conversationId: string): void {
  useSessionTasksActions.setState({
    tasksTabToOpen: conversationId,
    taskToShow: null,
    tasksTabAutoOpen: true,
  });
}
