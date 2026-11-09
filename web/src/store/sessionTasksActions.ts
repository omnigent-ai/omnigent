import { create } from "zustand";

interface SessionTasksActionsState {
  /** Per session: how its composer starts a workflow, when it offers one. */
  startWorkflow: Readonly<Record<string, () => void>>;
  /** A session whose Tasks tab should open; the shell clears it once open. */
  tasksTabToOpen: string | null;
}

/**
 * Actions that cross surfaces for a session's tasks: the composer registers
 * how to start a workflow, so the Tasks tab's empty state can link to it, and
 * the chat's task preview asks the shell to open the Tasks tab.
 */
export const useSessionTasksActions = create<SessionTasksActionsState>(() => ({
  startWorkflow: {},
  tasksTabToOpen: null,
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

/** Ask the shell to show a session's Tasks tab: the rail, or the drawer on a phone. */
export function openTasksTab(conversationId: string): void {
  useSessionTasksActions.setState({ tasksTabToOpen: conversationId });
}
