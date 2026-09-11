// Tests for SubagentCountTally — the "N sub-agent(s)" count shown in the
// workspace bar's right-side running-work slot next to the background-task
// tally. We mock the child-sessions hook so the busy-filtering and self-hide
// logic are exercised in isolation.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";

const h = vi.hoisted(() => ({
  children: [] as { id: string; busy: boolean }[],
}));

vi.mock("@/hooks/useChildSessions", () => ({
  useChildSessions: () => ({ children: h.children, isLoading: false, error: null }),
}));

import { SubagentCountTally } from "./ChatPage";

afterEach(() => {
  cleanup();
  h.children = [];
});

function child(id: string, busy: boolean): ChildSessionInfo {
  return {
    id,
    busy,
    title: null,
    task_summary: null,
    tool: null,
    session_name: null,
    current_task_status: null,
    last_message_preview: null,
    pending_elicitations_count: 0,
  };
}

describe("SubagentCountTally", () => {
  it("renders nothing when no sub-agent is busy", () => {
    // WHY: settled children must not leave a stale count in the bar — the
    // tally self-gates to null so the slot collapses when work finishes.
    h.children = [child("c1", false), child("c2", false)];
    const { container } = render(<SubagentCountTally sessionId="conv_1" />);
    expect(container.firstChild).toBeNull();
  });

  it("counts only busy children", () => {
    h.children = [child("c1", true), child("c2", false), child("c3", true)];
    render(<SubagentCountTally sessionId="conv_1" />);
    const tally = screen.getByTestId("subagent-count");
    expect(tally).toHaveTextContent("2 sub-agents");
    expect(tally).toHaveAttribute("aria-label", "2 sub-agents running");
  });

  it("uses the singular form for one busy sub-agent", () => {
    h.children = [child("c1", true)];
    render(<SubagentCountTally sessionId="conv_1" />);
    const tally = screen.getByTestId("subagent-count");
    expect(tally).toHaveTextContent("1 sub-agent");
    expect(tally).toHaveAttribute("aria-label", "1 sub-agent running");
  });
});
