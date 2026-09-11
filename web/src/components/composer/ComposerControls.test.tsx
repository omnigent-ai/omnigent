import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  ComposerHostTrigger,
  ComposerWorkspaceBar,
  ComposerWorkspaceTrigger,
  ComposerHarnessTrigger,
  ComposerPermissionPicker,
  ContextRing,
} from "./ComposerControls";

describe("shared composer controls", () => {
  it("uses the same workspace header and host geometry in either context", () => {
    render(
      <>
        <ComposerWorkspaceBar>
          <ComposerWorkspaceTrigger kind="directory" label="repo" />
          <ComposerWorkspaceTrigger kind="worktree" label="main" />
        </ComposerWorkspaceBar>
        <ComposerHostTrigger label="This machine" status="online" />
      </>,
    );
    expect(screen.getByRole("button", { name: "repo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "main" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "This machine" })).toHaveClass("w-11", "md:h-7");
  });

  it("renders a product-icon model trigger instead of a separate settings gear", () => {
    render(
      <ComposerHarnessTrigger
        label="Codex configuration"
        model="GPT-5.6"
        effort="High"
        icon={<span data-testid="product-icon" />}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Codex configuration" });
    expect(trigger).toHaveTextContent("GPT-5.6");
    expect(trigger).toHaveTextContent("High");
    expect(screen.getByTestId("product-icon")).toBeInTheDocument();
  });

  it("dispatches permission selections through the caller's handler", () => {
    const onSelect = vi.fn();
    render(
      <ComposerPermissionPicker
        label="Permissions"
        value="Manual"
        options={[
          { value: "manual", label: "Manual" },
          { value: "plan", label: "Plan" },
        ]}
        onSelect={onSelect}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Permissions: Manual" });
    expect(screen.getByText("Manual")).not.toHaveClass("hidden");
    expect(trigger).toHaveClass("w-auto", "gap-1", "px-2");
    fireEvent.keyDown(trigger, {
      key: "ArrowDown",
    });
    fireEvent.click(screen.getByRole("menuitem", { name: "Plan" }));
    expect(onSelect).toHaveBeenCalledWith("plan");
  });
});

describe("context ring", () => {
  it("keeps the workspace bar's neutral gray at every usage level", () => {
    // 20% / 65% / 92% cover the bands the old treatment painted gray,
    // yellow (>60%), and red (>80%); all must now stay the bar's gray.
    render(
      <TooltipProvider>
        <ContextRing contextWindow={200_000} tokensUsed={40_000} />
        <ContextRing contextWindow={200_000} tokensUsed={130_000} />
        <ContextRing contextWindow={200_000} tokensUsed={184_000} />
      </TooltipProvider>,
    );
    for (const pct of [20, 65, 92]) {
      const ring = screen.getByLabelText(`${pct}% of context used`);
      expect(ring).toHaveClass("text-muted-foreground");
      expect(ring).not.toHaveClass("text-destructive");
      expect(ring).not.toHaveClass("text-warning");
    }
  });

  it("caps the fill at 100% and merges the caller's alignment classes", () => {
    render(
      <TooltipProvider>
        <ContextRing contextWindow={128_000} tokensUsed={184_000} className="ml-auto" />
      </TooltipProvider>,
    );
    const ring = screen.getByLabelText("100% of context used");
    expect(ring).toHaveClass("ml-auto", "text-muted-foreground");
  });
});
