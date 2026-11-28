// A pull request's own header actions: the Pull Request Actions menu with Refresh Pull Request,
// its links and its removals, each run as its shared command says, with a failed refresh said
// under it.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import {
  AVAILABLE,
  type CommandAvailability,
  isCommandListed,
  unavailableBecause,
} from "@/store/paletteCommands";

import { pullRequestCommands, REFRESHING_PULL_REQUEST_REASON } from "./pullRequestCommands/model";
import { PullRequestHeaderActions, PullRequestRefreshNotice } from "./PullRequestHeaderActions";

const TARGET = { url: "https://github.com/acme/app/pull/7", number: 7, title: "Fix it" };
type Facts = Parameters<typeof pullRequestCommands>[1];

function renderActions({
  refresh = AVAILABLE,
  refreshing = false,
  removal = "available",
  finished = AVAILABLE,
}: {
  refresh?: Facts["refresh"];
  refreshing?: boolean;
  removal?: Facts["removal"];
  finished?: CommandAvailability;
} = {}) {
  const runs = new Map<string, ReturnType<typeof vi.fn>>();
  const commands = pullRequestCommands(TARGET, {
    openable: false,
    reviewSite: null,
    removal,
    refresh,
  })
    .filter((command) => isCommandListed(command.availability))
    .map((command) => {
      const run = vi.fn();
      runs.set(command.id, run);
      return { command, run };
    });
  const removeFinished = vi.fn();
  render(
    <TooltipProvider>
      <PullRequestHeaderActions
        commands={commands}
        refreshing={refreshing}
        removeFinished={{
          menuLabel: "Remove Closed Pull Requests",
          availability: finished,
          run: removeFinished,
        }}
      />
    </TooltipProvider>,
  );
  return { runs, removeFinished };
}

const openMenu = () => {
  fireEvent.keyDown(screen.getByRole("button", { name: "Pull Request Actions" }), { key: "Enter" });
  return screen.getByRole("menu");
};
const itemNames = (menu: HTMLElement) =>
  within(menu)
    .getAllByRole("menuitem")
    .map((item) => item.textContent);

describe("Pull Request Actions", () => {
  it("leads with Refresh Pull Request, then its links, then its removals", () => {
    renderActions();
    // The header has no refresh button of its own: the menu holds it.
    expect(screen.queryByRole("button", { name: "Refresh Pull Request" })).toBeNull();
    const menu = openMenu();
    const names = itemNames(menu);
    expect(names[0]).toBe("Refresh Pull Request");
    expect(names).toContain("Open on GitHub");
    expect(names).toContain("Copy GitHub link");
    expect(names.slice(-2)).toEqual(["Remove from this session", "Remove Closed Pull Requests"]);
    expect(within(menu).getAllByRole("separator").length).toBeGreaterThanOrEqual(3);
  });

  it("says nothing and shows the menu glyph while no refresh runs", () => {
    renderActions();
    expect(screen.queryByRole("status")).toBeNull();
    const trigger = screen.getByRole("button", { name: "Pull Request Actions" });
    expect(trigger).not.toHaveAttribute("aria-busy");
    expect(trigger.querySelector("svg")).not.toHaveClass("animate-spin");
  });

  it("refreshes this pull request from the menu", () => {
    const { runs } = renderActions();
    fireEvent.click(within(openMenu()).getByRole("menuitem", { name: "Refresh Pull Request" }));
    expect(runs.get("refresh")).toHaveBeenCalledTimes(1);
  });

  it("says a refresh is running beside the menu, which waits to refresh again", () => {
    renderActions({
      refreshing: true,
      refresh: unavailableBecause(REFRESHING_PULL_REQUEST_REASON),
    });
    expect(screen.getByRole("status")).toHaveTextContent("Refreshing…");
    const trigger = screen.getByRole("button", { name: "Pull Request Actions" });
    expect(trigger).toHaveAttribute("aria-busy", "true");
    // The spinner takes the menu glyph's place, so the header's actions keep their width.
    expect(trigger.querySelectorAll("svg")).toHaveLength(1);
    expect(trigger.querySelector("svg")).toHaveClass("animate-spin");
    expect(screen.getByRole("status")).toHaveClass("sr-only");
    expect(
      within(openMenu()).getByRole("menuitem", { name: "Refresh Pull Request" }),
    ).toHaveAttribute("aria-disabled", "true");
  });

  it("removes this pull request, or the session's merged and closed ones", async () => {
    const { runs, removeFinished } = renderActions();
    fireEvent.click(within(openMenu()).getByRole("menuitem", { name: "Remove from this session" }));
    expect(runs.get("remove")).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    fireEvent.click(
      within(openMenu()).getByRole("menuitem", { name: "Remove Closed Pull Requests" }),
    );
    expect(removeFinished).toHaveBeenCalledTimes(1);
  });

  it("stays usable where neither removal can run: its refresh and links still can", () => {
    renderActions({
      removal: "hidden",
      finished: unavailableBecause("No merged or closed pull requests to remove."),
    });
    const menu = openMenu();
    expect(
      within(menu).getByRole("menuitem", { name: "Refresh Pull Request" }),
    ).not.toHaveAttribute("aria-disabled");
    expect(
      within(menu).getByRole("menuitem", { name: "Remove Closed Pull Requests" }),
    ).toHaveAttribute("aria-disabled", "true");
  });

  it("is disabled only when nothing in it can run", () => {
    render(
      <TooltipProvider>
        <PullRequestHeaderActions commands={[]} refreshing={false} removeFinished={null} />
      </TooltipProvider>,
    );
    expect(screen.getByRole("button", { name: "Pull Request Actions" })).toBeDisabled();
  });
});

describe("a refresh that didn't go through", () => {
  it("says why under the header, with Try Again", () => {
    const tryAgain = vi.fn();
    const { rerender } = render(
      <PullRequestRefreshNotice
        failure={{ kind: "failed", message: "GitHub didn't answer." }}
        refreshing={false}
        onTryAgain={tryAgain}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Couldn’t refresh this pull request: GitHub didn't answer.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Try Again" }));
    expect(tryAgain).toHaveBeenCalledTimes(1);
    rerender(
      <PullRequestRefreshNotice failure={{ kind: "timed-out" }} refreshing onTryAgain={tryAgain} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Refreshing this pull request is taking longer than it should.",
    );
    expect(screen.getByRole("button", { name: "Try Again" })).toBeDisabled();
    rerender(<PullRequestRefreshNotice failure={null} refreshing={false} onTryAgain={tryAgain} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
