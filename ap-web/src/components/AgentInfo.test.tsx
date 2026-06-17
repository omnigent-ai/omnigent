import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Agent } from "@/hooks/useAgents";
import { useChatStore } from "@/store/chatStore";

// Mock the policies data layer so SessionPoliciesSection and AddPolicyDialog
// render deterministically without network. The add/delete mutations expose
// `mutate` spies we can assert on.
const addMutate = vi.fn();
const deleteMutate = vi.fn();
const policiesData = { current: [] as unknown[] };
const registryData = { current: [] as unknown[] };
vi.mock("@/hooks/usePolicies", () => ({
  usePolicies: () => ({ data: policiesData.current }),
  usePolicyRegistry: () => ({ data: registryData.current }),
  useAddPolicy: () => ({ mutate: addMutate, isPending: false, isError: false, error: null }),
  useDeletePolicy: () => ({ mutate: deleteMutate }),
}));

import { AgentInfoButton, AgentInfoContent, agentDisplayLabel } from "./AgentInfo";

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
    expect(screen.getByText("Databricks_coding_agent")).toBeInTheDocument();
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
    expect(screen.getByText("Claude")).toBeInTheDocument();
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
    expect(screen.getByText("Databricks_coding_agent")).toBeInTheDocument();
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
    expect(screen.getByText("Databricks_coding_agent")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-info-usage-by-model")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// SessionPoliciesSection + AddPolicyDialog, rendered via AgentInfoContent
// (no popover trigger needed) with the policies data layer mocked.
// ---------------------------------------------------------------------------

function renderContent(sessionId: string) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <AgentInfoContent agent={AGENT_WITH_BOTH} sessionId={sessionId} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("SessionPoliciesSection", () => {
  beforeEach(() => {
    addMutate.mockReset();
    deleteMutate.mockReset();
    policiesData.current = [];
    registryData.current = [];
  });

  it("shows the empty state when no user policies are applied", () => {
    // WHY: only `source === "session"` policies are user-managed; a spec
    // policy must not count, so the section reads "No policies added".
    policiesData.current = [{ id: "p_spec", name: "spec_one", handler: "h.spec", source: "spec" }];
    renderContent("conv_pol");
    expect(screen.getByText("No policies added")).toBeInTheDocument();
  });

  it("lists user policies and deletes one via the popover Remove button", () => {
    // WHY: a session-sourced policy renders as a pill; opening it and clicking
    // Remove must call deletePolicy.mutate with the policy id.
    policiesData.current = [
      { id: "p1", name: "deny_pii", handler: "guard.pii", source: "session" },
    ];
    renderContent("conv_pol");

    fireEvent.click(screen.getByRole("button", { name: /deny_pii/ }));
    fireEvent.click(screen.getByRole("button", { name: /Remove/ }));
    expect(deleteMutate).toHaveBeenCalledWith("p1");
  });

  it("filters the registry list and adds a callable policy", () => {
    // WHY: the add dialog filters available (not-yet-applied) policies by
    // name/description, and a callable policy adds with no factory_params.
    registryData.current = [
      { handler: "h.alpha", kind: "callable", name: "Alpha Guard", description: "blocks alpha" },
      { handler: "h.beta", kind: "callable", name: "Beta Guard", description: "blocks beta" },
    ];
    renderContent("conv_pol");

    fireEvent.click(screen.getByTitle("Add policy"));
    const dialog = screen.getByRole("dialog");
    // Filter to just Beta.
    fireEvent.change(within(dialog).getByPlaceholderText("Filter policies..."), {
      target: { value: "beta" },
    });
    expect(within(dialog).queryByText("Alpha Guard")).toBeNull();
    fireEvent.click(within(dialog).getByText("Beta Guard"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Add" }));

    expect(addMutate).toHaveBeenCalledWith(
      expect.objectContaining({ name: "beta_guard", type: "python", handler: "h.beta" }),
      expect.anything(),
    );
    // Callable kind sends no factory_params.
    expect(addMutate.mock.calls[0][0]).not.toHaveProperty("factory_params");
  });

  it("renders factory params and submits coerced values", () => {
    // WHY: a factory policy with a params schema renders inputs and sends
    // factory_params (always present for factory kind) on Add.
    registryData.current = [
      {
        handler: "h.factory",
        kind: "factory",
        name: "PII Factory",
        description: "configurable",
        params_schema: {
          properties: {
            threshold: { type: "integer", default: 5 },
            strict: { type: "boolean", default: true },
          },
          required: [],
        },
      },
    ];
    renderContent("conv_pol");

    fireEvent.click(screen.getByTitle("Add policy"));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByText("PII Factory"));

    // The integer param input is present (number type).
    const numberInput = within(dialog).getByPlaceholderText("5") as HTMLInputElement;
    fireEvent.change(numberInput, { target: { value: "9" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add" }));

    expect(addMutate).toHaveBeenCalledTimes(1);
    const payload = addMutate.mock.calls[0][0];
    expect(payload).toHaveProperty("factory_params");
    expect(payload.handler).toBe("h.factory");
  });

  it("shows the all-applied empty message when every registry policy is already added", () => {
    // WHY: when appliedHandlers covers the whole registry the filtered list is
    // empty AND available.length === 0, so the dialog says all are applied.
    registryData.current = [
      { handler: "h.alpha", kind: "callable", name: "Alpha Guard", description: "blocks alpha" },
    ];
    policiesData.current = [
      { id: "pa", name: "alpha_guard", handler: "h.alpha", source: "session" },
    ];
    renderContent("conv_pol");

    fireEvent.click(screen.getByTitle("Add policy"));
    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByText("All available policies are already applied."),
    ).toBeInTheDocument();
  });
});

describe("agentDisplayLabel", () => {
  it("maps native wrapper slugs to their display name", () => {
    expect(agentDisplayLabel("pi-native-ui")).toBe("Pi");
    expect(agentDisplayLabel("claude-native-ui")).toBe("Claude");
    expect(agentDisplayLabel("codex-native-ui")).toBe("Codex");
  });

  it("strips the fork/switch clone suffix before resolving the native label", () => {
    // Fork/switch routes clone a bound agent as "<name> (fork|switch <id>)".
    // The label must still resolve to "Pi" rather than the capitalized raw
    // slug "Pi-native-ui …" shown in the in-session model picker.
    expect(agentDisplayLabel("pi-native-ui (fork conv_ab12)")).toBe("Pi");
    expect(agentDisplayLabel("pi-native-ui (switch conv_ab12)")).toBe("Pi");
    expect(agentDisplayLabel("claude-native-ui (fork conv_ab12)")).toBe("Claude");
    expect(agentDisplayLabel("codex-native-ui (switch conv_ab12)")).toBe("Codex");
  });

  it("strips EVERY clone layer of a fork-of-a-fork before resolving", () => {
    // A fork of a fork nests suffixes. A single-layer strip would leave
    // "pi-native-ui (fork conv_a)" — no native match → the raw slug leaks
    // into the model picker. agentRootName peels every layer to the root.
    expect(agentDisplayLabel("pi-native-ui (fork conv_a) (fork conv_b)")).toBe("Pi");
    expect(agentDisplayLabel("claude-native-ui (fork conv_a) (switch conv_b)")).toBe("Claude");
    expect(agentDisplayLabel("polly (fork conv_a) (fork conv_b)")).toBe("Polly");
  });

  it("capitalizes non-native names and strips their clone suffix", () => {
    expect(agentDisplayLabel("polly")).toBe("Polly");
    expect(agentDisplayLabel("polly (fork conv_ab12)")).toBe("Polly");
  });
});
