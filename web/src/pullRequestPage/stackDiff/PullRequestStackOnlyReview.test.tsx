import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DiffReviewProps } from "@/components/diff-review/DiffReview";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  PullRequestStackOnlyReview,
  type PullRequestStackOnlyReviewProps,
  stackOnlyReviewScope,
} from "./PullRequestStackOnlyReview";
import { CHILD_HEAD, compareFile, comparison, PARENT_HEAD } from "./stackDiffFixtures";

// The review renders on the real library in its own tests; here it records what the
// comparison gives it.
const review = vi.hoisted(() => ({ props: undefined as DiffReviewProps<undefined> | undefined }));
vi.mock("@/components/diff-review/DiffReview", () => ({
  DiffReview: (reviewProps: DiffReviewProps<undefined>) => {
    review.props = reviewProps;
    return <div data-testid="review">{reviewProps.source?.toolbar}</div>;
  },
}));

const PR_URL = "https://github.com/acme/app/pull/102";

function renderReview(overrides: Partial<PullRequestStackOnlyReviewProps> = {}) {
  const showAll = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <TooltipProvider>
        <PullRequestStackOnlyReview
          comparison={comparison()}
          parentLabel="#100"
          conversationId="conv_1"
          prUrl={PR_URL}
          provider="github"
          conversationCount={0}
          onShowAllChanges={showAll}
          layout="unified"
          onLayoutChange={() => undefined}
          {...overrides}
        />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return { showAll };
}

afterEach(() => window.localStorage.clear());

describe("PullRequestStackOnlyReview", () => {
  it("shows the comparison's files only to read, with no conversations or viewed marks", () => {
    renderReview();
    const props = review.props!;
    expect(props.files.map((file) => file.path)).toEqual(["src/app.ts"]);
    expect(props.contents.get("src/app.ts")?.status).toBe("ready");
    expect(props.viewedMarks).toBe(false);
    expect(props.source?.expandContext).toBeUndefined();
    expect(props.source?.annotations).toBeUndefined();
    expect(props.source?.renderAnnotation).toBeUndefined();
    expect(props.source?.renderGutterUtility).toBeUndefined();
    expect(props.emptyMessage).toBe("This PR doesn't change any files since #100.");
  });

  it("keeps its file choices apart from the PR's review, per pair of heads", () => {
    renderReview();
    expect(review.props!.reviewScope).toBe(
      `pull-request:${PR_URL}:stack:${PARENT_HEAD}..${CHILD_HEAD}`,
    );
    expect(stackOnlyReviewScope(PR_URL, comparison({ childHead: "e".repeat(40) }))).not.toBe(
      review.props!.reviewScope,
    );
  });

  it("says what it leaves to All changes, and switches there", () => {
    const { showAll } = renderReview({
      comparison: comparison({
        filesTruncated: true,
        patchesTruncated: true,
        files: [compareFile({ patch: null, patch_state: "incomplete" })],
      }),
      conversationCount: 3,
    });
    const notes = screen.getByTestId("pull-request-stack-only-notes");
    expect(notes).toHaveTextContent("Showing the first 300 files. All changes may list more.");
    expect(notes).toHaveTextContent("Some diffs aren't available here. All changes shows them.");
    expect(notes).toHaveTextContent("3 conversations are in All changes.");
    fireEvent.click(screen.getByRole("button", { name: "Show all changes" }));
    expect(showAll).toHaveBeenCalledOnce();
  });

  it("shows no notes when nothing is left out", () => {
    renderReview();
    expect(screen.queryByTestId("pull-request-stack-only-notes")).toBeNull();
  });

  it("reveals a requested file this PR changed, and opens files in tabs of their own", () => {
    const openInTab = vi.fn();
    renderReview({ revealPath: "src/app.ts", onOpenFileInNewTab: openInTab });
    expect(review.props!.revealPath).toBe("src/app.ts");
    expect(review.props!.onOpenFileInNewTab).toBe(openInTab);
    expect(screen.queryByTestId("pull-request-stack-only-notes")).toBeNull();
  });

  it("says a requested file only the parent changed is in All changes, and switches there", () => {
    const { showAll } = renderReview({ revealPath: "README.md" });
    expect(review.props!.revealPath).toBeNull();
    expect(screen.getByTestId("pull-request-stack-only-notes")).toHaveTextContent(
      "README.md is only in All changes, from #100.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Show all changes" }));
    expect(showAll).toHaveBeenCalledOnce();
  });

  it("finds as the other pull request reviews do: Find in changes, under their highlight names", () => {
    renderReview();
    expect(screen.getByRole("button", { name: "Find in changes" })).toBeInTheDocument();
    expect(review.props!.source?.findHighlightNames).toEqual({
      all: "pr-find",
      current: "pr-find-active",
    });
  });

  it("opens old lines at the parent's head, under the file's old path", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    renderReview({
      comparison: comparison({
        files: [
          compareFile({ path: "src/new.ts", previous_path: "src/old.ts", status: "renamed" }),
        ],
      }),
    });
    act(() =>
      review.props!.source!.onLineSelection!(
        "src/new.ts",
        { start: 1, end: 1, side: "deletions" },
        "commit",
      ),
    );
    const hostLink = review.props!.fileActions!(review.props!.files[0]!).find(
      (action) => action.id === "open-on-code-host",
    );
    act(() => hostLink!.run());
    expect(open).toHaveBeenCalledWith(
      `https://github.com/acme/app/blob/${PARENT_HEAD}/src/old.ts#L1`,
      "_blank",
      "noopener,noreferrer",
    );
    open.mockRestore();
  });

  it("opens each side where its commits live, for a stack whose heads are forks", () => {
    renderReview({
      comparison: comparison({
        files: [
          compareFile({ path: "src/new.ts", previous_path: "src/old.ts", status: "renamed" }),
        ],
      }),
      repositories: {
        headRepositoryUrl: "https://github.com/child-fork/app",
        parentHeadRepository: "parent-fork/app",
      },
    });
    const hostLink = () =>
      review.props!.fileActions!(review.props!.files[0]!).find(
        (action) => action.id === "open-on-code-host",
      )!;
    expect(hostLink().link?.href).toBe(
      `https://github.com/child-fork/app/blob/${CHILD_HEAD}/src/new.ts`,
    );
    act(() =>
      review.props!.source!.onLineSelection!(
        "src/new.ts",
        { start: 1, end: 1, side: "deletions" },
        "commit",
      ),
    );
    expect(hostLink().link?.href).toBe(
      `https://github.com/parent-fork/app/blob/${PARENT_HEAD}/src/old.ts#L1`,
    );
  });

  it("can't open old lines once the parent's fork is gone", () => {
    renderReview({
      comparison: comparison({
        files: [compareFile({ path: "src/gone.ts", status: "removed" })],
      }),
      repositories: { parentHeadRepository: null },
    });
    const hostLink = review.props!.fileActions!(review.props!.files[0]!).find(
      (action) => action.id === "open-on-code-host",
    );
    expect(hostLink).toMatchObject({
      disabledReason: "The parent pull request's source repository isn't known.",
      link: undefined,
    });
  });
});
