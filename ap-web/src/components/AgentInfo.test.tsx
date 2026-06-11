import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Agent } from "@/hooks/useAgents";
import { useChatStore } from "@/store/chatStore";
import { AgentInfoButton } from "./AgentInfo";

afterEach(() => {
  cleanup();
});

function renderButton(agent: Agent | undefined) {
  return render(
    <TooltipProvider>
      <AgentInfoButton agent={agent} />
    </TooltipProvider>,
  );
}

/**
 * Render the info button bound to a session. A sessionId pulls in the
 * policies section (react-query), so wrap in a QueryClientProvider with
 * retries off — the policy fetch failing in jsdom is irrelevant to the
 * cost row under test and must not crash the render.
 */
function renderButtonWithSession(agent: Agent | undefined, sessionId: string) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <AgentInfoButton agent={agent} sessionId={sessionId} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

const AGENT_WITH_BOTH: Agent = {
  id: "agent_1",
  name: "databricks_coding_agent",
  description: "Codes against Databricks.",
  mcp_servers: [
    { name: "slack", transport: "http", description: "Slack MCP", url: "https://example/slack" },
    { name: "jira", transport: "stdio", command: "jira-mcp" },
  ],
  policies: [
    { name: "slack_policy", type: "function", on: ["tool_call"], description: "guard.slack" },
  ],
};

describe("AgentInfoButton", () => {
  it("renders nothing when the agent has no tools and no policies", () => {
    // An inert info icon over an empty popover is pure header noise — the
    // button must self-hide when there is nothing to surface.
    renderButton({ id: "a", name: "bare", mcp_servers: [], policies: [] });
    expect(screen.queryByTestId("agent-info-trigger")).toBeNull();
  });

  it("renders nothing while the agent is still loading (undefined)", () => {
    renderButton(undefined);
    expect(screen.queryByTestId("agent-info-trigger")).toBeNull();
  });

  it("hides the trigger when only spec policies are configured and no sessionId", () => {
    renderButton({
      id: "a",
      name: "policed",
      policies: [{ name: "block_sleep", type: "function", on: ["tool_call"] }],
    });
    expect(screen.queryByTestId("agent-info-trigger")).toBeNull();
  });

  it("reveals the agent name, MCP servers, and policies on click", () => {
    renderButton(AGENT_WITH_BOTH);
    // Closed popover: content is not in the DOM yet.
    expect(screen.queryByText("slack")).toBeNull();

    fireEvent.click(screen.getByTestId("agent-info-trigger"));

    // Name header plus every server and policy name proves the full
    // agent object flowed into the popover (not just structure).
    expect(screen.getByText("databricks_coding_agent")).toBeInTheDocument();
    expect(screen.getByText("Codes against Databricks.")).toBeInTheDocument();
    expect(screen.getByText("slack")).toBeInTheDocument();
    expect(screen.getByText("jira")).toBeInTheDocument();
    // Session policies render via SessionPoliciesSection when sessionId is passed.
  });

  it("maps native agent names to their friendly aliases in the header", () => {
    renderButton({
      id: "claude_1",
      name: "claude-native-ui",
      mcp_servers: [{ name: "tools", transport: "http" }],
    });
    fireEvent.click(screen.getByTestId("agent-info-trigger"));
    expect(screen.getByText("claude")).toBeInTheDocument();
    expect(screen.queryByText("claude-native-ui")).toBeNull();
  });
});

describe("AgentInfoButton session cost row", () => {
  // The per-session cost lives in the info popover (moved out of the
  // composer status line). It reads from the shared chat store, so reset
  // the field between cases to keep them independent.
  beforeEach(() => {
    useChatStore.setState({ sessionCostUsd: null });
  });

  it("shows the formatted session cost in the popover when priced", () => {
    useChatStore.setState({ sessionCostUsd: 1.234 });
    renderButtonWithSession(AGENT_WITH_BOTH, "conv_cost");
    // Closed popover: the cost row is not mounted yet.
    expect(screen.queryByTestId("agent-info-session-cost")).toBeNull();

    fireEvent.click(screen.getByTestId("agent-info-trigger"));

    // Asserts the formatted value (rounded to cents), not just presence —
    // a null/NaN cost slipping past the guard would render a garbage label.
    expect(screen.getByTestId("agent-info-session-cost")).toHaveTextContent("$1.23");
  });

  it("formats a priced sub-cent cost as <$0.01 (distinct from free)", () => {
    useChatStore.setState({ sessionCostUsd: 0.004 });
    renderButtonWithSession(AGENT_WITH_BOTH, "conv_cost");
    fireEvent.click(screen.getByTestId("agent-info-trigger"));
    expect(screen.getByTestId("agent-info-session-cost")).toHaveTextContent("<$0.01");
  });

  it("omits the cost row when the session is unpriced (null)", () => {
    // No turn priced yet → no row at all, rather than "$0.00" / "—".
    renderButtonWithSession(AGENT_WITH_BOTH, "conv_cost");
    fireEvent.click(screen.getByTestId("agent-info-trigger"));
    // The rest of the popover still renders (agent name proves it opened).
    expect(screen.getByText("databricks_coding_agent")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-info-session-cost")).toBeNull();
  });
});

describe("AgentInfoButton per-model usage breakdown", () => {
  // The breakdown reads `sessionUsageByModel` from the store; reset between
  // cases so they stay independent.
  beforeEach(() => {
    useChatStore.setState({ sessionUsageByModel: null });
  });

  it("renders per-model token buckets and cost for multiple models", () => {
    useChatStore.setState({
      sessionUsageByModel: {
        "claude-sonnet-4-6": {
          inputTokens: 12000,
          outputTokens: 3000,
          totalTokens: 15000,
          cacheReadInputTokens: null,
          cacheCreationInputTokens: null,
          totalCostUsd: 0.42,
        },
        "databricks-gpt-5-5": {
          inputTokens: 800,
          outputTokens: 200,
          totalTokens: 1000,
          cacheReadInputTokens: null,
          cacheCreationInputTokens: null,
          totalCostUsd: null,
        },
      },
    });
    renderButtonWithSession(AGENT_WITH_BOTH, "conv_models");
    fireEvent.click(screen.getByTestId("agent-info-trigger"));

    // Both model groups present, labeled by raw model id.
    expect(screen.getByTestId("agent-info-usage-by-model")).toBeInTheDocument();
    expect(screen.getByTestId("agent-info-model-claude-sonnet-4-6")).toHaveTextContent(
      "claude-sonnet-4-6",
    );
    // The dominant model (most total tokens) leads, and its compact values
    // and cost render; the unpriced model shows tokens but no Cost row.
    const gpt = screen.getByTestId("agent-info-model-databricks-gpt-5-5");
    expect(gpt).toHaveTextContent("databricks-gpt-5-5");
    expect(gpt).toHaveTextContent("1K");
    expect(gpt).not.toHaveTextContent("Cost");
  });

  it("renders a single model when only one contributed", () => {
    useChatStore.setState({
      sessionUsageByModel: {
        "claude-sonnet-4-6": {
          inputTokens: 12400,
          outputTokens: 250,
          totalTokens: 1530000,
          cacheReadInputTokens: 8000,
          cacheCreationInputTokens: 2000,
          totalCostUsd: 0.42,
        },
      },
    });
    renderButtonWithSession(AGENT_WITH_BOTH, "conv_models");
    fireEvent.click(screen.getByTestId("agent-info-trigger"));

    expect(screen.getByTestId("agent-info-usage-by-model")).toBeInTheDocument();
    expect(screen.getByTestId("agent-info-model-claude-sonnet-4-6")).toHaveTextContent(
      "claude-sonnet-4-6",
    );
  });

  it("hides the breakdown section when no usage is recorded", () => {
    renderButtonWithSession(AGENT_WITH_BOTH, "conv_models");
    fireEvent.click(screen.getByTestId("agent-info-trigger"));
    // The popover still opens (agent name proves it), but no breakdown.
    expect(screen.getByText("databricks_coding_agent")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-info-usage-by-model")).toBeNull();
  });
});
