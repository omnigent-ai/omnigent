import type { ComponentType, ReactNode } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { JournalEntry, StepProgress } from "@/lib/sessionJournal";
import type { WorkflowStep } from "@/lib/workflowGraphLayout";
import { taskMenuAction } from "@/shell/TasksPanel";

import { WorkflowGraphOverlay, WorkflowStepsGraph } from "./WorkflowStepsGraph";

// React Flow runs out of memory in jsdom; this stand-in draws each node with its type.
vi.mock("@xyflow/react", () => ({
  ReactFlow: ({
    nodes,
    nodeTypes,
    children,
  }: {
    nodes: { id: string; type: string; data: unknown }[];
    nodeTypes: Record<string, ComponentType<{ id: string; data: unknown }>>;
    children: ReactNode;
  }) => (
    <div data-testid="react-flow">
      {nodes.map((node) => {
        const Node = nodeTypes[node.type]!;
        return <Node key={node.id} id={node.id} data={node.data} />;
      })}
      {children}
    </div>
  ),
  Background: () => null,
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom" },
  useReactFlow: () => ({ zoomIn: vi.fn(), zoomOut: vi.fn(), fitView: vi.fn() }),
}));

const STEPS: WorkflowStep[] = [
  { id: "explore", name: "Explore the failures", goal: null, depends_on: [] },
  { id: "fix", name: "Fix the test", goal: "The suite passes", depends_on: ["explore"] },
];

/** A step's newest status, as the run's journal records it. */
function statusEntry(step: string, status: string, text: string): JournalEntry {
  return {
    id: `${step}-${status}`,
    position: 1,
    created_at: Date.parse("2026-11-12T15:05:00Z") / 1000,
    scheduled_task_run_id: null,
    step,
    author: { type: "agent" },
    kind: "status",
    payload: { status, text },
  };
}

function progress(...states: StepProgress["state"][]): StepProgress[] {
  return STEPS.map((step, index) => ({
    step,
    state: states[index]!,
    status: null,
    startedAt: null,
    native: false,
    activeForm: null,
  }));
}

describe("WorkflowStepsGraph", () => {
  afterEach(cleanup);

  it("numbers the workflow's steps, and opens them full screen", () => {
    render(<WorkflowStepsGraph steps={STEPS} title="Triage flaky tests" />);
    const nodes = screen.getAllByTestId("workflow-step-node");
    expect(nodes.map((node) => node.textContent)).toEqual([
      "1Explore the failures",
      "2Fix the testThe suite passes",
    ]);
    expect(screen.queryByTestId("workflow-graph-overlay")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "View fullscreen" }));
    const overlay = screen.getByRole("dialog", { name: "Triage flaky tests" });
    expect(within(overlay).getAllByTestId("workflow-step-node")).toHaveLength(2);
    // The workflow itself has no run, so its full-screen view has no legend.
    expect(within(overlay).queryByText("Not started")).toBeNull();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("workflow-graph-overlay")).toBeNull();
  });

  it("shows a step's full name and goal on hover, with no menu", async () => {
    render(<WorkflowStepsGraph steps={STEPS} title="Triage flaky tests" />);
    const fix = screen.getAllByTestId("workflow-step-node")[1]!;
    expect(within(fix).queryByTestId("workflow-step-node-menu")).toBeNull();
    fireEvent.pointerEnter(fix);
    const card = await screen.findByTestId("workflow-step-card");
    expect(card).toHaveTextContent("Step 2 of 2");
    expect(card).toHaveTextContent("Fix the test");
    expect(card).toHaveTextContent("The suite passes");
  });
});

describe("WorkflowGraphOverlay", () => {
  afterEach(cleanup);

  it("shows where each of a run's steps stands, with a legend", () => {
    const onClose = vi.fn();
    render(
      <WorkflowGraphOverlay
        title="Triage flaky tests"
        subtitle={<span>Fix the upload test</span>}
        steps={STEPS}
        progress={progress("done", "running")}
        onClose={onClose}
      />,
    );
    const nodes = screen.getAllByTestId("workflow-step-node");
    expect(nodes.map((node) => node.dataset.state)).toEqual(["done", "running"]);
    expect(within(nodes[1]!).getByRole("img", { name: "Running" })).toBeInTheDocument();
    expect(screen.getByText("Fix the upload test")).toBeInTheDocument();
    for (const label of ["Not started", "Running", "Waiting for you", "Done", "Failed"]) {
      expect(screen.getByText(label, { selector: "span.flex" })).toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("button", { name: "Exit fullscreen" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("says which failed step keeps a step from starting, in place of its goal", async () => {
    // The run named explore for its own case; the graph names it as its box does.
    const run = progress("failed", "waiting");
    run[0] = { ...run[0]!, step: { ...run[0]!.step, name: "Explore the upload failures" } };
    render(
      <WorkflowGraphOverlay
        title="Triage flaky tests"
        steps={STEPS}
        progress={run}
        onClose={vi.fn()}
      />,
    );
    const fix = screen.getAllByTestId("workflow-step-node")[1]!;
    expect(fix).toHaveTextContent("Can't start · Explore the failures failed");
    expect(fix).not.toHaveTextContent("The suite passes");
    // The goal is still there on hover.
    fireEvent.pointerEnter(fix);
    expect(await screen.findByTestId("workflow-step-card")).toHaveTextContent("The suite passes");
  });

  it("shows all of a run's step on hover: where it stands, its result and its goal", async () => {
    const run = progress("done", "failed");
    run[1] = { ...run[1]!, status: statusEntry("fix", "failed", "The upload test still fails") };
    render(
      <WorkflowGraphOverlay
        title="Triage flaky tests"
        steps={STEPS}
        progress={run}
        onClose={vi.fn()}
      />,
    );
    fireEvent.pointerEnter(screen.getAllByTestId("workflow-step-node")[1]!);
    const card = await screen.findByTestId("workflow-step-card");
    expect(card).toHaveTextContent("Task 2 of 2");
    expect(card).toHaveTextContent("Fix the test");
    expect(within(card).getByRole("img", { name: "Failed" })).toBeInTheDocument();
    expect(card).toHaveTextContent("The upload test still fails");
    expect(card).toHaveTextContent("Goal: The suite passes");
  });

  it("offers each step's action in its ⋯ menu, and an open menu takes the first Escape", async () => {
    const onTaskAction = vi.fn();
    const onClose = vi.fn();
    render(
      <WorkflowGraphOverlay
        title="Triage flaky tests"
        steps={STEPS}
        progress={progress("done", "failed")}
        menuActionFor={(item) => taskMenuAction(item, onTaskAction, undefined)}
        onClose={onClose}
      />,
    );
    const [explore, fix] = screen.getAllByTestId("workflow-step-node");
    // A done step has nothing to offer.
    expect(within(explore!).queryByTestId("workflow-step-node-menu")).toBeNull();
    fireEvent.pointerDown(within(fix!).getByTestId("workflow-step-node-menu"), { button: 0 });
    fireEvent.click(screen.getByTestId("workflow-step-node-retry"));
    expect(onTaskAction).toHaveBeenCalledWith("retry", "Fix the test");

    fireEvent.pointerDown(within(fix!).getByTestId("workflow-step-node-menu"), { button: 0 });
    const retry = await screen.findByTestId("workflow-step-node-retry");
    fireEvent.keyDown(retry, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("workflow-step-node-retry")).toBeNull());
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });
});
