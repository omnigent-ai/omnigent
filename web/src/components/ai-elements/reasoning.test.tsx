import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Reasoning, ReasoningContent, ReasoningTrigger } from "./reasoning";

afterEach(cleanup);

function renderReasoning(isStreaming: boolean, expandable = true) {
  return render(
    <Reasoning isStreaming={isStreaming} expandable={expandable}>
      <ReasoningTrigger />
      <ReasoningContent>Some reasoning text</ReasoningContent>
    </Reasoning>,
  );
}

describe("Reasoning — auto-expand", () => {
  it("blocks external image markdown and renders a placeholder", async () => {
    render(
      <Reasoning isStreaming={true}>
        <ReasoningTrigger />
        <ReasoningContent>{"![leak](https://attacker.example/pixel.png)"}</ReasoningContent>
      </Reasoning>,
    );

    expect(document.querySelector('img[src^="https://attacker.example"]')).toBeNull();
    expect(await screen.findByText("[Image blocked: leak]")).toBeTruthy();
  });

  it("renders the trigger button in the open state when isStreaming=true on mount", () => {
    renderReasoning(true);
    const trigger = screen.getByRole("button");
    expect(trigger.getAttribute("data-state")).toBe("open");
  });

  it("shows the shimmer 'Thinking...' label while streaming", () => {
    renderReasoning(true);
    expect(screen.getByText("Thinking...").className).toContain("text-shimmer");
  });

  it("renders the settled 'Thought for...' label without the shimmer", () => {
    renderReasoning(false);
    expect(screen.getByText("Thought for a few seconds").className).not.toContain("text-shimmer");
  });

  it("renders the trigger in the closed state when isStreaming=false on mount", () => {
    renderReasoning(false);
    const trigger = screen.getByRole("button");
    expect(trigger.getAttribute("data-state")).toBe("closed");
  });
});

describe("Reasoning — non-expandable (nothing to show)", () => {
  it("renders no interactive trigger when expandable=false", () => {
    // A settled, empty reasoning section: the header must not be a
    // clickable/focusable collapsible trigger. If this fails, the
    // `expandable=false` path is still rendering a CollapsibleTrigger
    // button — the dead "expands into nothing" affordance we removed.
    renderReasoning(false, false);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("does not render the reasoning content when expandable=false", () => {
    // Content is suppressed entirely (ReasoningContent returns null), so
    // there is no empty collapsible region under the flat header. A
    // failure here means the content body leaked into the DOM despite
    // there being nothing meaningful to expand.
    renderReasoning(false, false);
    expect(screen.queryByText("Some reasoning text")).toBeNull();
  });

  it("still shows the 'Thought for...' label as a flat header", () => {
    // The header text stays — the user should still see that the model
    // produced a (content-less) reasoning step; only the expand
    // affordance is gone.
    renderReasoning(false, false);
    expect(screen.getByText("Thought for a few seconds")).toBeTruthy();
  });
});
