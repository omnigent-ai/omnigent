import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
    labels: {},
    current_task_status: null,
    last_task_error: null,
    busy: overrides.busy ?? false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    routed_model: null,
  };
}

function setChildren(children: ChildSessionInfo[]) {
  useChildSessionsMock.mockReturnValue({ children, isLoading: false, error: null });
}

beforeEach(() => setChildren([]));
afterEach(() => {
  cleanup();
  useChildSessionsMock.mockReset();
});

describe("SubagentTaskIndicator", () => {
  it("renders nothing without a conversation", () => {
    const { container } = render(<SubagentTaskIndicator conversationId={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when no sub-agent is active", () => {
    // WHY: only busy children count — a finished child is not a running task.
    setChildren([child({ id: "a", busy: false })]);
    const { container } = render(<SubagentTaskIndicator conversationId="conv-1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("counts only busy sub-agents with a singular accessible name", () => {
    setChildren([child({ id: "a", busy: true }), child({ id: "b", busy: false })]);
    render(<SubagentTaskIndicator conversationId="conv-1" />);
    const pill = screen.getByTestId("subagent-task-pill");
    expect(pill).toHaveTextContent("1");
    expect(pill).toHaveAttribute("aria-label", "1 sub-agent running");
  });

  it("pluralizes the count and lists active sub-agents on open", () => {
    setChildren([
      child({ id: "a", busy: true, task_summary: "Investigate auth flow", tool: "researcher" }),
      child({ id: "b", busy: true, session_name: "docs" }),
    ]);
    render(<SubagentTaskIndicator conversationId="conv-1" />);
    const pill = screen.getByTestId("subagent-task-pill");
    expect(pill).toHaveAttribute("aria-label", "2 sub-agents running");
    fireEvent.click(pill);
    expect(screen.getByText("Investigate auth flow")).toBeInTheDocument();
    expect(screen.getByText("docs")).toBeInTheDocument();
  });
});
