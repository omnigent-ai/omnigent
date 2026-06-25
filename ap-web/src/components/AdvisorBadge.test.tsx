import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { useChatStore } from "@/store/chatStore";

import { AdvisorBadge, shortAdvisorModel } from "./AdvisorBadge";

afterEach(cleanup);

beforeEach(() => {
  // Reset the advisor-related store fields so each case starts clean.
  useChatStore.setState({
    conversationId: null,
    costControlTier: null,
    costControlModel: null,
  });
});

function renderBadge() {
  return render(
    <TooltipProvider>
      <AdvisorBadge />
    </TooltipProvider>,
  );
}

describe("AdvisorBadge", () => {
  it("renders nothing when no advisor tier is set", () => {
    // An agent without cost_control routing (or before the advisor runs)
    // must not show a pill.
    useChatStore.setState({ conversationId: "conv_1", costControlTier: null });
    const { container } = renderBadge();
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when there is no active conversation", () => {
    useChatStore.setState({
      conversationId: null,
      costControlTier: "cheap",
      costControlModel: "databricks-claude-haiku-4-5",
    });
    const { container } = renderBadge();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the short model name and accent styling for the expensive tier", () => {
    useChatStore.setState({
      conversationId: "conv_1",
      costControlTier: "expensive",
      costControlModel: "databricks-claude-opus-4-7",
    });
    renderBadge();
    // Pill shows the model with the databricks- prefix stripped.
    expect(screen.getByText("claude-opus-4-7")).toBeInTheDocument();
    // aria-label names the tier + full model; expensive tier uses the accent color.
    const pill = screen.getByLabelText(
      /routed this session to the expensive tier using databricks-claude-opus-4-7/i,
    );
    // text-primary = accent (expensive); a regression to muted would drop this class.
    expect(pill.className).toContain("text-primary");
  });

  it("uses muted styling for the cheap tier", () => {
    useChatStore.setState({
      conversationId: "conv_1",
      costControlTier: "cheap",
      costControlModel: "databricks-claude-haiku-4-5",
    });
    renderBadge();
    const pill = screen.getByLabelText(/cheap tier/i);
    expect(pill.className).toContain("text-muted-foreground");
    expect(pill.className).not.toContain("text-primary");
  });

  it("falls back to the tier word when no model label is present (legacy session)", () => {
    // Sessions judged before the cost_control.model label existed carry only
    // the tier; the pill still renders, labelled by tier.
    useChatStore.setState({
      conversationId: "conv_1",
      costControlTier: "cheap",
      costControlModel: null,
    });
    renderBadge();
    expect(screen.getByText("cheap")).toBeInTheDocument();
  });
});

describe("shortAdvisorModel", () => {
  it("strips the databricks- prefix", () => {
    expect(shortAdvisorModel("databricks-claude-opus-4-7")).toBe("claude-opus-4-7");
  });

  it("passes through non-databricks ids unchanged", () => {
    expect(shortAdvisorModel("gpt-5.4")).toBe("gpt-5.4");
  });
});
