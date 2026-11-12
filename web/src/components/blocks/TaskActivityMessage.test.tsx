import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { useSessionTasksActions } from "@/store/sessionTasksActions";
import { ActivityNotice, TaskMarkersProvider } from "./TaskActivityMessage";

function taskMarker(status: string, fields: Record<string, unknown> = {}) {
  return {
    type: "resource_event",
    event_type: "session.journal.task",
    resource_type: "journal",
    resource_id: "deploy",
    resource: {
      name: "Deploy the swap",
      status,
      text: null,
      entry_id: "e1",
      native: false,
      ...fields,
    },
  };
}

const PLAN_LINE = {
  type: "resource_event",
  event_type: "session.journal.plan",
  resource_type: "journal",
  resource_id: "e0",
  resource: {
    change: "made",
    name: "Ship the demo",
    tasks: 3,
    added: [],
    removed: [],
    entry_id: "e0",
  },
};

function renderMarker(data: Record<string, unknown>, taskIds: string[] | null = ["deploy"]) {
  const notice = <ActivityNotice data={data} />;
  return render(
    <MemoryRouter>
      {taskIds ? (
        <TaskMarkersProvider value={{ conversationId: "conv", taskIds: new Set(taskIds) }}>
          {notice}
        </TaskMarkersProvider>
      ) : (
        notice
      )}
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  useSessionTasksActions.setState({ tasksTabToOpen: null, taskToShow: null });
});

describe("TaskActivityMessage", () => {
  it("names what happened to the task and opens the Tasks tab at it", () => {
    renderMarker(taskMarker("done"));
    const marker = screen.getByTestId("task-activity");
    expect(marker.textContent).toBe("Completed Deploy the swap");
    expect(marker.dataset.messageId).toBe("journal-e1");
    fireEvent.click(screen.getByRole("button", { name: "Deploy the swap" }));
    expect(useSessionTasksActions.getState()).toMatchObject({
      tasksTabToOpen: "conv",
      taskToShow: { conversationId: "conv", stepId: "deploy" },
    });
  });

  it("says when a task waits for you, in pink", () => {
    renderMarker(taskMarker("needs_input", { text: "Approve the deploy?" }));
    expect(screen.getByText("Waiting for you on")).toHaveClass("text-brand-accent");
  });

  it("stops linking a task its plan dropped, and says so on hover", async () => {
    renderMarker(taskMarker("done", { text: "Pod 1/1" }), ["verify"]);
    expect(screen.queryByRole("button")).toBeNull();
    fireEvent.focus(screen.getByText("Deploy the swap"));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Pod 1/1 · No longer in the plan");
  });

  it("shows a plan change as one line that opens the Tasks tab", () => {
    renderMarker(PLAN_LINE);
    fireEvent.click(screen.getByRole("button", { name: "Made a plan: Ship the demo · 3 tasks" }));
    expect(useSessionTasksActions.getState()).toMatchObject({
      tasksTabToOpen: "conv",
      taskToShow: null,
    });
  });

  it("is plain text outside a session's own chat", () => {
    renderMarker(taskMarker("started"), null);
    expect(screen.getByTestId("task-activity").textContent).toBe("Started Deploy the swap");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("still shows a sub-agent's marker", () => {
    renderMarker({
      type: "resource_event",
      event_type: "session.subagent.delegated",
      resource_type: "session",
      resource_id: "child",
      resource: { title: "Reviewer" },
    });
    expect(screen.queryByTestId("task-activity")).toBeNull();
  });
});
