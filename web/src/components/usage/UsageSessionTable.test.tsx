import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UsageSessionTable } from "./UsageSessionTable";
import type { SessionUsage } from "@/lib/usageApi";

// The table only needs Link to render its children as a plain anchor; the
// routing provider is out of scope for these display-focused tests.
vi.mock("@/lib/routing", () => ({
  Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
    <a href={to}>{children}</a>
  ),
}));

function session(overrides: Partial<SessionUsage> = {}): SessionUsage {
  return {
    id: "conv_1",
    createdAt: 1_700_000_000,
    updatedAt: 1_700_000_000,
    title: "My session",
    costUsd: 1.23,
    models: {},
    harness: "claude-code",
    otherHarnesses: null,
    llmModel: null,
    agentName: null,
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("UsageSessionTable model display", () => {
  it("shows the model name without a badge for a single-model session", () => {
    render(<UsageSessionTable sessions={[session({ models: { "claude-opus-4-8": 1.23 } })]} />);
    const cell = screen.getByRole("link", { name: "My session" }).closest("td")!;
    expect(within(cell).getByText("claude-opus-4-8")).toBeInTheDocument();
    // No additional models → no +N badge.
    expect(within(cell).queryByText(/^\+\d+$/)).toBeNull();
  });

  it("shows the highest-cost model with a +N badge for a multi-model session", () => {
    render(
      <UsageSessionTable
        sessions={[
          session({
            models: {
              "claude-opus-4-8": 5.0,
              "claude-sonnet-5": 2.0,
              "claude-haiku-4-5": 0.5,
            },
          }),
        ]}
      />,
    );
    const cell = screen.getByRole("link", { name: "My session" }).closest("td")!;
    // Primary model is the highest-cost one.
    expect(within(cell).getByText("claude-opus-4-8")).toBeInTheDocument();
    // Badge counts the remaining models (3 total - 1 primary = 2).
    expect(within(cell).getByText("+2")).toBeInTheDocument();
  });

  it("lists the non-primary models in the badge tooltip, sorted by descending cost", () => {
    render(
      <UsageSessionTable
        sessions={[
          session({
            models: {
              "claude-opus-4-8": 5.0,
              "claude-haiku-4-5": 0.5,
              "claude-sonnet-5": 2.0,
            },
          }),
        ]}
      />,
    );
    const badge = screen.getByText("+2");
    expect(badge.getAttribute("title")).toBe("claude-sonnet-5, claude-haiku-4-5");
  });

  it("shows no model label and no badge when the session has no models", () => {
    render(<UsageSessionTable sessions={[session({ models: {} })]} />);
    const cell = screen.getByRole("link", { name: "My session" }).closest("td")!;
    expect(within(cell).queryByText(/^\+\d+$/)).toBeNull();
    // Only the session title link renders in the cell.
    expect(cell.textContent).toBe("My session");
  });
});
