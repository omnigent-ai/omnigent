// Check Again for a published comment's change through the host settles only on reads it made
// itself: a kept read from before, which nothing on screen reads again, says nothing.
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import type { ReviewThreadsPage } from "@/lib/pullRequestReviewThreads";

import { readCommentThroughHost } from "./hostCommentCheck";

const SESSION = "session-1";
const PR = "https://github.com/acme/app/pull/42";
const KEY = ["pr-page-github-review-threads", SESSION, PR];
// A complete page of the comment's thread, without the comment: proof it's gone, if fresh.
const withoutComment: ReviewThreadsPage = {
  data: [{ id: "PRRT_1", comments: { data: [], has_more: false } }],
  has_more: false,
} as unknown as ReviewThreadsPage;
const target = {
  kind: "review" as const,
  prUrl: PR,
  commentId: "PRRC_1",
  threadId: "PRRT_1",
} as Parameters<typeof readCommentThroughHost>[2];

describe("readCommentThroughHost", () => {
  it("can't say from a kept read that wasn't read again", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(KEY, withoutComment);

    expect(await readCommentThroughHost(queryClient, SESSION, target)).toBeNull();
  });

  it("says the comment is gone from a read the check made", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(KEY, { data: [], has_more: true });
    const observer = new QueryObserver(queryClient, {
      queryKey: KEY,
      queryFn: async () => withoutComment,
      staleTime: Infinity,
    });
    const stop = observer.subscribe(() => {});

    expect(await readCommentThroughHost(queryClient, SESSION, target)).toEqual({ state: "absent" });
    stop();
  });
});
