// The Commits tab on the new pull request page: its list states and the open commit's header
// and states. The classic tab's behavior is in PullRequestCommits.test.
import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { PendingFocus } from "@/hooks/usePendingFocus";
import type * as UsePullRequestCommitsModule from "@/hooks/usePullRequestCommits";
import type { CommitsTabStatus, PullRequestCommit } from "@/lib/pullRequestCommits";

import { PullRequestCommitsTab } from "./PullRequestCommits";
import { commitCommands } from "./pullRequestCommits/commitCommands";
import type {
  CommitFocusTarget,
  RunnableCommitCommand,
} from "./pullRequestCommits/useCommitCommands";

const diff = vi.hoisted(() => ({
  current: {
    isPending: false,
    error: null as Error | null,
    data: { sha: "x", patch: "PATCH" } as { sha: string; patch: string } | undefined,
  },
  parse: vi.fn(() => [{ files: [{ name: "src/parser.ts", type: "change", hunks: [] }] }]),
}));

vi.mock("@/hooks/usePullRequestCommits", async (importOriginal) => ({
  ...(await importOriginal<typeof UsePullRequestCommitsModule>()),
  useCommitDiff: () => diff.current,
}));
vi.mock("@pierre/diffs", () => ({ parsePatchFiles: diff.parse }));

const PR_URL = "https://github.com/acme/app/pull/7";
const commit = (digit: string, subject: string): PullRequestCommit => ({
  sha: digit.repeat(40),
  subject,
  author_login: "dev",
  author_name: "Dev",
  authored_at: "2026-11-03T09:00:00Z",
  committed_at: "2026-11-03T09:00:00Z",
  url: `${PR_URL}/commits/${digit.repeat(40)}`,
  ci_state: "SUCCESS",
});
const COMMITS = [
  commit("1", "Add the parser"),
  commit("2", "Fix the parser and every caller that depended on its old behavior"),
  commit("3", "Test it"),
];
const SOURCE = {
  account: { host: "github.com", login: "dev" },
  repository: "acme/app",
  number: 7,
  headSha: "f".repeat(40),
  baseSha: "e".repeat(40),
};

/** Runnable commands for the open commit, as the panel builds them, each recording its run. */
function commandsFor(sha: string | undefined, listed: readonly PullRequestCommit[], ran: string[]) {
  const index = sha ? listed.findIndex((entry) => entry.sha === sha) : -1;
  const open = sha
    ? {
        sha,
        listed: index >= 0,
        previousSha: index > 0 ? listed[index - 1]!.sha : null,
        nextSha: index >= 0 ? (listed[index + 1]?.sha ?? null) : null,
        moreAfterList: false,
      }
    : null;
  return Object.fromEntries(
    commitCommands({ tabShown: true, onCommitsTab: true, open }).map((command) => [
      command.id,
      { command, run: () => ran.push(command.id) } satisfies RunnableCommitCommand,
    ]),
  );
}

function renderTab({
  status = { kind: "ready" } as CommitsTabStatus,
  selectedSha,
  listed = COMMITS,
  totalCount = listed.length,
  timeline = true,
  focusRequest = null,
}: {
  status?: CommitsTabStatus;
  selectedSha?: string;
  listed?: PullRequestCommit[];
  totalCount?: number;
  timeline?: boolean;
  focusRequest?: PendingFocus<CommitFocusTarget> | null;
} = {}) {
  const ran: string[] = [];
  const client = new QueryClient();
  const tree = (props: {
    timeline: boolean;
    request: PendingFocus<CommitFocusTarget> | null;
    sha?: string;
  }) => (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestCommitsTab
          status={status}
          onRetry={vi.fn()}
          revision={{ headSha: SOURCE.headSha, baseSha: SOURCE.baseSha }}
          source={SOURCE}
          commits={
            {
              data: {
                commits: listed,
                total_count: totalCount,
                head_sha: SOURCE.headSha,
                base_sha: SOURCE.baseSha,
                complete: totalCount === listed.length,
              },
            } as never
          }
          selectedSha={props.sha ?? selectedSha}
          onSelectCommit={vi.fn()}
          commands={commandsFor(props.sha ?? selectedSha, listed, ran)}
          focusRequest={props.request}
          onFocusRequestSettled={vi.fn()}
          provider="github"
          prUrl={PR_URL}
          renderFiles={(files) => <div data-testid="files">{files.map((file) => file.path)}</div>}
          timeline={props.timeline}
        />
      </TooltipProvider>
    </QueryClientProvider>
  );
  const view = render(tree({ timeline, request: focusRequest }));
  return {
    ...view,
    ran,
    rerenderWith: (props: {
      timeline: boolean;
      request?: PendingFocus<CommitFocusTarget> | null;
      sha?: string;
    }) => view.rerender(tree({ request: null, ...props })),
  };
}

const header = () => document.querySelector<HTMLElement>(".commit-detail-header")!;

describe("the list's states on the new page", () => {
  it.each([
    [{ kind: "loading" }, "Loading commits…"],
    [{ kind: "checking" }, "Checking access to this pull request…"],
  ] as const)("says what's loading in one line (%o)", (status, line) => {
    renderTab({ status: status as CommitsTabStatus });
    expect(screen.getByRole("status")).toHaveTextContent(line);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("titles a failed read and offers Try Again", () => {
    renderTab({
      status: { kind: "failed", message: "GitHub is unavailable.", error: new Error("x") } as never,
    });
    expect(screen.getByRole("alert")).toHaveTextContent("Commits Unavailable");
    expect(screen.getByRole("alert")).toHaveTextContent("GitHub is unavailable.");
    expect(screen.getByRole("button", { name: "Try Again" })).toBeVisible();
  });

  it("says a slow read is still loading, without an alert", () => {
    renderTab({ status: { kind: "slow" } as never });
    expect(screen.getByRole("status")).toHaveTextContent("Still Loading Commits");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Try Again" })).toBeVisible();
  });
});

describe("the open commit's header on the new page", () => {
  it("leads back to the list and shows the commit, its position and navigation", async () => {
    const user = userEvent.setup();
    const view = renderTab({ selectedSha: COMMITS[1]!.sha });
    const bar = header();
    const title = within(bar).getByRole("heading", { level: 3 });
    expect(title).toHaveTextContent(COMMITS[1]!.subject);
    // A long subject wraps in full, so it needs no hover to read.
    expect(title).not.toHaveAttribute("title");
    expect(bar).toHaveTextContent("dev · 2222222");
    expect(within(bar).getByText("2 of 3")).toBeVisible();
    await user.click(within(bar).getByRole("button", { name: "Commits" }));
    expect(view.ran).toEqual(["all-commits"]);
    // The header's steps are the same commands ⌘K runs.
    await user.click(within(bar).getByRole("button", { name: "Next commit" }));
    expect(view.ran).toEqual(["all-commits", "next-commit"]);
    expect(within(bar).getByRole("link", { name: "Open commit on GitHub" })).toBeVisible();
  });

  it("shows the commit's whole description under its title, in either header", () => {
    const described = [
      { ...COMMITS[0]!, body: "Why it changed.\n\n- one\n- two" },
      ...COMMITS.slice(1),
    ];
    const view = renderTab({ selectedSha: COMMITS[0]!.sha, listed: described });
    const body = within(header()).getByTestId("commit-message-body");
    expect(body.textContent).toBe("Why it changed.\n\n- one\n- two");
    // A short one shows whole, with nothing to unfold.
    expect(within(header()).queryByRole("button", { name: "Show more" })).toBeNull();
    view.rerenderWith({ timeline: false });
    expect(screen.getByTestId("commit-message-body").textContent).toBe(
      "Why it changed.\n\n- one\n- two",
    );
  });

  it("folds a long description behind Show more, and each commit starts folded", async () => {
    const user = userEvent.setup();
    const long = Array.from({ length: 12 }, (_, line) => `Line ${line + 1}`).join("\n");
    const described = COMMITS.map((entry) => ({ ...entry, body: long }));
    const view = renderTab({ selectedSha: COMMITS[0]!.sha, listed: described });
    const body = within(header()).getByTestId("commit-message-body");
    // All of it is there to read and find; the fold only clips what shows.
    expect(body.textContent).toBe(long);
    expect(body).toHaveClass("line-clamp-6");
    const more = within(header()).getByRole("button", { name: "Show more" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(more).toHaveAttribute("aria-controls", body.id);

    await user.click(more);
    expect(body).not.toHaveClass("line-clamp-6");
    expect(within(header()).getByRole("button", { name: "Show less" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    // A step to the next commit shows its description folded again.
    view.rerenderWith({ timeline: true, sha: COMMITS[1]!.sha });
    expect(within(header()).getByText("2 of 3")).toBeVisible();
    expect(within(header()).getByTestId("commit-message-body")).toHaveClass("line-clamp-6");
  });

  it("shows the title alone for an older desktop app, which sends no description", () => {
    renderTab({ selectedSha: COMMITS[1]!.sha });
    expect(within(header()).queryByTestId("commit-message-body")).toBeNull();
    expect(within(header()).getByRole("heading", { level: 3 })).toHaveTextContent(
      COMMITS[1]!.subject,
    );
  });

  it("disables the step at either end of the list", () => {
    renderTab({ selectedSha: COMMITS[0]!.sha });
    expect(
      within(header()).getByRole("button", { name: /^Previous commit: This is the first commit/ }),
    ).toBeDisabled();
    expect(within(header()).getByText("1 of 3")).toBeVisible();
  });

  it("counts the position in the loaded list, not the pull request's total", () => {
    renderTab({ selectedSha: COMMITS[2]!.sha, totalCount: 400 });
    expect(within(header()).getByText("3 of 3")).toBeVisible();
  });

  it("shows no position for a commit the loaded list doesn't include", () => {
    renderTab({ selectedSha: "9".repeat(40) });
    expect(header().querySelector(".commit-position")).toBeNull();
    expect(header()).not.toHaveTextContent(/\b0 of\b/);
    // The tab still says why: it left the pull request, or the list shown doesn't include it.
    expect(
      screen.getByText(/no longer in the pull request|isn’t in the list shown here/),
    ).toBeVisible();
  });

  it("keeps focus on the step's button after stepping", () => {
    const view = renderTab({ selectedSha: COMMITS[1]!.sha });
    view.rerenderWith({ timeline: true, request: { target: "next-commit", id: 1 } as never });
    expect(within(header()).getByRole("button", { name: "Next commit" })).toHaveFocus();
  });

  it("puts focus on the title when a commit is opened from elsewhere, in either header", () => {
    const view = renderTab({ selectedSha: COMMITS[1]!.sha });
    view.rerenderWith({ timeline: true, request: { target: "commit", id: 1 } as never });
    expect(within(header()).getByRole("heading", { level: 3 })).toHaveFocus();
    view.rerenderWith({ timeline: false, request: { target: "commit", id: 2 } as never });
    expect(screen.getByRole("heading", { level: 3 })).toHaveFocus();
  });

  it("switches between the classic header and the new one while a commit is open", () => {
    const view = renderTab({ selectedSha: COMMITS[1]!.sha, timeline: false });
    expect(document.querySelector(".commit-detail-header")).toBeNull();
    expect(screen.getByRole("heading", { level: 3 })).toHaveTextContent(COMMITS[1]!.subject);
    view.rerenderWith({ timeline: true });
    expect(within(header()).getByText("2 of 3")).toBeVisible();
    view.rerenderWith({ timeline: false });
    expect(document.querySelector(".commit-detail-header")).toBeNull();
  });
});

describe("the open commit's states on the new page", () => {
  it("loads in one line, then titles a failure with Try Again", () => {
    diff.current = { isPending: true, error: null, data: undefined };
    const view = renderTab({ selectedSha: COMMITS[0]!.sha });
    expect(screen.getByRole("status")).toHaveTextContent("Loading commit changes…");
    diff.current = {
      isPending: false,
      error: new Error("GitHub is unavailable."),
      data: undefined,
    };
    view.rerenderWith({ timeline: true });
    expect(screen.getByRole("alert")).toHaveTextContent("Commit Changes Unavailable");
    expect(screen.getByRole("button", { name: "Try Again" })).toBeVisible();
    // Off the new page, the same failure keeps the classic message.
    view.rerenderWith({ timeline: false });
    expect(screen.getByRole("alert")).toHaveTextContent("GitHub is unavailable.");
    expect(screen.getByRole("alert")).not.toHaveTextContent("Commit Changes Unavailable");
    diff.current = { isPending: false, error: null, data: { sha: "x", patch: "PATCH" } };
  });

  it("says when a commit changes no files", () => {
    diff.parse.mockImplementationOnce(() => [{ files: [] }]);
    renderTab({ selectedSha: COMMITS[0]!.sha });
    expect(screen.getByRole("status")).toHaveTextContent("No Changes");
    expect(screen.getByRole("status")).toHaveTextContent("This commit doesn’t change any files.");
  });

  it("shows the commit's files once they load", () => {
    renderTab({ selectedSha: COMMITS[0]!.sha });
    expect(screen.getByTestId("files")).toHaveTextContent("src/parser.ts");
  });
});
