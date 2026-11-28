import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { PendingFocus } from "@/hooks/usePendingFocus";
import type { PullRequestCommit, PullRequestCommits } from "@/lib/pullRequestCommits";
import { PullRequestCommitsTab } from "@/shell/PullRequestCommits";
import type { CommitFocusTarget } from "@/shell/pullRequestCommits/useCommitCommands";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import {
  commitHistoryHeading,
  committedTime,
  COMMIT_HISTORY_LIMIT,
  matchesCommitSearch,
} from "./pullRequestCommitHistoryModel";
import { COPIED_MS, useCopyFeedback } from "./useCopyFeedback";
import { longRelativeTime } from "@/lib/relativeTime";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

const clipboard = vi.hoisted(() => ({ copyText: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyText: clipboard.copyText }));

const PR_URL = "https://1.host.example.com/acme/app/pull/7";
const commit = (digit: string, subject: string, over: Partial<PullRequestCommit> = {}) => ({
  sha: digit.repeat(40),
  subject,
  author_login: "harry-yao",
  author_name: "Harry Yao",
  authored_at: new Date(Date.now() - 4 * 86_400_000).toISOString(),
  committed_at: new Date(Date.now() - 4 * 86_400_000).toISOString(),
  url: `${PR_URL}/commits/${digit.repeat(40)}`,
  ci_state: "SUCCESS",
  ...over,
});
const COMMITS: PullRequestCommit[] = [
  commit("1", "fix(host): keep workspace filesystem calls off the tunnel loop"),
  commit("2", "fix(runner): forward git's CA paths", {
    author_login: "jsonmp-k8",
    ci_state: "FAILURE",
  }),
  commit("3", "docs: list every builtin harness", { ci_state: null }),
];
const SOURCE = {
  account: { host: "1.host.example.com", login: "dev" },
  repository: "acme/app",
  number: 7,
  headSha: "f".repeat(40),
  baseSha: "e".repeat(40),
};

function listOf(commits: PullRequestCommit[], over: Partial<PullRequestCommits> = {}) {
  return {
    commits,
    total_count: commits.length,
    head_sha: SOURCE.headSha,
    base_sha: SOURCE.baseSha,
    complete: true,
    ...over,
  };
}

function renderHistory({
  list = listOf(COMMITS),
  timeline = true,
  active = true,
  focusRequest = null,
}: {
  list?: PullRequestCommits;
  timeline?: boolean;
  active?: boolean;
  focusRequest?: PendingFocus<CommitFocusTarget> | null;
} = {}) {
  const onSelectCommit = vi.fn();
  const onOpenCommitInNewTab = vi.fn();
  const client = new QueryClient();
  const tree = (props: { timeline: boolean; active: boolean }) => (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestCommitsTab
          status={{ kind: "ready" }}
          onRetry={vi.fn()}
          revision={{ headSha: SOURCE.headSha, baseSha: SOURCE.baseSha }}
          source={SOURCE}
          commits={{ data: list } as never}
          selectedSha={undefined}
          onSelectCommit={onSelectCommit}
          onOpenCommitInNewTab={onOpenCommitInNewTab}
          commands={{}}
          focusRequest={focusRequest}
          onFocusRequestSettled={vi.fn()}
          provider="named-host"
          prUrl={PR_URL}
          renderFiles={() => null}
          timeline={props.timeline}
          active={props.active}
        />
      </TooltipProvider>
    </QueryClientProvider>
  );
  const view = render(tree({ timeline, active }));
  return {
    ...view,
    onSelectCommit,
    onOpenCommitInNewTab,
    rerenderWith: (props: { timeline: boolean; active: boolean }) => view.rerender(tree(props)),
  };
}

const cards = () => within(screen.getByRole("list", { name: "Commits" })).getAllByRole("listitem");
const card = (subject: RegExp) => cards().find((item) => subject.test(item.textContent ?? ""))!;
const paletteLabels = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) =>
    group.commands.map((command) => command.label),
  );

beforeEach(() => {
  clipboard.copyText.mockReset();
  usePaletteCommandsStore.setState({ groups: {} });
});
afterEach(() => vi.useRealTimers());

describe("commitHistoryHeading", () => {
  it("counts a complete history", () => {
    expect(commitHistoryHeading(listOf(COMMITS))).toEqual({ title: "3 commits" });
    expect(commitHistoryHeading(listOf(COMMITS.slice(0, 1)))).toEqual({ title: "1 commit" });
  });

  it("names the limit only when the read reached it", () => {
    const many = Array.from({ length: COMMIT_HISTORY_LIMIT }, (_, index) =>
      commit("a", `Commit ${index}`, { sha: index.toString(16).padStart(40, "0") }),
    );
    expect(commitHistoryHeading(listOf(many, { total_count: 400, complete: false }))).toEqual({
      title: "250 commits shown",
      note: "History is limited to 250 commits",
    });
  });

  it("notes a shorter list only when the read says it stopped short", () => {
    expect(commitHistoryHeading(listOf(COMMITS, { total_count: 5, complete: false }))).toEqual({
      title: "3 commits shown",
      note: "Showing 3 of 5 commits",
    });
    // A complete read, or an older app's that doesn't say, gets no note, whatever the total.
    expect(commitHistoryHeading(listOf(COMMITS, { total_count: 9, complete: true }))).toEqual({
      title: "3 commits",
    });
    expect(commitHistoryHeading(listOf(COMMITS, { total_count: 9, complete: undefined }))).toEqual({
      title: "3 commits",
    });
  });
});

describe("committedTime", () => {
  it("names when a commit was committed, falling back to when it was authored", () => {
    expect(
      committedTime(
        commit("4", "Rebased", {
          authored_at: "2025-11-07T09:00:00Z",
          committed_at: "2026-11-07T09:00:00Z",
        }),
      ),
    ).toBe(Date.parse("2026-11-07T09:00:00Z"));
    expect(
      committedTime(
        commit("5", "Old", { authored_at: "2025-11-07T09:00:00Z", committed_at: null }),
      ),
    ).toBe(Date.parse("2025-11-07T09:00:00Z"));
    expect(
      committedTime(commit("6", "None", { authored_at: null, committed_at: null })),
    ).toBeNull();
  });

  it("dates a rebased commit's card by when it was committed", () => {
    renderHistory({
      list: listOf([
        commit("7", "Rebased onto main", {
          authored_at: "2025-11-07T09:00:00Z",
          committed_at: new Date(Date.now() - 4 * 86_400_000).toISOString(),
        }),
      ]),
    });
    expect(cards()[0]).toHaveTextContent(
      new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(-4, "day"),
    );
  });
});

describe("matchesCommitSearch and longRelativeTime", () => {
  it("finds a commit by subject, author or SHA, needing every word", () => {
    const [first] = COMMITS;
    expect(matchesCommitSearch(first!, "tunnel harry")).toBe(true);
    expect(matchesCommitSearch(first!, "1111111")).toBe(true);
    expect(matchesCommitSearch(first!, "tunnel jsonmp")).toBe(false);
  });

  it("reads times as GitHub's long relative ages", () => {
    const now = Date.UTC(2026, 10, 7, 12);
    expect(longRelativeTime(now - 4 * 86_400_000, now)).toBe(
      new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(-4, "day"),
    );
    expect(longRelativeTime(now - 2 * 3_600_000, now)).toBe(
      new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(-2, "hour"),
    );
    expect(longRelativeTime(now - 20_000, now)).toBe(
      new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(-20, "second"),
    );
  });
});

describe("the commit history", () => {
  it("shows the history header and a card per commit on the timeline", () => {
    renderHistory();
    expect(screen.getByText("History")).toBeVisible();
    expect(screen.getByRole("heading", { level: 2, name: "3 commits" })).toBeVisible();
    expect(cards()).toHaveLength(3);
    const first = card(/tunnel loop/);
    expect(within(first).getByText("HY")).toBeVisible();
    expect(first).toHaveTextContent(/harry-yao committed .*ago/);
    expect(within(first).getByRole("img", { name: "CI: Checks passed" })).toBeVisible();
    expect(within(first).getByText("1111111")).toBeVisible();
    expect(within(card(/CA paths/)).getByRole("img", { name: "CI: Checks failed" })).toBeVisible();
    expect(screen.queryByText(/History is limited/)).toBeNull();
  });

  it("names the limit and links the rest when the history is cut short", () => {
    renderHistory({ list: listOf(COMMITS, { total_count: 5, complete: false }) });
    expect(screen.getByRole("heading", { level: 2, name: "3 commits shown" })).toBeVisible();
    expect(screen.getByText(/Showing 3 of 5 commits/)).toBeVisible();
    expect(screen.getByRole("link", { name: "See all on Example Host" })).toHaveAttribute(
      "href",
      `${PR_URL}/commits`,
    );
  });

  it("opens a commit from its card, SHA or chevron, and in a new tab with ⌘-click", async () => {
    const user = userEvent.setup();
    const view = renderHistory();
    const first = card(/tunnel loop/);
    await user.click(within(first).getByRole("button", { name: /tunnel loop/ }));
    expect(view.onSelectCommit).toHaveBeenLastCalledWith(COMMITS[0]!.sha);
    fireEvent.click(within(first).getByText("1111111"));
    fireEvent.click(first.querySelector(".commit-row-open")!);
    expect(view.onSelectCommit).toHaveBeenCalledTimes(3);
    fireEvent.click(within(first).getByRole("button", { name: /tunnel loop/ }), {
      metaKey: true,
      ctrlKey: true,
    });
    expect(view.onOpenCommitInNewTab).toHaveBeenCalledWith(COMMITS[0]!.sha, { background: true });
  });

  it("moves between cards with the arrow keys, as one tab stop", async () => {
    const user = userEvent.setup();
    renderHistory();
    const buttons = () => cards().map((item) => within(item).getAllByRole("button")[0]!);
    expect(buttons().map((button) => button.tabIndex)).toEqual([0, -1, -1]);
    buttons()[0]!.focus();
    await user.keyboard("{ArrowDown}");
    expect(buttons()[1]).toHaveFocus();
  });

  it("takes focus when a command asks for the list", () => {
    renderHistory({ focusRequest: { target: "list", id: 1 } as never });
    expect(within(cards()[0]!).getAllByRole("button")[0]).toHaveFocus();
  });

  it("finds commits, counting matches, and Escape closes the search and returns focus", async () => {
    const user = userEvent.setup();
    renderHistory();
    const toggle = screen.getByRole("button", { name: "Find commits" });
    await user.click(toggle);
    const search = screen.getByRole("searchbox", { name: "Find commits" });
    await waitFor(() => expect(search).toHaveFocus());
    await user.keyboard("runner");
    expect(cards()).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("1 of 3");
    await user.clear(search);
    await user.keyboard("nothing like it");
    expect(screen.getByText("No Matching Commits")).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("searchbox", { name: "Find commits" })).toBeNull();
    expect(toggle).toHaveFocus();
    expect(cards()).toHaveLength(3);
  });

  it("copies a SHA and confirms it in place, or says it couldn't", async () => {
    clipboard.copyText.mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error("denied"));
    renderHistory();
    const copyButton = () =>
      within(card(/tunnel loop/)).getByRole("button", { name: /commit SHA/i });
    expect(copyButton()).toHaveAttribute("tabindex", "-1");
    await act(async () => fireEvent.click(copyButton()));
    expect(clipboard.copyText).toHaveBeenCalledWith(COMMITS[0]!.sha);
    expect(copyButton()).toHaveAccessibleName("Commit SHA copied");
    await act(async () => fireEvent.click(copyButton()));
    expect(copyButton()).toHaveAccessibleName("Couldn’t copy the commit SHA. Try again.");
  });

  it("offers finding and copying in ⌘K while shown", async () => {
    const view = renderHistory();
    expect(paletteLabels()).toEqual(
      expect.arrayContaining(["Find commits", "Copy a commit's SHA"]),
    );
    clipboard.copyText.mockResolvedValue(undefined);
    const copy = Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .find((command) => command.label === "Copy a commit's SHA")!;
    await act(async () => {
      await copy.setting!.choose(COMMITS[1]!.sha);
    });
    expect(clipboard.copyText).toHaveBeenCalledWith(COMMITS[1]!.sha);
    view.rerenderWith({ timeline: true, active: false });
    expect(paletteLabels()).not.toContain("Find commits");
  });

  it("opens a card's description from the … beside its title, as the host's list does", async () => {
    const described = [
      commit("1", "fix(host): keep workspace filesystem calls off the tunnel loop", {
        body: "The loop stalled under load.\n\n- moves the calls to a thread",
      }),
      COMMITS[1]!,
    ];
    const view = renderHistory({ list: listOf(described) });
    const first = card(/tunnel loop/);
    expect(within(first).queryByTestId("commit-row-body")).toBeNull();
    // Only a commit with a description has the toggle.
    expect(within(card(/CA paths/)).queryByRole("button", { name: "Show description" })).toBeNull();

    await userEvent.click(within(first).getByRole("button", { name: "Show description" }));
    const body = within(first).getByTestId("commit-row-body");
    expect(body.textContent).toBe("The loop stalled under load.\n\n- moves the calls to a thread");
    const toggle = within(first).getByRole("button", { name: "Hide description" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-controls", body.id);
    // Opening the description doesn't open the commit.
    expect(view.onSelectCommit).not.toHaveBeenCalled();

    await userEvent.click(toggle);
    expect(within(first).queryByTestId("commit-row-body")).toBeNull();
  });

  it("shows and hides every description from ⌘K", async () => {
    const described = [
      commit("1", "Add the parser", { body: "First body" }),
      commit("2", "Fix the parser", { body: "Second body" }),
      COMMITS[2]!,
    ];
    renderHistory({ list: listOf(described) });
    const run = (label: string) =>
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((command) => command.label === label)!.run!();
    act(() => run("Show commit descriptions"));
    expect(screen.getAllByTestId("commit-row-body").map((body) => body.textContent)).toEqual([
      "First body",
      "Second body",
    ]);
    expect(paletteLabels()).toContain("Hide commit descriptions");
    act(() => run("Hide commit descriptions"));
    expect(screen.queryAllByTestId("commit-row-body")).toEqual([]);
  });

  it("toggles one commit's description from ⌘K as its … does, leaving the others", async () => {
    const described = [
      commit("1", "Add the parser", { body: "First body" }),
      commit("2", "Fix the parser", { body: "Second body" }),
      COMMITS[2]!,
    ];
    renderHistory({ list: listOf(described) });
    const toggleOne = () =>
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((command) => command.label === "Show or hide a commit's description")!.setting!;
    const shown = () => screen.queryAllByTestId("commit-row-body").map((body) => body.textContent);
    // Only commits with a description are offered, each saying what picking it does.
    expect(toggleOne().choices.map((choice) => choice.label)).toEqual([
      `Show 1111111 Add the parser`,
      `Show 2222222 Fix the parser`,
    ]);

    await act(async () => {
      await toggleOne().choose(described[1]!.sha);
    });
    expect(shown()).toEqual(["Second body"]);
    expect(toggleOne().choices[1]!.label).toBe("Hide 2222222 Fix the parser");
    // The card's … closes the same description again.
    await userEvent.click(
      within(card(/Fix the parser/)).getByRole("button", { name: "Hide description" }),
    );
    expect(shown()).toEqual([]);
    await userEvent.click(
      within(card(/Fix the parser/)).getByRole("button", { name: "Show description" }),
    );
    await act(async () => {
      await toggleOne().choose(described[1]!.sha);
    });
    expect(shown()).toEqual([]);
  });

  it("shows only titles from an older desktop app, which sends no descriptions", () => {
    renderHistory();
    expect(screen.queryByRole("button", { name: "Show description" })).toBeNull();
    expect(paletteLabels()).not.toContain("Show commit descriptions");
    expect(paletteLabels()).not.toContain("Show or hide a commit's description");
    expect(card(/tunnel loop/).textContent).toContain(
      "fix(host): keep workspace filesystem calls off the tunnel loop",
    );
  });

  it("says when there are no commits", () => {
    renderHistory({ list: listOf([]) });
    expect(screen.getByText("No commits")).toBeVisible();
  });

  it("keeps the classic list without the timeline", () => {
    const view = renderHistory({ timeline: false });
    expect(screen.queryByText("History")).toBeNull();
    expect(screen.getByRole("list", { name: "Commits" })).toBeVisible();
    view.rerenderWith({ timeline: true, active: true });
    expect(screen.getByText("History")).toBeVisible();
  });
});

describe("useCopyFeedback", () => {
  it("confirms only the latest copy when earlier ones finish late", async () => {
    vi.useFakeTimers();
    const deferred = () => {
      let resolve!: () => void;
      const promise = new Promise<void>((done) => {
        resolve = done;
      });
      return { promise, resolve };
    };
    const first = deferred();
    const second = deferred();
    const { result, unmount } = renderHook(() => useCopyFeedback<string>());
    act(() => result.current.copy(() => first.promise, "a"));
    act(() => result.current.copy(() => second.promise, "b"));
    await act(async () => second.resolve());
    expect(result.current.stateOf("b")).toBe("copied");
    // The first copy finishing now doesn't take over the confirmation, or its timer.
    await act(async () => first.resolve());
    expect(result.current.stateOf("b")).toBe("copied");
    expect(result.current.stateOf("a")).toBe("idle");
    act(() => vi.advanceTimersByTime(COPIED_MS));
    expect(result.current.stateOf("b")).toBe("idle");
    unmount();
  });
});
