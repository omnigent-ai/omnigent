// Drafts as the page shows them: marked Pending in their thread, and the pending review's
// notices for a read that failed or is out of date, an incomplete review, and drafts not placed.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { openLink } = vi.hoisted(() => ({ openLink: vi.fn() }));
vi.mock("@/lib/openLinkInApp", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  openLink,
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import { PullRequestReviewThread } from "@/newPullRequestPage/shell/PullRequestReviewThread";
import { noReviewAnswer, pendingAnswer, unplacedDraft } from "@/test/pendingReviewAnswers";

import {
  parsePendingReviewAnswer,
  type PendingReviewRead,
  type PendingReviewState,
  pendingReviewState,
  type UnplacedDraft,
} from "./pendingReview";
import { PendingReviewNotice } from "./PendingReviewNotice";

const PR = "https://github.com/acme/app/pull/42";
const stateOf = (answer: unknown, failed = false): PendingReviewState =>
  pendingReviewState({
    offered: true,
    data: parsePendingReviewAnswer(answer) as PendingReviewRead,
    dataUpdatedAt: Date.now() - 2 * 60_000,
    failed,
    fetching: false,
  });

function notice(state: PendingReviewState, notPlaced: UnplacedDraft[] = []) {
  const onRetry = vi.fn();
  render(
    <PendingReviewNotice
      state={state}
      notPlaced={notPlaced}
      conversationId="session-1"
      pullRequestUrl={PR}
      onRetry={onRetry}
    />,
  );
  return { onRetry };
}

afterEach(() => openLink.mockReset());

describe("a draft in its thread", () => {
  it("is marked Pending, saying only its author can see it; a published comment isn't", () => {
    const thread = {
      id: "PRRT_1",
      path: "src/app.ts",
      side: "RIGHT",
      line: 12,
      start_line: null,
      subject_type: "LINE",
      resolved: false,
      outdated: false,
      can_reply: false,
      diff_hunk: "",
      comments: {
        data: [
          { id: "PRRC_1", author: "carol", body: "Published.", created_at: null, url: null },
          {
            id: "PRRC_2",
            author: "alice",
            body: "Draft.",
            created_at: null,
            url: null,
            pending: true,
            pending_review_id: "PRR_1",
          },
        ],
        has_more: false,
        next_cursor: null,
      },
    } as ReviewThread;
    render(
      <QueryClientProvider client={new QueryClient()}>
        <TooltipProvider>
          <PullRequestReviewThread
            thread={thread}
            context={{
              conversationId: "session-1",
              prUrl: PR,
              account: "alice",
              canReply: true,
              loadComments: vi.fn(),
              loadingComments: null,
            }}
          />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    const badges = screen.getAllByText("Pending");
    expect(badges).toHaveLength(1);
    expect(badges[0]!.closest("li")).toHaveTextContent("Draft.");
    expect(badges[0]).toHaveTextContent(
      "Pending: Only you can see this until you submit your review.",
    );
  });
});

describe("the pending review's notices", () => {
  it("says nothing for a current read with no review pending", () => {
    const { container } = render(
      <PendingReviewNotice
        state={stateOf(noReviewAnswer())}
        notPlaced={[]}
        conversationId="session-1"
        pullRequestUrl={PR}
        onRetry={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("says nothing for a current, complete pending review with every draft placed", () => {
    const { container } = render(
      <PendingReviewNotice
        state={stateOf(pendingAnswer({ unplaced: [] }))}
        notPlaced={[]}
        conversationId="session-1"
        pullRequestUrl={PR}
        onRetry={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("says a read that failed couldn't check, with Retry and Open on GitHub", () => {
    const { onRetry } = notice({ kind: "error" });
    expect(screen.getByRole("status")).toHaveTextContent("Couldn’t check for your pending review.");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Open on GitHub" }));
    expect(openLink).toHaveBeenCalledWith("session-1", PR);
  });

  it("says when the last good read is out of date", () => {
    notice(stateOf(noReviewAnswer(), true));
    expect(screen.getByRole("status")).toHaveTextContent(
      "Couldn’t check your pending review again. Last checked 2m ago.",
    );
  });

  it("counts every comment GitHub has, beside how many are shown", () => {
    notice(stateOf(pendingAnswer({ unplaced: [unplacedDraft("PRRC_1")], total: 40 })));
    expect(screen.getByRole("status")).toHaveTextContent(
      "Showing 1 of the 40 comments in your pending review. The rest are on GitHub.",
    );
  });

  it("lists drafts no shown thread holds, each opening on GitHub", () => {
    const draft: UnplacedDraft = {
      ...unplacedDraft("PRRC_1", { path: "src/b.ts", line: 7 }),
      url: `${PR}#r1`,
    };
    notice(stateOf(pendingAnswer({ unplaced: [draft] })), [draft]);
    expect(screen.getByRole("status")).toHaveTextContent("Pending comments not shown in the diff:");
    expect(screen.getByRole("listitem")).toHaveTextContent("src/b.ts:7");
    fireEvent.click(screen.getByRole("button", { name: "Open src/b.ts:7 on GitHub" }));
    expect(openLink).toHaveBeenCalledWith("session-1", `${PR}#r1`);
  });

  it("says nothing while loading or where pending reviews aren't offered", () => {
    for (const state of [{ kind: "loading" }, { kind: "unavailable" }] as const) {
      const { container, unmount } = render(
        <PendingReviewNotice
          state={state}
          notPlaced={[]}
          conversationId="session-1"
          pullRequestUrl={PR}
          onRetry={vi.fn()}
        />,
      );
      expect(container).toBeEmptyDOMElement();
      unmount();
    }
  });
});
