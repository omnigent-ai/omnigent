import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { RoutingDecisionCard } from "./StatusBlocks";

afterEach(cleanup);

describe("RoutingDecisionCard — session-level auto-routing", () => {
  it("applied verdict: shows model pill with tier and rationale", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-opus-4-8"
        applied
        rationale="Multi-file refactor needs deep reasoning."
      />,
    );
    const card = screen.getByTestId("routing-decision-card");
    expect(card).toHaveTextContent("Smart routing");
    expect(card).toHaveTextContent("· applied");
    expect(card).toHaveTextContent("Session");
    expect(card).toHaveTextContent("opus");
    expect(card).toHaveTextContent("Multi-file refactor needs deep reasoning.");
    expect(card.getAttribute("data-applied")).toBe("true");
  });

  it("advisory verdict: shows '· advisory' and the model that would have been picked", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-haiku-4-5"
        applied={false}
        rationale="Trivial question."
      />,
    );
    const card = screen.getByTestId("routing-decision-card");
    expect(card).toHaveTextContent("· advisory");
    expect(card).toHaveTextContent("haiku");
    expect(card.getAttribute("data-applied")).toBe("false");
  });

  it("shows agent name as row label when mirrored into parent session", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-haiku-4-5"
        applied
        rationale="Simple task."
        agent="claude_code"
      />,
    );
    const card = screen.getByTestId("routing-decision-card");
    // The agent name replaces the generic "Session" label so the
    // orchestrator's transcript identifies which sub-agent was routed.
    expect(card).toHaveTextContent("claude_code");
    expect(card.textContent).not.toContain("Session");
  });

  it("expands raw verdict JSON behind the chevron", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-opus-4-8"
        applied
        rationale="Deep reasoning required."
      />,
    );
    // Collapsed by default — raw JSON not visible.
    expect(screen.queryByText(/"rationale"/)).toBeNull();
    fireEvent.click(screen.getByTestId("routing-decision-raw-toggle"));
    expect(screen.getByText(/"rationale"/)).toBeInTheDocument();
  });
});

describe("routing decision — harness / scope / raw pick", () => {
  // Harness + which sub-agent the decision covers: without the badge a
  // native-subagent decision is indistinguishable from a session one, and a
  // badge on a session/turn decision would invent a sub-agent that has none.
  it.each([
    ["native_subagent", "subagent: researcher"],
    ["turn", null],
    ["session", null],
  ] as const)("card: %s scope renders the sub-agent badge as %s", (scope, badge) => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-sonnet-5"
        applied
        rationale="short task"
        agent="researcher"
        routing={{ harness: "claude-native", scope }}
      />,
    );
    expect(screen.getByTestId("routing-decision-card")).toHaveTextContent("claude-native");
    if (badge === null) {
      expect(screen.queryByTestId("routing-decision-scope")).toBeNull();
    } else {
      expect(screen.getByTestId("routing-decision-scope")).toHaveTextContent(badge);
    }
  });

  // The router's vocabulary pick may have had no endpoint and been mapped to a
  // servable id — that must be visible. When it resolves to the same short
  // name there is nothing to disclose, so the row stays off.
  it.each([
    ["gpt-5-6-sol", "gpt-5-6-sol"],
    ["claude-sonnet-5", null],
  ] as const)("card: raw pick %s is disclosed as %s", (rawModel, shown) => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-sonnet-5"
        applied
        rationale="x"
        routing={{ rawModel }}
      />,
    );
    expect(screen.getByTestId("routing-decision-card")).toHaveTextContent("sonnet");
    if (shown === null) {
      expect(screen.queryByTestId("routing-decision-raw-model")).toBeNull();
    } else {
      expect(screen.getByTestId("routing-decision-raw-model")).toHaveTextContent(shown);
    }
  });

  it("card: renders harness, scope badge, raw pick, and the extras in the raw JSON", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-sonnet-5"
        applied
        rationale="short task"
        agent="researcher"
        routing={{
          harness: "codex-native",
          scope: "child_session",
          decisionId: "dec_123",
          rawModel: "gpt-5-6-sol",
          attemptedOverride: "databricks-claude-opus-4-8",
        }}
      />,
    );
    expect(screen.getByTestId("routing-decision-harness")).toHaveTextContent("codex-native");
    // The badge's exact wording is pinned once, on subagentScopeLabel.
    expect(screen.getByTestId("routing-decision-scope")).toBeTruthy();
    expect(screen.getByTestId("routing-decision-raw-model")).toHaveTextContent("gpt-5-6-sol");
    // Identity + attempted override are audit data — they belong in the
    // expandable verdict, not the glance row.
    fireEvent.click(screen.getByTestId("routing-decision-raw-toggle"));
    expect(screen.getByText(/"decision_id"/)).toBeInTheDocument();
    expect(screen.getByText(/"attempted_override"/)).toBeInTheDocument();
  });

  it("card: omits the new rows and JSON keys when the fields are absent", () => {
    render(
      <RoutingDecisionCard model="databricks-claude-opus-4-8" applied rationale="deep reasoning" />,
    );
    expect(screen.queryByTestId("routing-decision-harness")).toBeNull();
    expect(screen.queryByTestId("routing-decision-scope")).toBeNull();
    expect(screen.queryByTestId("routing-decision-raw-model")).toBeNull();
    fireEvent.click(screen.getByTestId("routing-decision-raw-toggle"));
    expect(screen.queryByText(/"harness"/)).toBeNull();
    expect(screen.queryByText(/"raw_model"/)).toBeNull();
  });
});
