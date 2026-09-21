import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { SubagentTaskIndicator } from "./SubagentTaskIndicator";

const useChildSessionsMock = vi.fn();
vi.mock("@/hooks/useChildSessions", () => ({
  useChildSessions: (conversationId: string | null) => useChildSessionsMock(conversationId),
}));

function child(overrides: Partial<ChildSessionInfo>): ChildSessionInfo {
  return {
    id: overrides.id ?? "child-1",
    title: overrides.title ?? null,
    task_summary: overrides.task_summary ?? null,
    tool: overrides.tool ?? null,
    session_name: overrides.session_name ?? null,
    labels: overrides.labels ?? {},
    current_task_status: overrides.current_task_status ?? null,
    last_task_error: overrides.last_task_error ?? null,
    busy: overrides.busy ?? false,
    last_message_preview: overrides.last_message_preview ?? null,
    pending_elicitations_count: overrides.pending_elicitations_count ?? 0,
    routed_model: overrides.routed_model ?? null,
  };
}

function setChildren(children: ChildSessionInfo[]) {
  useChildSessionsMock.mockReturnValue({ children, isLoading: false, error: null });
}

function renderIndicator(conversationId: string | null = "conv-1", route = "/") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <SubagentTaskIndicator conversationId={conversationId} />
    </MemoryRouter>,
  );
}

beforeEach(() => setChildren([]));
afterEach(() => {
  cleanup();
  useChildSessionsMock.mockReset();
});

describe("SubagentTaskIndicator", () => {
  it("self-hides without requiring a router", () => {
    const { container } = render(<SubagentTaskIndicator conversationId={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing without a conversation", () => {
    const { container } = renderIndicator(null);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when every sub-agent is settled", () => {
    setChildren([child({ id: "done", current_task_status: "completed" })]);
    const { container } = renderIndicator();
    expect(container).toBeEmptyDOMElement();
  });

  it("counts active, parked, and errored sub-agents while excluding settled rows", () => {
    setChildren([
      child({ id: "working", busy: true }),
      child({ id: "parked", busy: true, pending_elicitations_count: 1 }),
      child({ id: "failed", current_task_status: "failed" }),
      child({ id: "done", current_task_status: "completed" }),
    ]);
    renderIndicator();

    const pill = screen.getByTestId("subagent-task-pill");
    expect(pill).toHaveTextContent("3");
    expect(pill).toHaveClass("px-1", "md:px-2", "text-destructive");
    expect(pill).toHaveAttribute("data-state", "error");
    expect(pill).toHaveAccessibleName(
      "3 sub-agents: 1 active, 1 awaiting input, 1 needs attention",
    );
  });

  it("shows state labels and honest navigation targets in the popover", () => {
    setChildren([
      child({ id: "working", busy: true, task_summary: "Investigate auth", tool: "researcher" }),
      child({ id: "parked", pending_elicitations_count: 1, session_name: "docs" }),
      child({
        id: "failed",
        current_task_status: "failed",
        last_task_error: { code: "tool_error", message: "Tool failed" },
        title: "Review failure",
      }),
    ]);
    renderIndicator("conv-1", "/c/conv-1?file=README.md&debug=1");
    fireEvent.click(screen.getByTestId("subagent-task-pill"));

    expect(screen.getByRole("status", { name: "Working" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Needs response" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Failed" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Investigate auth sub-agent" })).toHaveAttribute(
      "href",
      "/c/working?debug=1",
    );
  });

  it("uses the parked trigger treatment when no child has an error", () => {
    setChildren([child({ id: "parked", busy: true, pending_elicitations_count: 1 })]);
    renderIndicator();
    const pill = screen.getByTestId("subagent-task-pill");
    expect(pill).toHaveAttribute("data-state", "parked");
    expect(pill).toHaveClass("text-warning");
    expect(pill).toHaveAccessibleName("1 sub-agent: 1 awaiting input");
  });
});
