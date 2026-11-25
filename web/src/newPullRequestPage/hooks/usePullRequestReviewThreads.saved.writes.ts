// The cache changes a finished write makes to a conversations page, as usePullRequests makes
// them: a created conversation as GitHub answered it, and a comment edited or deleted.

import type { QueryClient, QueryKey } from "@tanstack/react-query";

import type { ReviewComment, ReviewThread } from "@/lib/pullRequestReviewThreads";
import { withChangedComment, withWrittenThread } from "@/lib/reviewThreadsHistory";
import type { TimedThreadsPage } from "@/lib/reviewThreadsReadAt";

/** `thread`, created and answered now, shown in the page under `key`. */
export function writeThread(client: QueryClient, key: QueryKey, thread: ReviewThread) {
  const answeredAt = Date.now();
  client.setQueryData<TimedThreadsPage>(
    key,
    (page) => page && withWrittenThread(page, thread, answeredAt),
  );
}

/** `commentId` in `threadId` edited to `body`, or deleted without one. */
export function changeComment(
  client: QueryClient,
  key: QueryKey,
  threadId: string,
  commentId: string,
  body?: string,
) {
  client.setQueryData<TimedThreadsPage>(
    key,
    (page) =>
      page &&
      withChangedComment(page, threadId, commentId, (value: ReviewComment) =>
        body === undefined ? null : { ...value, body },
      ),
  );
}
