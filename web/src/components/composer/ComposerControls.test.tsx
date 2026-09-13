import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ComposerHostTrigger,
  ComposerWorkspaceBar,
  ComposerWorkspaceTrigger,
  ComposerHarnessTrigger,
  ComposerPermissionPicker,
} from "./ComposerControls";
import {
  COMPOSER_COLLAPSED_LABEL_CLASS,
  COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS,
} from "./ChatComposer";

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
        label="Permission mode"
        value="Bypass permissions"
        options={[
          { value: "manual", label: "Manual" },
          { value: "plan", label: "Plan" },
        ]}
        onSelect={onSelect}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Permission mode: Bypass permissions" });
    for (const forbidden of ["hidden", "max-w-20"]) {
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

describe("workspace bar label collapse", () => {
  class StubResizeObserver {
    static callbacks: ResizeObserverCallback[] = [];
    constructor(callback: ResizeObserverCallback) {
      StubResizeObserver.callbacks.push(callback);
    }
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  const fireResize = () => {
    for (const callback of StubResizeObserver.callbacks) callback([], {} as ResizeObserver);
  };

  afterEach(() => {
    StubResizeObserver.callbacks = [];
    vi.unstubAllGlobals();
  });

  // jsdom does no layout, so stand in for it with fixed scroll/client widths.
  const defineWidth = (element: HTMLElement, clientWidth: number, scrollWidth: number) => {
    Object.defineProperty(element, "clientWidth", { configurable: true, get: () => clientWidth });
    Object.defineProperty(element, "scrollWidth", { configurable: true, get: () => scrollWidth });
  };

  it("marks each chip label to collapse to its icon", () => {
    render(
      <ComposerWorkspaceBar>
        <ComposerWorkspaceTrigger kind="directory" label="repo" />
      </ComposerWorkspaceBar>,
    );
    const label = screen.getByText("repo");
    expect(label).toHaveAttribute("data-workspace-collapse-label");
    expect(label).toHaveClass(COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS);
  });

  it("collapses the labels to icons once a chip can no longer show its full text", () => {
    vi.stubGlobal("ResizeObserver", StubResizeObserver);
    render(
      <ComposerWorkspaceBar data-testid="bar">
        <ComposerWorkspaceTrigger kind="worktree" label="feature/really-long-branch-name" />
      </ComposerWorkspaceBar>,
    );
    const bar = screen.getByTestId("bar");
    const label = bar.querySelector<HTMLElement>("[data-workspace-collapse-label]")!;
    defineWidth(bar, 200, 200);
    // The label's text is wider than the box it was given: it is truncating.
    defineWidth(label, 40, 120);
    fireResize();
    expect(bar).toHaveAttribute("data-labels", "collapsed");
    // Given room for the full text again, the labels come back.
    defineWidth(label, 120, 120);
    fireResize();
    expect(bar).not.toHaveAttribute("data-labels");
  });

  it("collapses when the row overflows even if no single label is truncating", () => {
    vi.stubGlobal("ResizeObserver", StubResizeObserver);
    render(
      <ComposerWorkspaceBar data-testid="bar">
        <ComposerWorkspaceTrigger kind="directory" label="repo" />
      </ComposerWorkspaceBar>,
    );
    const bar = screen.getByTestId("bar");
    const label = bar.querySelector<HTMLElement>("[data-workspace-collapse-label]")!;
    defineWidth(label, 40, 40);
    defineWidth(bar, 150, 300);
    fireResize();
    expect(bar).toHaveAttribute("data-labels", "collapsed");
  });
});
