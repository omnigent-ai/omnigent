import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  ComposerHostTrigger,
  ComposerWorkspaceBar,
  ComposerWorkspaceTrigger,
  ComposerHarnessTrigger,
  ComposerPermissionPicker,
} from "./ComposerControls";

describe("shared composer controls", () => {
  it("uses the same workspace header and host geometry in either context", () => {
    render(
      <>
        <ComposerWorkspaceBar data-testid="workspace-bar">
          <ComposerWorkspaceTrigger kind="directory" label="repo" />
          <ComposerWorkspaceTrigger kind="worktree" label="main" />
        </ComposerWorkspaceBar>
        <ComposerHostTrigger label="This machine" status="online" />
      </>,
    );
    expect(screen.getByRole("button", { name: "repo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "main" })).toBeInTheDocument();
    expect(screen.getByTestId("workspace-bar")).toHaveClass(
      "items-center",
      "py-1.5",
      "h-[37px]",
      "gap-0.5",
      "md:gap-2",
    );
    expect(screen.getByTestId("workspace-bar")).not.toHaveClass("items-start", "pt-1.5");
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
      expect(trigger).toHaveClass(
        "min-w-10",
        "md:min-w-11",
        "px-0.5",
        "md:px-1",
        "max-w-[calc(50%-0.25rem)]",
      );
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
    expect(screen.getByTestId("composer-agent-model-value")).toHaveClass("truncate");
    expect(screen.getByTestId("composer-agent-model-value")).toHaveAttribute(
      "title",
      "GPT-5.6-Sol",
    );
    expect(screen.getByTestId("composer-agent-effort-value")).not.toHaveClass("hidden");
    expect(screen.getByTestId("product-icon")).toBeInTheDocument();
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
    for (const forbidden of ["hidden", "max-w-20"]) {
      expect(screen.getByText("Bypass permissions")).not.toHaveClass(forbidden);
    }
    expect(trigger).toHaveClass("w-auto", "gap-1", "px-2");
    fireEvent.keyDown(trigger, {
      key: "ArrowDown",
    });
    fireEvent.click(screen.getByRole("menuitem", { name: "Plan" }));
    expect(onSelect).toHaveBeenCalledWith("plan");
  });
});
