import type { ComponentType, ReactNode } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { StepProgress } from "@/lib/sessionJournal";
import type { WorkflowStep } from "@/lib/workflowGraphLayout";

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

  it("says which failed step keeps a step from starting, in place of its goal", () => {
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
    expect(fix).toHaveAttribute("title", "The suite passes");
  });
});
