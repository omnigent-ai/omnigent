import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  ComposerHostTrigger,
  ComposerWorkspaceBar,
  ComposerWorkspaceTrigger,
  ComposerHarnessTrigger,
  ComposerPermissionPicker,
} from "./ComposerControls";
import { COMPOSER_COLLAPSED_LABEL_CLASS } from "./ChatComposer";

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

  it("lets workspace labels use half the bar instead of a fixed pixel cap", () => {
    render(
      <ComposerWorkspaceBar>
        <ComposerWorkspaceTrigger kind="directory" label="new-composer-width" />
        <ComposerWorkspaceTrigger kind="worktree" label="feature/new-composer-width" />
      </ComposerWorkspaceBar>,
    );

    for (const trigger of screen.getAllByRole("button")) {
      expect(trigger).toHaveClass("min-w-0", "max-w-[calc(50%-0.25rem)]");
      expect(trigger).not.toHaveClass("max-w-[180px]");
      expect(trigger.querySelector("span")).toHaveClass("min-w-0", "truncate");
      for (const icon of trigger.querySelectorAll("svg")) {
        expect(icon).toHaveClass("shrink-0");
      }
    }
  });

  it("renders a product-icon model trigger instead of a separate settings gear", () => {
    render(
      <ComposerHarnessTrigger
        label="Codex configuration"
        model="GPT-5.6-Sol"
        effort="High"
        icon={<span data-testid="product-icon" />}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Codex configuration" });
    expect(trigger).toHaveTextContent("GPT-5.6-Sol");
    expect(trigger).toHaveClass("w-auto");
    expect(trigger).toHaveClass("px-2", "py-0", "border-0", "leading-5", "md:min-h-7");
    expect(trigger).not.toHaveClass("pr-0");
    expect(trigger).not.toHaveClass("max-w-[7.25rem]", "md:max-w-40");
    expect(trigger).toHaveTextContent("High");
    expect(screen.getByTestId("composer-agent-model-value")).not.toHaveClass("truncate");
    expect(screen.getByTestId("composer-agent-effort-value")).not.toHaveClass("hidden");
    expect(screen.getByTestId("product-icon")).toBeInTheDocument();
    expect(screen.getByTestId("composer-agent-config-value")).toHaveClass(
      COMPOSER_COLLAPSED_LABEL_CLASS,
    );
  });

  it("keeps an icon-less model label visible when the action row collapses", () => {
    render(<ComposerHarnessTrigger label="Agent" model="No agents" />);
    expect(screen.getByTestId("composer-agent-config-value")).not.toHaveClass(
      COMPOSER_COLLAPSED_LABEL_CLASS,
    );
  });

  it("dispatches permission selections through the caller's handler", () => {
    const onSelect = vi.fn();
    render(
      <ComposerPermissionPicker
        label="Permissions"
        value="Bypass permissions"
        options={[
          { value: "manual", label: "Manual" },
          { value: "plan", label: "Plan" },
        ]}
        onSelect={onSelect}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Permissions: Bypass permissions" });
    for (const forbidden of ["hidden", "max-w-20", "truncate"]) {
      expect(screen.getByText("Bypass permissions")).not.toHaveClass(forbidden);
    }
    expect(screen.getByText("Bypass permissions")).toHaveClass(COMPOSER_COLLAPSED_LABEL_CLASS);
    expect(trigger).toHaveClass("w-auto", "gap-1", "px-2");
    fireEvent.keyDown(trigger, {
      key: "ArrowDown",
    });
    fireEvent.click(screen.getByRole("menuitem", { name: "Plan" }));
    expect(onSelect).toHaveBeenCalledWith("plan");
  });
});
