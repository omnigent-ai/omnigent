import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComposerPrLink } from "./ComposerPrLink";
import { COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS } from "./ChatComposer";

afterEach(cleanup);

describe("ComposerPrLink", () => {
  it("renders nothing when there are no PRs", () => {
    const { container } = render(
      <ComposerPrLink state="ready" prCount={0} prNumber={null} onOpen={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when there is no way to open the tab", () => {
    const { container } = render(
      <ComposerPrLink state="ready" prCount={1} prNumber={42} onOpen={null} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a single PR number and opens the tab on click", () => {
    const onOpen = vi.fn();
    render(<ComposerPrLink state="ready" prCount={1} prNumber={42} onOpen={onOpen} />);
    const link = screen.getByTestId("composer-pr-link");
    expect(link).toHaveTextContent("#42");
    expect(link).toHaveClass("text-sm", "gap-1", "min-w-0");
    expect(link).not.toHaveClass("shrink-0");
    expect(link.querySelector("svg")).toHaveClass("lucide-git-pull-request", "shrink-0");
    expect(screen.getByText("#42")).toHaveClass("truncate");
    expect(screen.getByText("#42")).not.toHaveClass("underline");
    expect(screen.getByText("#42")).toHaveClass("group-hover:underline");
    expect(screen.getByText("#42")).toHaveAttribute("title", "#42");
    expect(link).toHaveAttribute("title", "View this PR in the GitHub tab");
    expect(link).toHaveAccessibleName("#42");
    // A trigger for the bar's collapse, but never hidden by it.
    expect(screen.getByText("#42")).toHaveAttribute("data-workspace-collapse-label");
    expect(screen.getByText("#42")).not.toHaveClass(COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS);
    fireEvent.click(link);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("summarizes multiple PRs as a count", () => {
    render(<ComposerPrLink state="ready" prCount={3} prNumber={42} onOpen={() => {}} />);
    const link = screen.getByTestId("composer-pr-link");
    expect(link).toHaveTextContent("3 PRs");
    expect(link).toHaveClass("text-sm", "gap-1");
    expect(link).toHaveAttribute("title", "View these PRs in the GitHub tab");
    expect(screen.getByText("3 PRs")).toHaveAttribute("title", "3 PRs");
  });

  it("renders explicit loading and unknown states without a null PR number", () => {
    const { rerender } = render(
      <ComposerPrLink state="loading" prCount={0} prNumber={null} onOpen={() => {}} />,
    );
    expect(screen.getByTestId("composer-pr-loading")).toHaveTextContent("Checking PR…");
    rerender(<ComposerPrLink state="unknown" prCount={0} prNumber={null} onOpen={() => {}} />);
    expect(screen.getByTestId("composer-pr-unknown")).toHaveTextContent("PR unavailable");
    expect(screen.queryByText("#null")).toBeNull();
  });

  it("falls back to a safe singular label when the association number is missing", () => {
    render(<ComposerPrLink state="ready" prCount={1} prNumber={null} onOpen={() => {}} />);
    expect(screen.getByTestId("composer-pr-link")).toHaveTextContent("1 PR");
    expect(screen.queryByText("#null")).toBeNull();
  });
});
