import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComposerPrLink } from "./ComposerPrLink";
import { COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS } from "./ChatComposer";
import type { ComposerPullRequest } from "@/hooks/useComposerGitStatus";
import { useIsCoarsePointer } from "@/hooks/useIsCoarsePointer";
import type * as OpenLinkInAppModule from "@/lib/openLinkInApp";
import { followLinkWithPopupFallback } from "@/lib/openLinkInApp";

vi.mock("@/hooks/useIsCoarsePointer", () => ({ useIsCoarsePointer: vi.fn(() => false) }));
vi.mock("@/lib/openLinkInApp", async (importOriginal) => ({
  ...(await importOriginal<typeof OpenLinkInAppModule>()),
  followLinkWithPopupFallback: vi.fn(),
}));

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
    const description = document.getElementById(link.getAttribute("aria-describedby") ?? "");
    expect(description).toHaveAttribute("role", "tooltip");
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

  it("opens previewed PRs in the sidebar and keeps the new-tab links separate", async () => {
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
        url: "https://gitlab.com/example/app/-/merge_requests/7",
        host: "gitlab.com",
        provider: "gitlab",
        provider_display: {
          id: "gitlab",
          display_name: "GitLab",
          request_name: "merge request",
          number_prefix: "!",
        },
        repository: "example/app",
        number: 7,
        relationship: "created",
        title: "",
        state: "MERGED",
      },
      {
        url: "javascript:alert(1)",
        host: "github.com",
        repository: "example/app",
        number: 40,
        relationship: "created",
        title: "Untrusted link",
        state: "OPEN",
      },
    ];
    const onOpen = vi.fn();
    render(
      <>
        <ComposerPrLink
          state="ready"
          prCount={3}
          prNumber={42}
          prStatus="conflict"
          prs={prs}
          onOpen={onOpen}
          conversationId="conv_1"
        />
        <button type="button">Next composer control</button>
      </>,
    );
    const trigger = screen.getByTestId("composer-pr-link");
    fireEvent.focus(trigger);
    const preview = await screen.findByTestId("composer-pr-preview");
    const rows = preview.querySelectorAll("li");

    const titled = within(rows[0]).getByRole("link", { name: "Open example/app #42 in a new tab" });
    expect(titled).toHaveAttribute("href", "https://github.com/example/app/pull/42");
    expect(titled).toHaveAttribute("target", "_blank");
    expect(titled).toHaveAttribute("rel", "noopener noreferrer");
    expect(titled).toHaveAttribute("title", "Open in new tab");
    expect(titled.parentElement).toHaveTextContent("Resolve conflicting changes");

    // Without a title the link follows the PR's identity instead.
    const untitled = within(rows[1]).getByRole("link", {
      name: "Open example/app !7 in a new tab",
    });
    expect(untitled).toHaveAttribute("href", "https://gitlab.com/example/app/-/merge_requests/7");
    expect(untitled.parentElement).toHaveTextContent("example/app !7");

    expect(within(rows[2]).queryByRole("link")).toBeNull();
    expect(screen.getAllByTestId("composer-pr-preview-open")).toHaveLength(2);
    // The screen-reader description stays text: one link per PR, no hidden duplicates.
    expect(trigger).toHaveAccessibleDescription(
      "Pull requests: example/app #42, Merge conflicts, Resolve conflicting changes. " +
        "example/app !7, Merged. example/app #40, Open, Untrusted link",
    );

    // Clicks share chat's external-link handling for host panes and the in-app browser.
    fireEvent.click(titled);
    expect(followLinkWithPopupFallback).toHaveBeenCalledWith(
      expect.anything(),
      "https://github.com/example/app/pull/42",
      "conv_1",
    );
    expect(onOpen).not.toHaveBeenCalled();
    expect(screen.getByTestId("composer-pr-preview")).toBeVisible();

    const user = userEvent.setup();
    await user.tab();
    const firstRow = screen.getByRole("button", {
      name: "View example/app #42 in the Pull Requests tab",
    });
    await user.tab();
    expect(firstRow).toHaveFocus();
    await user.tab({ shift: true });
    expect(trigger).toHaveFocus();
    await user.tab();
    await user.tab();
    expect(titled).toHaveFocus();
    await user.tab();
    expect(within(rows[1]).getByRole("button")).toHaveFocus();
    await user.tab();
    expect(untitled).toHaveFocus();
    await user.tab();
    expect(within(rows[2]).getByRole("button")).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Next composer control" })).toHaveFocus();
    await user.tab({ shift: true });
    await screen.findByTestId("composer-pr-preview");
    await user.tab();
    await user.keyboard("{Enter}");
    expect(onOpen).toHaveBeenLastCalledWith(prs[0].url);
    await waitFor(() => expect(screen.queryByTestId("composer-pr-preview")).toBeNull());
    expect(trigger).toHaveFocus();

    fireEvent.focus(trigger);
    await screen.findByTestId("composer-pr-preview");
    const secondRow = screen.getByRole("button", {
      name: "View example/app !7 in the Pull Requests tab",
    });
    // Safari can blur the focused control before clicking without focusing the target.
    if (fireEvent.mouseDown(secondRow)) fireEvent.blur(trigger, { relatedTarget: null });
    fireEvent.click(secondRow);
    expect(onOpen).toHaveBeenLastCalledWith(prs[1].url);
    expect(onOpen).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.queryByTestId("composer-pr-preview")).toBeNull());

    fireEvent.focus(trigger);
    await screen.findByTestId("composer-pr-preview");
    await user.tab();
    fireEvent.scroll(document.body);
    await waitFor(() => expect(screen.queryByTestId("composer-pr-preview")).toBeNull());
    expect(trigger).toHaveFocus();
  });
});
