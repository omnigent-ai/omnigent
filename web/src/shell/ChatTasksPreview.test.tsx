import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { SessionTasksView } from "@/hooks/useSessionTasks";
import { nativeTaskProgress, planSteps, stepProgress, type NativeTask } from "@/lib/sessionJournal";
import { journalEntry, journalPlan } from "@/test/journalFixtures";
import { useSessionTasksActions } from "@/store/sessionTasksActions";
import { ChatTasksPreview } from "./ChatTasksPreview";

function view(todos: NativeTask[], extra: Partial<SessionTasksView> = {}): SessionTasksView {
  return {
    plan: null,
    progress: nativeTaskProgress(todos, true),
    native: true,
    journal: {} as SessionTasksView["journal"],
    journalSupported: true,
    loading: false,
    sessionWorking: true,
    paused: null,
    ...extra,
  };
}

const TODOS: NativeTask[] = [
  { content: "Add the flag", status: "completed", activeForm: "Adding the flag" },
  { content: "Document it", status: "in_progress", activeForm: "Documenting it" },
  { content: "Run the tests", status: "pending", activeForm: "Running the tests" },
];

afterEach(() => {
  cleanup();
  useSessionTasksActions.setState({ tasksTabToOpen: null });
});

describe("ChatTasksPreview", () => {
  it("takes no space while there are no tasks, or while it's unknown whether there's a plan", () => {
    // It returns null rather than an empty bar, so the chat keeps its height.
    const { container, rerender } = render(
      <ChatTasksPreview conversationId="c" tasks={view([])} />,
    );
    expect(container.firstChild).toBeNull();
    rerender(<ChatTasksPreview conversationId="c" tasks={view(TODOS, { loading: true })} />);
    expect(container.firstChild).toBeNull();
  });

  it("previews the count and the task in progress, and opens the Tasks tab on click", () => {
    render(<ChatTasksPreview conversationId="c" tasks={view(TODOS)} />);
    const preview = screen.getByTestId("tasks-preview");
    expect(preview.textContent).toBe("Tasks1/3·Documenting it");
    expect(preview.getAttribute("aria-label")).toBe(
      "Tasks, 1 of 3 finished: Documenting it. Open the Tasks tab",
    );
    fireEvent.click(preview);
    expect(useSessionTasksActions.getState().tasksTabToOpen).toBe("c");
  });

  it("counts a failed task as finished, and says one failed", () => {
    const plan = journalPlan(0, ["measure"], ["pool"]);
    const statuses = [
      journalEntry(1, { kind: "status", step: "measure", payload: { status: "done", text: "" } }),
      journalEntry(2, { kind: "status", step: "pool", payload: { status: "failed", text: "" } }),
    ];
    const progress = stepProgress(planSteps(plan), statuses, false);
    render(
      <ChatTasksPreview
        conversationId="c"
        tasks={view([], { plan, progress, native: false, sessionWorking: false })}
      />,
    );
    expect(screen.getByTestId("tasks-preview").textContent).toBe("Tasks2/2·Finished, 1 failed");
  });

  it("leads with a task waiting for you, in pink", () => {
    const plan = journalPlan(0, ["build"], ["approve", ["build"]]);
    const statuses = [
      journalEntry(1, { kind: "status", step: "build", payload: { status: "done", text: "ok" } }),
      journalEntry(2, {
        kind: "status",
        step: "approve",
        payload: { status: "needs_input", text: "Approve the deploy?" },
      }),
    ];
    const progress = stepProgress(planSteps(plan), statuses, true);
    render(
      <ChatTasksPreview conversationId="c" tasks={view([], { plan, progress, native: false })} />,
    );
    const preview = screen.getByTestId("tasks-preview");
    expect(preview.textContent).toBe("Tasks1/2·Waiting for you: approve");
    expect(within(preview).getByText("Waiting for you")).toHaveClass("text-brand-accent");
  });

  it("names the plan in its label and, on hover or focus, its tooltip", async () => {
    const plan = journalPlan(0, ["measure"]);
    plan.payload.name = "Cut GET /orders p95 by 20%";
    const progress = stepProgress(planSteps(plan), [], true);
    render(
      <ChatTasksPreview conversationId="c" tasks={view([], { plan, progress, native: false })} />,
    );
    const preview = screen.getByTestId("tasks-preview");
    expect(preview.getAttribute("aria-label")).toBe(
      "Tasks for Cut GET /orders p95 by 20%, 0 of 1 finished: Next: measure. Open the Tasks tab",
    );
    fireEvent.focus(preview);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Cut GET /orders p95 by 20%");
  });

  it("keeps the native-shell stylesheet hook on the layout row", () => {
    // index.css moves `.chat-tasks-preview` below the safe-area-shifted header
    // in the iOS and Android shells; without the hook the header covers it.
    const { container } = render(<ChatTasksPreview conversationId="c" tasks={view(TODOS)} />);
    expect(container.firstElementChild).toHaveClass("chat-tasks-preview");
  });
});
