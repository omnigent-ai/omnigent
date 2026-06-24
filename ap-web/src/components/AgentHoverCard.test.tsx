import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { AgentHoverCard, AgentRowTooltip } from "./AgentHoverCard";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";

function agent(overrides: Partial<AvailableAgent> = {}): AvailableAgent {
  return {
    id: "ag_1",
    name: "some-agent",
    display_name: "Some Agent",
    description: null,
    harness: null,
    model: null,
    skills: [],
    ...overrides,
  };
}

afterEach(cleanup);

// Both wrappers no-op when there's no description, and wrap the trigger
// otherwise. The flyout *body* is deferred until open (radix mounts
// content on hover/focus), so these assert the branch that decides
// whether a flyout exists at all — the part that runs at render. The
// `asChild` trigger merges its `data-slot` marker onto our child, so the
// marker's presence is the observable signal that a flyout is wired up.

describe("AgentHoverCard", () => {
  it("wraps the trigger when the agent has a description", () => {
    render(
      <AgentHoverCard agent={agent({ description: "Plans and splits up the work." })}>
        <button data-testid="trigger">Some Agent</button>
      </AgentHoverCard>,
    );
    expect(screen.getByTestId("trigger")).toHaveAttribute("data-slot", "hover-card-trigger");
  });

  it("renders the trigger bare when the agent has no description", () => {
    // Nothing to show → no wrapper, so an empty flyout can never open.
    render(
      <AgentHoverCard agent={agent({ description: null })}>
        <button data-testid="trigger">Some Agent</button>
      </AgentHoverCard>,
    );
    expect(screen.getByTestId("trigger")).not.toHaveAttribute("data-slot", "hover-card-trigger");
  });

  it("treats an empty-string description as nothing to show", () => {
    // `!agent.description` also catches "", so a blank label doesn't open
    // a flyout with an empty body.
    render(
      <AgentHoverCard agent={agent({ description: "" })}>
        <button data-testid="trigger">Some Agent</button>
      </AgentHoverCard>,
    );
    expect(screen.getByTestId("trigger")).not.toHaveAttribute("data-slot", "hover-card-trigger");
  });
});

describe("AgentRowTooltip", () => {
  it("wraps the row content when the agent has a description", () => {
    render(
      <TooltipProvider>
        <AgentRowTooltip agent={agent({ description: "Plans and splits up the work." })}>
          <div data-testid="row">Some Agent</div>
        </AgentRowTooltip>
      </TooltipProvider>,
    );
    // A tooltip (not a hover card) is used inside dropdown rows because it
    // opens reliably while a menu is open — so the marker here is the
    // tooltip trigger, not the hover-card one.
    expect(screen.getByTestId("row")).toHaveAttribute("data-slot", "tooltip-trigger");
  });

  it("renders the row content bare when the agent has no description", () => {
    render(
      <TooltipProvider>
        <AgentRowTooltip agent={agent({ description: null })}>
          <div data-testid="row">Some Agent</div>
        </AgentRowTooltip>
      </TooltipProvider>,
    );
    expect(screen.getByTestId("row")).not.toHaveAttribute("data-slot", "tooltip-trigger");
  });
});
