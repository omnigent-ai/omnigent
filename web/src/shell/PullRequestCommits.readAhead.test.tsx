// Stepping through the Commits tab with the real commit reads: a step to a neighbour shows
// its files at once, because the tab read its patch ahead while the reader looked at this one.
import { act, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { readCommitDiffOnHost } from "@/lib/hostPullRequestReads";
import type { PullRequestCommit } from "@/lib/pullRequestCommits";

import { PullRequestCommitsTab } from "./PullRequestCommits";

vi.mock("@/lib/hostPullRequestReads", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readCommitDiffOnHost: vi.fn(),
}));

/** About what one commit's patch takes to read from the host (median of real reads). */
const READ_MS = 1200;
const commit = (digit: string): PullRequestCommit => ({
  sha: digit.repeat(40),
  subject: `Commit ${digit}`,
  author_login: "dev",
  author_name: "Dev",
  authored_at: "2026-11-03T09:00:00Z",
  committed_at: "2026-11-03T09:00:00Z",
  url: null,
  ci_state: null,
});
const COMMITS = ["1", "2", "3", "4"].map(commit);
const SOURCE = {
  account: { host: "github.com", login: "dev" },
  repository: "acme/app",
  number: 7,
  headSha: "f".repeat(40),
  baseSha: "e".repeat(40),
  host: { conversationId: "s1", prUrl: "https://github.com/acme/app/pull/7" },
};
const patchFor = (sha: string) =>
  [
    `diff --git a/file-${sha[0]}.ts b/file-${sha[0]}.ts`,
    "index 1111111..2222222 100644",
    `--- a/file-${sha[0]}.ts`,
    `+++ b/file-${sha[0]}.ts`,
    "@@ -1 +1 @@",
    "-old",
    "+new",
    "",
  ].join("\n");

function renderTab() {
  const client = new QueryClient();
  const element = (sha: string) => (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestCommitsTab
          status={{ kind: "ready" }}
          onRetry={() => {}}
          revision={{ headSha: SOURCE.headSha, baseSha: SOURCE.baseSha }}
          source={SOURCE}
          commits={
            {
              data: {
                commits: COMMITS,
                total_count: COMMITS.length,
                head_sha: SOURCE.headSha,
                base_sha: SOURCE.baseSha,
                complete: true,
              },
            } as never
          }
          selectedSha={sha}
          onSelectCommit={() => {}}
          commands={{}}
          focusRequest={null}
          onFocusRequestSettled={() => {}}
          provider="github"
          prUrl="https://github.com/acme/app/pull/7"
          renderFiles={(files) => <div data-testid="files">{files.map((file) => file.path)}</div>}
        />
      </TooltipProvider>
    </QueryClientProvider>
  );
  const view = render(element(COMMITS[0]!.sha));
  return { show: (index: number) => view.rerender(element(COMMITS[index]!.sha)) };
}

/** Let `ms` pass, and the cache tell the tab what answered meanwhile. */
const waitFor = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms + 1));

const readShas = () => vi.mocked(readCommitDiffOnHost).mock.calls.map((call) => call[2][0]);

describe("stepping through commits", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(readCommitDiffOnHost).mockReset();
    vi.mocked(readCommitDiffOnHost).mockImplementation(
      (_session, _prUrl, sha) =>
        new Promise((resolve) => {
          setTimeout(() => resolve({ sha, patch: patchFor(sha) }), READ_MS);
        }),
    );
  });
  afterEach(() => vi.useRealTimers());

  it("shows the next commit's files at once, read while the reader was on this one", async () => {
    const tab = renderTab();
    await act(async () => {});
    expect(screen.getByText("Loading changes…")).toBeTruthy();
    await waitFor(READ_MS);
    expect(screen.getByTestId("files").textContent).toBe("file-1.ts");
    // Opening the first commit read the second ahead, beside its own.
    expect(readShas()).toEqual(["1", "2"]);

    tab.show(1);
    expect(screen.queryByText("Loading changes…")).toBeNull();
    expect(screen.getByTestId("files").textContent).toBe("file-2.ts");
    // Stepping down reads the next two that way; the one above is already here.
    await waitFor(0);
    expect(readShas()).toEqual(["1", "2", "3", "4"]);

    await waitFor(READ_MS);
    tab.show(2);
    expect(screen.getByTestId("files").textContent).toBe("file-3.ts");
    tab.show(3);
    expect(screen.getByTestId("files").textContent).toBe("file-4.ts");
    // Back up, every commit is already read.
    tab.show(2);
    tab.show(1);
    expect(screen.getByTestId("files").textContent).toBe("file-2.ts");
    expect(readShas()).toEqual(["1", "2", "3", "4"]);
  });
});
