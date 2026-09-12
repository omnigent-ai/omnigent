import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { ComposerContextRing } from "./ComposerContextRing";

function renderRing(contextWindow: number | null, tokensUsed: number | null) {
  return render(
    <TooltipProvider>
      <ComposerContextRing contextWindow={contextWindow} tokensUsed={tokensUsed} />
    </TooltipProvider>,
  );
}

afterEach(cleanup);

describe("ComposerContextRing", () => {
  it("renders nothing when the context window is unknown or zero", () => {
    expect(renderRing(null, 100).container).toBeEmptyDOMElement();
    expect(renderRing(0, 100).container).toBeEmptyDOMElement();
  });

  it("renders nothing when tokensUsed is unknown", () => {
    expect(renderRing(1000, null).container).toBeEmptyDOMElement();
  });

  it("shows the used percentage, rounded", () => {
    renderRing(1000, 123);
    expect(screen.getByTestId("composer-context-ring")).toHaveTextContent("12%");
    expect(screen.getByTestId("composer-context-ring")).toHaveClass("gap-1");
    expect(screen.getByLabelText("12% of context used")).toBeInTheDocument();
  });

  it("fits the SVG to the painted ring so its padding does not widen the label gap", () => {
    renderRing(1000, 490);
    const svg = screen.getByTestId("composer-context-ring").querySelector("svg");
    expect(svg).toHaveAttribute("viewBox", "1.5 1.5 13 13");
    expect(svg).toHaveAttribute("width", "13");
    expect(svg).toHaveAttribute("height", "13");
  });

  it("clamps over-full usage to 100%", () => {
    renderRing(1000, 4000);
    expect(screen.getByTestId("composer-context-ring")).toHaveTextContent("100%");
  });

  it("stays grayscale — never warning/destructive — even at high usage", () => {
    // WHY: the bar is ambient status, not an alarm (#7024). A near-full
    // context must not paint the old warning/destructive colors.
    renderRing(1000, 950);
    const ring = screen.getByTestId("composer-context-ring");
    expect(ring).toHaveClass("text-muted-foreground");
    expect(ring.className).not.toMatch(/text-(warning|destructive)/);
  });
});
