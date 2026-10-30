import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComposerPrLink } from "./ComposerPrLink";
import { COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS } from "./ChatComposer";
import type { ComposerPullRequest } from "@/hooks/useComposerGitStatus";
import { useIsCoarsePointer } from "@/hooks/useIsCoarsePointer";

vi.mock("@/hooks/useIsCoarsePointer", () => ({ useIsCoarsePointer: vi.fn(() => false) }));

afterEach(() => {
  cleanup();
  vi.mocked(useIsCoarsePointer).mockReturnValue(false);
});

describe("ComposerPrLink", () => {
  it.each(["ready", "loading", "unknown"] as const)(
    "renders nothing when there are no PRs and the lookup is %s",
    (state) => {
      const { container } = render(
        <ComposerPrLink state={state} prCount={0} prNumber={null} onOpen={() => {}} />,
      );
      expect(container).toBeEmptyDOMElement();
    },
  );

  it.each(["ready", "loading", "unknown"] as const)(
    "renders nothing when there is no way to open the tab and the lookup is %s",
    (state) => {
      const { container } = render(
        <ComposerPrLink state={state} prCount={1} prNumber={42} onOpen={null} />,
      );
      expect(container).toBeEmptyDOMElement();
    },
  );

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
    expect(link).toHaveAttribute("title", "View this PR in the Pull Requests tab");
    expect(link).toHaveAccessibleName("#42");
    // A trigger for the bar's collapse, but never hidden by it.
    expect(screen.getByText("#42")).toHaveAttribute("data-workspace-collapse-label");
    expect(screen.getByText("#42")).not.toHaveClass(COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS);
    fireEvent.click(link);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("marks the PR number with its provider's prefix", () => {
    render(
      <ComposerPrLink
        state="ready"
        prCount={1}
        prNumber={7}
        prNumberPrefix="!"
        onOpen={() => {}}
      />,
    );
    const link = screen.getByTestId("composer-pr-link");
    expect(link).toHaveTextContent("!7");
    expect(link).toHaveAccessibleName("!7");
    expect(screen.getByText("!7")).toHaveAttribute("title", "!7");
    expect(screen.queryByText("#7")).toBeNull();
  });

  it("leaves a count and the missing-number label without a prefix", () => {
    const { rerender } = render(
      <ComposerPrLink
        state="ready"
        prCount={2}
        prNumber={7}
        prNumberPrefix="!"
        onOpen={() => {}}
      />,
    );
    expect(screen.getByTestId("composer-pr-link")).toHaveTextContent(/^2 PRs$/);
    rerender(
      <ComposerPrLink
        state="ready"
        prCount={1}
        prNumber={null}
        prNumberPrefix="!"
        onOpen={() => {}}
      />,
    );
    expect(screen.getByTestId("composer-pr-link")).toHaveTextContent(/^1 PR$/);
  });

  it("summarizes multiple PRs as a count", () => {
    render(<ComposerPrLink state="ready" prCount={3} prNumber={42} onOpen={() => {}} />);
    const link = screen.getByTestId("composer-pr-link");
    expect(link).toHaveTextContent("3 PRs");
    expect(link).toHaveClass("text-sm", "gap-1");
    expect(link).toHaveAttribute("title", "View these PRs in the Pull Requests tab");
    expect(screen.getByText("3 PRs")).toHaveAttribute("title", "3 PRs");
  });

  it("keeps loading and unavailable status for a known PR", () => {
    const { rerender } = render(
      <ComposerPrLink state="loading" prCount={1} prNumber={42} onOpen={() => {}} />,
    );
    expect(screen.getByTestId("composer-pr-loading")).toHaveTextContent("Checking PR…");
    rerender(<ComposerPrLink state="unknown" prCount={1} prNumber={42} onOpen={() => {}} />);
    expect(screen.getByTestId("composer-pr-unknown")).toHaveTextContent("PR unavailable");
    expect(screen.queryByTestId("composer-pr-link")).toBeNull();
  });

  it("falls back to a safe singular label when the association number is missing", () => {
    render(<ComposerPrLink state="ready" prCount={1} prNumber={null} onOpen={() => {}} />);
    expect(screen.getByTestId("composer-pr-link")).toHaveTextContent("1 PR");
    expect(screen.queryByText("#null")).toBeNull();
  });

  it.each([false, true])("shows each PR state on keyboard focus (coarse=%s)", async (coarse) => {
    vi.mocked(useIsCoarsePointer).mockReturnValue(coarse);
    const prs: ComposerPullRequest[] = [
      {
        url: "https://github.com/example/app/pull/42",
        host: "github.com",
        repository: "example/app",
        number: 42,
        relationship: "created",
        title: "Resolve conflicting changes",
        state: "OPEN",
        merge_state_status: "DIRTY",
      },
      {
        url: "https://github.com/example/app/pull/41",
        host: "github.com",
        repository: "example/app",
        number: 41,
        relationship: "created",
        title: "Previously merged work",
        state: "MERGED",
      },
    ];
    const onOpen = vi.fn();
    render(
      <ComposerPrLink
        state="ready"
        prCount={2}
        prNumber={42}
        prStatus="conflict"
        prs={prs}
        onOpen={onOpen}
      />,
    );
    const link = screen.getByTestId("composer-pr-link");
    expect(link).toHaveAttribute("data-pr-status", "conflict");
    expect(link).toHaveAccessibleName("2 PRs");
    expect(link).not.toHaveAttribute("aria-describedby");
    fireEvent.focus(link);
    const preview = await screen.findByTestId("composer-pr-preview");
    expect(link).toHaveAttribute("aria-describedby", preview.id);
    expect(link).toHaveAccessibleDescription(/Merge conflicts.*Merged.*Previously merged work/);
    expect(preview.querySelectorAll("li[data-pr-status]")).toHaveLength(2);
    expect(preview.querySelector("li")).toHaveAttribute("data-pr-status", "conflict");
    expect(screen.getByText("Merged")).toBeVisible();
    expect(screen.getByText("Previously merged work")).toBeVisible();
    expect(preview.querySelector(".lucide-git-merge")).toBeInTheDocument();
    fireEvent.keyDown(link, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("composer-pr-preview")).toBeNull());
    fireEvent.click(link);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});
