import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { parsePatchFiles } from "@pierre/diffs";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DiffReviewProps } from "@/components/diff-review/DiffReview";
import type * as PullRequestsModule from "@/newPullRequestPage/hooks/usePullRequests";
import type { PullRequestChangedFile } from "@/newPullRequestPage/hooks/usePullRequests";
import { readFileViewPreferences } from "@/lib/fileViewPreferences";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  commitReviewScope,
  PullRequestCommitReview,
  type PullRequestCommitReviewProps,
} from "./PullRequestCommitReview";
import { newReviewThreadDraftKey } from "@/newPullRequestPage/hooks/usePullRequests";
import { draftOwner } from "@/lib/workspaceDraftOwner";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

// The review renders on the real library in its own tests; here it records what
// a commit gives it and renders the toolbar the commit adds.
const review = vi.hoisted(() => ({ props: undefined as DiffReviewProps<undefined> | undefined }));
vi.mock("@/components/diff-review/DiffReview", () => ({
  DiffReview: (reviewProps: DiffReviewProps<undefined>) => {
    review.props = reviewProps;
    return <div data-testid="review">{reviewProps.source?.toolbar}</div>;
  },
}));

// A commit is only read, so the PR's conversation and draft hooks must stay unused.
const threadHooks = vi.hoisted(() => ({ drafts: vi.fn(), composer: vi.fn() }));
vi.mock("@/newPullRequestPage/hooks/usePullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof PullRequestsModule>()),
  useNewReviewThreadDrafts: threadHooks.drafts,
  usePullRequestReviewThreadComposer: threadHooks.composer,
}));

const PATH = "src/app.py";
const PATCH = [
  `diff --git a/${PATH} b/${PATH}`,
  `--- a/${PATH}`,
  `+++ b/${PATH}`,
  "@@ -1,3 +1,3 @@",
  " first",
  "-old",
  "+new",
  " third",
  "",
].join("\n");
const fileDiff = parsePatchFiles(PATCH)[0]!.files[0]!;
const changedFile = {
  path: PATH,
  name: "app.py",
  bytes: 1,
  modified_at: null,
  status: "modified",
  lines_added: 1,
  lines_removed: 1,
} as PullRequestChangedFile;
const PR_URL = "https://github.com/acme/app/pull/7";
const SHA = "1".repeat(40);

function props(
  overrides: Partial<PullRequestCommitReviewProps> = {},
): PullRequestCommitReviewProps {
  return {
    files: [changedFile],
    filesByPath: new Map([[PATH, fileDiff]]),
    conversationId: "conv_1",
    prUrl: PR_URL,
    sha: SHA,
    layout: "unified",
    onLayoutChange: () => undefined,
    ...overrides,
  };
}

function renderCommit(
  overrides: Partial<PullRequestCommitReviewProps> = {},
  client = new QueryClient(),
) {
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestCommitReview {...props(overrides)} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return client;
}

afterEach(() => {
  window.localStorage.clear();
  threadHooks.drafts.mockClear();
  threadHooks.composer.mockClear();
});

describe("PullRequestCommitReview", () => {
  it("shows the commit's files with no conversations, drafts, or composer", () => {
    // A PR draft the reader left in Changes stays as it was.
    const client = new QueryClient();
    const draftKey = newReviewThreadDraftKey(draftOwner(), "conv_1", PR_URL, PATH);
    const draft = { body: "Half a thought", anchor: { path: PATH } };
    client.setQueryData(draftKey, draft);
    renderCommit({}, client);

    const source = review.props!.source!;
    expect(review.props!.files.map((file) => [file.path, file.change])).toEqual([
      [PATH, "modified"],
    ]);
    expect(review.props!.contents.get(PATH)?.status).toBe("ready");
    expect(source.annotations).toBeUndefined();
    expect(source.renderAnnotation).toBeUndefined();
    expect(source.renderGutterUtility).toBeUndefined();
    expect(source.footer).toBeUndefined();
    expect(source.renderHeaderExtras).toBeUndefined();
    expect(threadHooks.drafts).not.toHaveBeenCalled();
    expect(threadHooks.composer).not.toHaveBeenCalled();
    expect(client.getQueryData(draftKey)).toBe(draft);
  });

  it("keeps viewed marks per commit, apart from the pull request's", () => {
    renderCommit();
    expect(review.props!.reviewScope).toBe(`pull-request:${PR_URL}:commit:${SHA}`);
    expect(commitReviewScope(PR_URL, SHA)).not.toBe(`pull-request:${PR_URL}`);
    expect(commitReviewScope(PR_URL, "2".repeat(40))).not.toBe(review.props!.reviewScope);
  });

  it("names its ⌘K rows for the commit, and withdraws them while hidden", () => {
    renderCommit({ active: false });
    expect(review.props!.commandHeading).toBe("Commit");
    expect(review.props!.commandsActive).toBe(false);
  });

  it("opens a file on the PR's host at this commit, at the lines picked", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    renderCommit({ provider: "github" });
    act(() =>
      review.props!.source!.onLineSelection!(
        PATH,
        { start: 2, end: 2, side: "additions" },
        "commit",
      ),
    );
    const [hostLink] = review.props!.fileActions!(review.props!.files[0]!);
    expect(hostLink).toMatchObject({ id: "open-on-code-host", label: "Open in GitHub" });
    act(() => hostLink!.run());
    expect(open).toHaveBeenCalledWith(
      `https://github.com/acme/app/blob/${SHA}/${PATH}#L2`,
      "_blank",
      "noopener,noreferrer",
    );
    open.mockRestore();
  });

  it("opens a fork PR's commit in the fork, at that commit and the lines picked", () => {
    renderCommit({
      provider: "github",
      repositories: { headRepositoryUrl: "https://github.com/fork/app" },
    });
    act(() =>
      review.props!.source!.onLineSelection!(
        PATH,
        { start: 2, end: 2, side: "additions" },
        "commit",
      ),
    );
    const [hostLink] = review.props!.fileActions!(review.props!.files[0]!);
    expect(hostLink!.link?.href).toBe(`https://github.com/fork/app/blob/${SHA}/${PATH}#L2`);
  });

  it("can't open a commit's file when the PR's source repository is known to be unknown", () => {
    renderCommit({ provider: "github", repositories: { headRepositoryUrl: null } });
    const [hostLink] = review.props!.fileActions!(review.props!.files[0]!);
    expect(hostLink).toMatchObject({
      id: "open-on-code-host",
      disabledReason: "The pull request's source repository isn't known.",
      link: undefined,
    });
  });

  it("paints its find under a pull request's highlight names", () => {
    renderCommit();
    expect(review.props!.source?.findHighlightNames).toEqual({
      all: "pr-find",
      current: "pr-find-active",
    });
  });

  it("names Example Host for a Example Host pull request", () => {
    renderCommit({
      provider: "named-host",
      prUrl: "https://2.host.example.com/eng/universe/pull/9",
    });
    const [hostLink] = review.props!.fileActions!(review.props!.files[0]!);
    expect(hostLink).toMatchObject({ id: "open-on-code-host", label: "Open in Example Host" });
  });

  it("opens its find from the toolbar, and shares the saved wrap preference", () => {
    renderCommit();
    expect(review.props!.findOpen).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Find in changes" }));
    expect(review.props!.findOpen).toBe(true);
    act(() => review.props!.onWrapLinesChange!(true));
    expect(review.props!.wrapLines).toBe(true);
    expect(readFileViewPreferences().wrapLines).toBe(true);
  });
});
