import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import type { WorkflowNodeSummary } from "@/hooks/useWorkflows";
import { WorkflowNodeDetails } from "./WorkflowNodeDetails";

function node(overrides: Partial<WorkflowNodeSummary> = {}): WorkflowNodeSummary {
  return {
    id: "explore_a",
    title: "explore-a",
    role: "investigate",
    deps: [],
    agent: "claude_code",
    state: "pending",
    attempt_count: 0,
    child_session_id: null,
    result: null,
    error: null,
    ...overrides,
  };
}

function renderDetails(n: WorkflowNodeSummary, search = "") {
  return render(
    <MemoryRouter>
      <WorkflowNodeDetails node={n} search={search} />
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("WorkflowNodeDetails", () => {
  it("shows metadata for every node, even before it starts", () => {
    renderDetails(node({ deps: ["root"], attempt_count: 2 }));
    expect(screen.getByText("explore-a")).toBeInTheDocument();
    expect(screen.getByText("claude_code")).toBeInTheDocument();
    expect(screen.getByText("root")).toBeInTheDocument();
    // A node with no child session tells the user why, instead of a dead click.
    expect(screen.getByText(/hasn.t started/i)).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("links to the child agent chat when the node has a session", () => {
    renderDetails(node({ child_session_id: "conv_child1", state: "running" }), "?foo=bar");
    const link = screen.getByRole("link", { name: /open agent chat/i });
    expect(link).toHaveAttribute("href", "/c/conv_child1?foo=bar");
  });

  it("renders the node error but not the raw result JSON", () => {
    renderDetails(
      node({
        state: "failed",
        error: "Agent is already processing",
        result: { verdict: "approve" },
      }),
    );
    expect(screen.getByText(/Agent is already processing/)).toBeInTheDocument();
    // The full result lives in the node's chat, not the popover.
    expect(screen.queryByText(/"verdict"/)).toBeNull();
  });
});
