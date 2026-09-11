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
    expect(trigger).not.toHaveClass("max-w-[7.25rem]", "md:max-w-40");
    expect(trigger).toHaveTextContent("High");
    expect(screen.getByTestId("composer-agent-model-value")).not.toHaveClass("truncate");
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
    for (const forbidden of ["hidden", "max-w-20", "truncate"]) {
      expect(screen.getByText("Bypass permissions")).not.toHaveClass(forbidden);
    }
    expect(trigger).toHaveClass("w-auto", "gap-1", "px-2");
    fireEvent.keyDown(trigger, {
      key: "ArrowDown",
    });
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Plan" }));
    expect(onSelect).toHaveBeenCalledWith("plan");
  });

  it("marks the current mode's row as checked and leaves the rest unchecked", () => {
    render(
      <ComposerPermissionPicker
        label="Permissions"
        value="Auto"
        current="auto"
        options={[
          { value: "default", label: "Manual" },
          { value: "auto", label: "Auto" },
          { value: "plan", label: "Plan" },
        ]}
        onSelect={vi.fn()}
      />,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "Permissions: Auto" }), {
      key: "ArrowDown",
    });
    const currentRow = screen.getByTestId("composer-permission-option-auto");
    expect(currentRow).toHaveAttribute("aria-checked", "true");
    // The row carries the shared check-indicator slot, like the other
    // current-value menus.
    expect(
      currentRow.querySelector('[data-slot="dropdown-menu-radio-item-indicator"]'),
    ).toBeInTheDocument();
    for (const other of ["default", "plan"]) {
      expect(screen.getByTestId(`composer-permission-option-${other}`)).not.toHaveAttribute(
        "aria-checked",
        "true",
      );
    }
  });

  it("marks no row when the current mode is unknown", () => {
    render(
      <ComposerPermissionPicker
        label="Permissions"
        value="Permissions"
        options={[
          { value: "default", label: "Manual" },
          { value: "auto", label: "Auto" },
        ]}
        onSelect={vi.fn()}
      />,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "Permissions: Permissions" }), {
      key: "ArrowDown",
    });
    for (const mode of ["default", "auto"]) {
      expect(screen.getByTestId(`composer-permission-option-${mode}`)).not.toHaveAttribute(
        "aria-checked",
        "true",
      );
    }
  });
});
