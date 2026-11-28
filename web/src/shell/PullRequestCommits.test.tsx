// The Commits tab on its own: the host's name in its copy, its arrow keys, its
// header buttons, and Mac sign-in recovery. The panel wiring is in PullRequestPanel.test.
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { TooltipProvider } from "@/components/ui/tooltip";
import type * as UsePullRequestCommitsModule from "@/hooks/usePullRequestCommits";
import type { CommitsTabStatus, PullRequestCommit } from "@/lib/pullRequestCommits";

import { PullRequestCommitsTab } from "./PullRequestCommits";
import { commitCommands } from "./pullRequestCommits/commitCommands";
import type {
  CommitFocusTarget,
  RunnableCommitCommand,
} from "./pullRequestCommits/useCommitCommands";
import type { PendingFocus } from "@/hooks/usePendingFocus";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

const diff = vi.hoisted(() => ({
  current: { isPending: false, error: null as Error | null, data: { sha: "x", patch: "PATCH" } },
  parse: vi.fn(() => [{ files: [{ name: "src/parser.ts", type: "change", hunks: [] }] }]),
}));

vi.mock("@/hooks/usePullRequestCommits", async (importOriginal) => ({
  ...(await importOriginal<typeof UsePullRequestCommitsModule>()),
  useCommitDiff: () => diff.current,
}));
vi.mock("@pierre/diffs", () => ({ parsePatchFiles: diff.parse }));

const PR_URL = "https://1.host.example.com/acme/app/pull/7";
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
  commit("2", "Fix the parser"),
  commit("3", "Test it"),
];
const SOURCE = {
  account: { host: "1.host.example.com", login: "dev" },
  repository: "acme/app",
  number: 7,
  headSha: "f".repeat(40),
  baseSha: "e".repeat(40),
};

/** Runnable commands for the commit at `index`, each recording when it runs. */
function commandsFor(index: number | null, ran: string[] = []) {
  const open =
    index === null
      ? null
      : {
          sha: COMMITS[index]!.sha,
          listed: true,
          previousSha: COMMITS[index - 1]?.sha ?? null,
          nextSha: COMMITS[index + 1]?.sha ?? null,
          moreAfterList: false,
        };
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
  totalCount = COMMITS.length,
  provider = "named-host",
  focusRequest = null,
  onFocusRequestSettled = vi.fn(),
  ran = [] as string[],
  onSelectCommit = vi.fn(),
  onRetry = vi.fn(),
}: {
  status?: CommitsTabStatus;
  selectedSha?: string;
  totalCount?: number;
  provider?: string | null;
  focusRequest?: PendingFocus<CommitFocusTarget> | null;
  onFocusRequestSettled?: () => void;
  ran?: string[];
  onSelectCommit?: (sha: string | undefined) => void;
  onRetry?: () => void;
} = {}) {
  const client = new QueryClient();
  const element = (sha: string | undefined, request: PendingFocus<CommitFocusTarget> | null) => (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestCommitsTab
          status={status}
          onRetry={onRetry}
          revision={{ headSha: SOURCE.headSha, baseSha: SOURCE.baseSha }}
          source={SOURCE}
          commits={
            {
              data: {
                commits: COMMITS,
                total_count: totalCount,
                head_sha: SOURCE.headSha,
                base_sha: SOURCE.baseSha,
                complete: totalCount === COMMITS.length,
              },
            } as never
          }
          selectedSha={sha}
          onSelectCommit={onSelectCommit}
          commands={commandsFor(sha ? COMMITS.findIndex((entry) => entry.sha === sha) : null, ran)}
          focusRequest={request}
          onFocusRequestSettled={onFocusRequestSettled}
          provider={provider}
          prUrl={PR_URL}
          renderFiles={(files) => <div data-testid="files">{files.map((file) => file.path)}</div>}
        />
      </TooltipProvider>
    </QueryClientProvider>
  );
  const view = render(element(selectedSha, focusRequest));
  return {
    ...view,
    show: (sha: string | undefined, request: PendingFocus<CommitFocusTarget> | null = null) =>
      view.rerender(element(sha, request)),
  };
}

afterEach(() => {
  diff.current = { isPending: false, error: null, data: { sha: "x", patch: "PATCH" } };
  diff.parse.mockImplementation(() => [
    { files: [{ name: "src/parser.ts", type: "change", hunks: [] }] },
  ]);
});

describe("the commit list", () => {
  const row = (subject: string) => screen.getByRole("button", { name: new RegExp(subject) });

  it("moves between commits with the arrow keys, Home and End, and opens one with Enter", async () => {
    const onSelectCommit = vi.fn();
    renderTab({ onSelectCommit });
    const list = screen.getByRole("list", { name: "Commits" });
    // One tab stop for the whole list.
    expect(
      within(list)
        .getAllByRole("button")
        .filter((button) => button.tabIndex === 0),
    ).toEqual([row("Add the parser")]);
    row("Add the parser").focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(row("Fix the parser")).toHaveFocus();
    await userEvent.keyboard("{End}");
    expect(row("Test it")).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}");
    expect(row("Test it")).toHaveFocus();
    await userEvent.keyboard("{Home}");
    expect(row("Add the parser")).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(onSelectCommit).toHaveBeenCalledWith(COMMITS[1]!.sha);
  });

  it("returns focus to the commit just left when asked to focus the list, once", () => {
    const onFocusRequestSettled = vi.fn();
    const { show } = renderTab({ selectedSha: COMMITS[2]!.sha, onFocusRequestSettled });
    show(undefined, { target: "list" });
    expect(row("Test it")).toHaveFocus();
    expect(row("Test it").tabIndex).toBe(0);
    expect(onFocusRequestSettled).toHaveBeenCalledOnce();
  });

  it("links the rest of a long list to the PR's commits on its host", () => {
    renderTab({ totalCount: 300 });
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Showing the first 3 of 300 commits.");
    expect(
      within(status).getByRole("link", { name: "See the rest on Example Host" }),
    ).toHaveAttribute("href", `${PR_URL}/commits`);
  });

  it("says why any other list read failed, with a retry", () => {
    const error = new Error("Example Host couldn't answer.");
    renderTab({ status: { kind: "failed", message: error.message, error } });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Couldn’t load commits. Example Host couldn't answer.",
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("an open commit", () => {
  it("names the PR's host in its link", () => {
    renderTab({ selectedSha: COMMITS[1]!.sha });
    expect(screen.getByRole("link", { name: "Open commit on Example Host" })).toHaveAttribute(
      "href",
      COMMITS[1]!.url,
    );
  });

  it("runs the shared commands from its header, greyed out with the reason at the ends", async () => {
    const ran: string[] = [];
    renderTab({ selectedSha: COMMITS[0]!.sha, ran });
    expect(
      screen.getByRole("button", { name: "Previous commit: This is the first commit." }),
    ).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Next commit" }));
    await userEvent.click(screen.getByRole("button", { name: "All changes" }));
    await userEvent.click(screen.getByRole("button", { name: "All commits" }));
    expect(ran).toEqual(["next-commit", "all-changes", "all-commits"]);
  });

  it("keeps focus in its header after a step, on the other button at either end", () => {
    const onFocusRequestSettled = vi.fn();
    const { show } = renderTab({ selectedSha: COMMITS[0]!.sha, onFocusRequestSettled });
    show(COMMITS[1]!.sha, { target: "next-commit" });
    expect(screen.getByRole("button", { name: "Next commit" })).toHaveFocus();
    show(COMMITS[2]!.sha, { target: "next-commit" });
    // Next is greyed out on the last commit, so focus stays on Previous.
    expect(screen.getByRole("button", { name: "Previous commit" })).toHaveFocus();
    show(COMMITS[1]!.sha, { target: "previous-commit" });
    expect(screen.getByRole("button", { name: "Previous commit" })).toHaveFocus();
    show(COMMITS[0]!.sha, { target: "previous-commit" });
    expect(screen.getByRole("button", { name: "Next commit" })).toHaveFocus();
    expect(onFocusRequestSettled).toHaveBeenCalledTimes(4);
  });

  it("leaves a list request for the list", () => {
    const onFocusRequestSettled = vi.fn();
    renderTab({
      selectedSha: COMMITS[1]!.sha,
      focusRequest: { target: "list" },
      onFocusRequestSettled,
    });
    expect(document.body).toHaveFocus();
    expect(onFocusRequestSettled).not.toHaveBeenCalled();
  });

  it("points to the host when its diff can't be read", () => {
    diff.parse.mockImplementation(() => {
      throw new Error("bad patch");
    });
    renderTab({ selectedSha: COMMITS[1]!.sha });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Couldn’t read this commit’s diff. Open it on Example Host instead.",
    );
  });

  it("names GitHub for a PR whose host doesn’t say which provider it is", () => {
    diff.parse.mockImplementation(() => {
      throw new Error("bad patch");
    });
    renderTab({ selectedSha: COMMITS[1]!.sha, provider: null });
    expect(screen.getByRole("link", { name: "Open commit on GitHub" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Open it on GitHub instead.");
  });
});
