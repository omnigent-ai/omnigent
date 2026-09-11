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

  it("lets the permission label use free row space and only truncate under flex pressure", () => {
    render(
      <ComposerPermissionPicker
        label="Permissions"
        value="Bypass permissions"
        options={[{ value: "bypassPermissions", label: "Bypass permissions" }]}
        onSelect={() => {}}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Permissions: Bypass permissions" });
    // The pill must yield to flex pressure instead of being exempt from it.
    expect(trigger).toHaveClass("min-w-0");
    expect(trigger).not.toHaveClass("shrink-0");
    const label = screen.getByText("Bypass permissions");
    // No fixed cap: with free row space the full mode name renders; the
    // ellipsis appears only when the row genuinely runs out of width.
    expect(label).toHaveClass("min-w-0", "truncate");
    expect(label.className).not.toMatch(/max-w-/);
  });

  it("lets the model label use free row space and shrink under real width pressure", () => {
    render(
      <ComposerHarnessTrigger
        label="Configure session"
        model="Fable 5.1 (1M context)"
        effort="xHigh"
      />,
    );
    const trigger = screen.getByRole("button", { name: "Configure session" });
    // `shrink` must win over the Button base's `shrink-0`, and no fixed
    // max-width cap may clip the label while the row still has free space.
    expect(trigger).toHaveClass("shrink", "min-w-0");
    expect(trigger).not.toHaveClass("shrink-0");
    expect(trigger.className).not.toMatch(/max-w-/);
    const model = screen.getByText("Fable 5.1 (1M context)");
    expect(model).toHaveClass("min-w-0", "truncate");
    // The effort tag never absorbs the shrink; the model label truncates.
    expect(screen.getByText("xHigh")).toHaveClass("shrink-0");
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
