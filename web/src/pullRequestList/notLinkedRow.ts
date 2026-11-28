// A list row the session's machine won't read because the session doesn't link its pull
// request, such as one this Mac found on the session's branch. The owner can link it from the row.

import { PullRequestApiError } from "@/lib/pullRequestRpc";

/** How the server refuses to read a pull request for a session that doesn't link it. */
export const NOT_LINKED_REFUSAL = "This pull request is not associated with the session";

/** What a Not Linked row says, for the tooltip, while no link of it has failed. */
export const NOT_LINKED_DETAIL =
  "This session doesn’t link this pull request, so its machine won’t read it. Open it on GitHub, or link it to this session.";

/** Whether a row's read failed because the reading session doesn't link the pull request. */
export function isNotLinkedRefusal(error: unknown): boolean {
  return (
    error instanceof PullRequestApiError &&
    error.status === 400 &&
    error.message.includes(NOT_LINKED_REFUSAL)
  );
}
