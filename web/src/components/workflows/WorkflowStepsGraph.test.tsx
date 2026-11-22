import type { ComponentType, ReactElement, ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { JournalEntry, StepProgress } from "@/lib/sessionJournal";
import type { WorkflowStep } from "@/lib/workflowGraphLayout";
import { taskMenuAction } from "@/shell/TasksPanel";

import {
  HOVER_CARD_OPEN_DELAY_MS,
  WorkflowGraphOverlay,
  WorkflowStepsGraph,
} from "./WorkflowStepsGraph";

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
        // React Flow disables pointer events on read-only node wrappers.
        return (
          <div key={node.id} style={{ pointerEvents: "none" }}>
            <Node id={node.id} data={node.data} />
          </div>
        );
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

// A goal written for people first, as the save-workflow skill asks.
const MARKDOWN_GOAL = [
  "**Inputs:** the failing test's name, from the run's request.",
  "",
  "**What it does:**",
  "- Reruns the test",
  "- Fixes its cause",
  "",
  "**Details:** rerun it with `pnpm test <name>`.",
].join("\n");
const MARKDOWN_STEPS: WorkflowStep[] = [STEPS[0]!, { ...STEPS[1]!, goal: MARKDOWN_GOAL }];

/** Inline code checks whether it names a workspace file, which needs a query client. */
function renderWithQueries(ui: ReactElement) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {ui}
    </QueryClientProvider>,
  );
}

/** The goal's bold labels, list items and code rendered as Markdown, with no syntax left over. */
function expectMarkdownGoal(card: HTMLElement) {
  expect(within(card).getByText("Inputs:")).toHaveAttribute("data-streamdown", "strong");
  expect(
    within(card)
      .getAllByRole("listitem")
      .map((item) => item.textContent),
  ).toEqual(["Reruns the test", "Fixes its cause"]);
  expect(within(card).getByText("pnpm test <name>").tagName).toBe("CODE");
  expect(card.textContent).not.toMatch(/\*\*|`/);
}

// One unbroken token, as a command or path in a step's goal is.
const LONG_PATH = "//services/payments/third_party/sync/source:unit_suite_with_a_long_name";

/**
 * jsdom has no layout, so check what keeps a card's text inside it: the card wraps long
 * tokens and fits the room beside its step. A `break-words` inside would let a long token
 * set its line's width past the card again.
 */
function expectTextWrapsInside(card: HTMLElement) {
  expect(card).toHaveClass(
    "wrap-anywhere",
    "max-w-[min(18rem,var(--radix-hover-card-content-available-width))]",
    "max-h-(--radix-hover-card-content-available-height)",
    "overflow-y-auto",
  );
  expect(card.querySelector(".break-words")).toBeNull();
}

/**
 * Radix can leave a hover card's open timer pending when focus opens the card
 * after a pointer enter. jsdom tears the window down before that timer fires,
 * so let pending timers run while the cards are still mounted.
 */
async function settleHoverCards() {
  await act(async () => {
    await new Promise<void>((resolve) => {
      setTimeout(resolve, HOVER_CARD_OPEN_DELAY_MS + 50);
    });
  });
}

// Runs after every test, before unmounting, so pending hover-card timers fire while mounted.
afterEach(async () => {
  await settleHoverCards();
  cleanup();
});

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
    content: { status, text },
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

  it("shows a step's full name and goal on hover, wrapped inside its card, with no menu", async () => {
    const goal = `\`${LONG_PATH}\` passes`;
    renderWithQueries(
      <WorkflowStepsGraph steps={[STEPS[0]!, { ...STEPS[1]!, goal }]} title="Triage flaky tests" />,
    );
    const fix = screen.getAllByTestId("workflow-step-node")[1]!;
    expect(within(fix).queryByTestId("workflow-step-node-menu")).toBeNull();
    expect(within(fix).getByText(`${LONG_PATH} passes`).parentElement).toHaveClass("wrap-anywhere");
    await userEvent.hover(fix);
    const card = await screen.findByTestId("workflow-step-card");
    expect(card).toHaveTextContent("Step 2 of 2");
    expect(card).toHaveTextContent("Fix the test");
    expect(card).toHaveTextContent(`${LONG_PATH} passes`);
    // The graph zooms by scaling what's inside it; the card opens outside it.
    expect(screen.getByTestId("react-flow")).not.toContainElement(card);
    expectTextWrapsInside(card);
  });

  it("previews a step's goal as plain text in its box, and shows it as Markdown on hover", async () => {
    renderWithQueries(<WorkflowStepsGraph steps={MARKDOWN_STEPS} title="Triage flaky tests" />);
    const fix = screen.getAllByTestId("workflow-step-node")[1]!;
    expect(fix).toHaveTextContent(
      "Inputs: the failing test's name, from the run's request. What it does: Reruns the test · " +
        "Fixes its cause · Details: rerun it with pnpm test <name>.",
    );
    expect(fix.textContent).not.toMatch(/\*\*|`/);
    await userEvent.hover(fix);
    expectMarkdownGoal(await screen.findByTestId("workflow-step-card"));
  });
});

describe("WorkflowGraphOverlay", () => {
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
    await userEvent.hover(fix);
    expect(await screen.findByTestId("workflow-step-card")).toHaveTextContent("The suite passes");
  });

  it("shows all of a run's step on hover: where it stands, its result and its goal", async () => {
    const run = progress("done", "failed").map((item, index) => ({
      ...item,
      step: MARKDOWN_STEPS[index]!,
    }));
    run[1] = { ...run[1]!, status: statusEntry("fix", "failed", "The upload test still fails") };
    renderWithQueries(
      <WorkflowGraphOverlay
        title="Triage flaky tests"
        steps={MARKDOWN_STEPS}
        progress={run}
        onClose={vi.fn()}
      />,
    );
    await userEvent.hover(screen.getAllByTestId("workflow-step-node")[1]!);
    const card = await screen.findByTestId("workflow-step-card");
    expect(card).toHaveTextContent("Task 2 of 2");
    expect(card).toHaveTextContent("Fix the test");
    expect(within(card).getByRole("img", { name: "Failed" })).toBeInTheDocument();
    expect(card).toHaveTextContent("The upload test still fails");
    // The whole goal follows its result, as Markdown.
    expect(within(card).getByText("Goal")).toBeInTheDocument();
    expectMarkdownGoal(card);
    expectTextWrapsInside(card);
  });

  it("says a stopped step started when its attempt did, not at its newest start", async () => {
    const now = Date.now() / 1000;
    const run = progress("stopped", "waiting");
    const newest = {
      ...statusEntry("explore", "started", "Pushed fix 7"),
      created_at: now - 1_926,
    };
    run[0] = { ...run[0]!, status: newest, startedAt: now - 14_840 };
    render(
      <WorkflowGraphOverlay
        title="Triage flaky tests"
        steps={STEPS}
        progress={run}
        onClose={vi.fn()}
      />,
    );
    await userEvent.hover(screen.getAllByTestId("workflow-step-node")[0]!);
    expect(await screen.findByTestId("workflow-step-card")).toHaveTextContent(
      "Stopped · started 4h ago",
    );
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
