import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { childSessionsQueryKey, type ChildSessionInfo } from "@/hooks/useChildSessions";
import type { ElicitationBlock } from "@/lib/blocks";
import { useChatStore } from "@/store/chatStore";
import { TerminalSubagentApprovals } from "./TerminalSubagentApprovals";

function elicitationBlock(overrides: Partial<ElicitationBlock> = {}): ElicitationBlock {
  return {
    type: "elicitation",
    ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "resp_1", itemId: null },
    elicitationId: "elic_child",
    targetSessionId: "conv_child",
    message: "Claude wants to call Bash",
    phase: "tool_call",
    policyName: "claude-native-permission",
    contentPreview: "{}",
    requestedSchema: {},
    url: null,
    status: "pending",
    response: null,
    ...overrides,
  };
}

const CHILD_SESSION: ChildSessionInfo = {
  id: "conv_child",
  title: "claude-native:review",
  task_summary: "Commit range review",
  tool: "claude-native",
  session_name: "review",
  labels: {},
  current_task_status: "in_progress",
  busy: true,
  last_message_preview: null,
  pending_elicitations_count: 1,
};

function renderApprovals() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(childSessionsQueryKey("conv_parent"), [CHILD_SESSION]);
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TooltipProvider>
          <TerminalSubagentApprovals />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TerminalSubagentApprovals", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_parent", blocks: [] });
  });

  afterEach(() => {
    cleanup();
    useChatStore.setState({ blocks: [] });
  });

  it("shows only pending prompts mirrored from sub-agents", () => {
    useChatStore.setState({
      blocks: [
        elicitationBlock(),
        elicitationBlock({ elicitationId: "elic_own", targetSessionId: null }),
        elicitationBlock({ elicitationId: "elic_self", targetSessionId: "conv_parent" }),
        elicitationBlock({
          elicitationId: "elic_done",
          status: "responded",
          response: { action: "accept" },
        }),
      ],
    });

    renderApprovals();

    const panel = screen.getByTestId("terminal-subagent-approvals");
    const cards = within(panel).getAllByTestId("approval-card");
    expect(cards).toHaveLength(1);
    expect(cards[0]).toHaveAttribute("data-state", "pending");
    expect(cards[0]).toHaveTextContent("Claude wants to call Bash");
    expect(within(panel).getByTestId("approval-card-requester")).toHaveTextContent(
      "Requested by sub-agent Commit range review",
    );
  });

  it("clears once the sub-agent prompt is answered", () => {
    useChatStore.setState({ blocks: [elicitationBlock()] });
    renderApprovals();
    expect(screen.getByTestId("terminal-subagent-approvals")).toBeInTheDocument();

    act(() => {
      useChatStore.setState({
        blocks: [elicitationBlock({ status: "responded", response: { action: "accept" } })],
      });
    });

    expect(screen.queryByTestId("terminal-subagent-approvals")).toBeNull();
  });

  it("renders nothing when only this session's own prompts are pending", () => {
    useChatStore.setState({ blocks: [elicitationBlock({ targetSessionId: null })] });
    renderApprovals();
    expect(screen.queryByTestId("terminal-subagent-approvals")).toBeNull();
  });
});
