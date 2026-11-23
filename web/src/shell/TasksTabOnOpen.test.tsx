import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionTasksActions } from "@/store/sessionTasksActions";

import { TasksTabOnOpen } from "./TasksTabOnOpen";

const sessionTasks = vi.hoisted(() => ({ progress: [] as unknown[], loading: true }));
const chat = vi.hoisted(() => ({
  conversationId: null as string | null,
  loadingConversation: true,
}));
vi.mock("@/hooks/useSessionTasks", () => ({ useSessionTasks: () => sessionTasks }));
vi.mock("@/store/chatStore", () => ({
  useChatStore: <T,>(select: (state: typeof chat) => T) => select(chat),
}));

const tasks = (count: number) => Array.from({ length: count }, (_, index) => ({ index }));
const tabToOpen = () => useSessionTasksActions.getState().tasksTabToOpen;

describe("TasksTabOnOpen", () => {
  beforeEach(() => {
    useSessionTasksActions.setState({
      tasksTabToOpen: null,
      taskToShow: null,
      tasksTabRequestedBy: "click",
    });
    Object.assign(sessionTasks, { progress: [], loading: true });
    Object.assign(chat, { conversationId: null, loadingConversation: true });
  });
  afterEach(cleanup);

  it("shows an opened session's Tasks tab once its tasks are known", () => {
    const { rerender } = render(<TasksTabOnOpen conversationId="a" skip={false} />);
    expect(tabToOpen()).toBeNull();

    // The journal is in, but the snapshot with the agent's own tasks isn't yet.
    Object.assign(sessionTasks, { progress: tasks(2), loading: false });
    Object.assign(chat, { conversationId: "a" });
    rerender(<TasksTabOnOpen conversationId="a" skip={false} />);
    expect(tabToOpen()).toBeNull();

    Object.assign(chat, { loadingConversation: false });
    rerender(<TasksTabOnOpen conversationId="a" skip={false} />);
    // Only the tab: the rail keeps its restored state, and a phone keeps the chat in view.
    expect(useSessionTasksActions.getState()).toMatchObject({
      tasksTabToOpen: "a",
      tasksTabRequestedBy: "session-open",
    });
  });

  it("keeps a click's request for the session it opens", () => {
    const click = {
      tasksTabToOpen: "a",
      taskToShow: { conversationId: "a", stepId: "step-1" },
      tasksTabRequestedBy: "click",
    } as const;
    useSessionTasksActions.setState(click);
    Object.assign(sessionTasks, { progress: tasks(1), loading: false });
    Object.assign(chat, { conversationId: "a", loadingConversation: false });
    render(<TasksTabOnOpen conversationId="a" skip={false} />);

    expect(useSessionTasksActions.getState()).toMatchObject(click);
  });

  it("leaves a session without tasks alone, even when tasks come later", () => {
    Object.assign(sessionTasks, { progress: [], loading: false });
    Object.assign(chat, { conversationId: "a", loadingConversation: false });
    const { rerender } = render(<TasksTabOnOpen conversationId="a" skip={false} />);

    Object.assign(sessionTasks, { progress: tasks(1) });
    rerender(<TasksTabOnOpen conversationId="a" skip={false} />);
    expect(tabToOpen()).toBeNull();
  });

  it("does it each time a session opens, except from a link to something else", () => {
    Object.assign(sessionTasks, { progress: tasks(1), loading: false });
    Object.assign(chat, { conversationId: "a", loadingConversation: false });
    const { rerender } = render(<TasksTabOnOpen conversationId="a" skip />);
    expect(tabToOpen()).toBeNull();

    Object.assign(chat, { conversationId: "b" });
    rerender(<TasksTabOnOpen conversationId="b" skip={false} />);
    expect(tabToOpen()).toBe("b");

    useSessionTasksActions.setState({ tasksTabToOpen: null });
    Object.assign(chat, { conversationId: "a" });
    rerender(<TasksTabOnOpen conversationId="a" skip={false} />);
    expect(tabToOpen()).toBe("a");
  });
});
