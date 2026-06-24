import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SessionStateBadge } from "./SessionStateBadge";
import type { SessionState } from "@/hooks/useSessionState";
import i18n from "@/i18n";

const t = i18n.getFixedT(null, "common");

function renderBadge(state: SessionState) {
  return render(
    <TooltipProvider>
      <SessionStateBadge state={state} />
    </TooltipProvider>,
  );
}

afterEach(cleanup);

describe("SessionStateBadge — per-state rendering", () => {
  it("renders awaiting as a 'Needs response' tag with a count-aware accessible label", () => {
    renderBadge({ kind: "awaiting", count: 3 });
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("data-state", "awaiting");
    expect(badge).toHaveAttribute("aria-label", t("approvalPromptsWaiting", { count: 3 }));
    // The approval indicator is a visible text tag (not an icon-only dot), so
    // it reads as "Needs response" at a glance in the row.
    expect(badge).toHaveTextContent(t("needsResponse"));
  });

  it("uses singular wording when only one prompt is pending", () => {
    renderBadge({ kind: "awaiting", count: 1 });
    expect(screen.getByTestId("session-state-badge")).toHaveAttribute(
      "aria-label",
      t("approvalPromptsWaiting", { count: 1 }),
    );
  });

  it("renders running with a pulsing brand-pink dot", () => {
    const { container } = renderBadge({ kind: "running" });
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("data-state", "running");
    // The running indicator is a single pulsing brand-pink dot; a missing dot
    // (or the old success-tone dot grid) means it regressed.
    const dot = container.querySelector('[data-testid="running-dot"]');
    expect(dot).not.toBeNull();
    expect(dot?.getAttribute("class")).toContain("running-pulse-dot");
    expect(dot?.getAttribute("class")).toContain("bg-brand-accent");
    expect(container.querySelector(".bg-success")).toBeNull();
  });

  it("renders unseen messages as a solid (non-pulsing) brand-pink dot", () => {
    const { container } = renderBadge({ kind: "unseen" });
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("aria-label", t("newMessages"));
    expect(badge).toHaveAttribute("data-state", "unseen");
    // Unread reuses the brand-pink token but stays static; the pulsing
    // variant (running-pulse-dot) is reserved for the running state.
    const dot = container.querySelector(".bg-brand-accent");
    expect(dot).not.toBeNull();
    expect(dot?.getAttribute("class")).not.toContain("running-pulse-dot");
    expect(container.querySelector(".bg-info")).toBeNull();
  });
});
