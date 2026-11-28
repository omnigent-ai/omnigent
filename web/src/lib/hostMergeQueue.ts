// A pull request's place in its base's merge queue, read and changed through the session's host
// with GetMergeQueue, EnqueuePullRequest and DequeuePullRequest.

import { hostWrite } from "@/lib/hostPullRequestWrites";
import { readPullRequestRpc } from "@/lib/pullRequestRpc";

/** Where the PR stands in its base's queue, as the host read it. */
export interface MergeQueueState {
  prUrl: string;
  /** Whether the base merges through a queue; null where GitHub didn't say. */
  enabled: boolean | null;
  /** Whether the PR is in the queue; null where GitHub didn't say. */
  queued: boolean | null;
  /** Its 1-based place in the queue while queued. */
  position: number | null;
  /** GitHub's state for its queue entry, e.g. `QUEUED` or `AWAITING_CHECKS`. */
  state: string | null;
  headSha: string | null;
}

const optionalBoolean = (value: unknown): boolean | null | undefined =>
  value === null || value === undefined ? null : typeof value === "boolean" ? value : undefined;

const optionalText = (value: unknown): string | null =>
  typeof value === "string" && value ? value : null;

/** The host's answer for `prUrl`, or null for anything that isn't one. */
export function parseMergeQueueState(answer: unknown, prUrl: string): MergeQueueState | null {
  if (typeof answer !== "object" || answer === null || Array.isArray(answer)) return null;
  const data = answer as Record<string, unknown>;
  if (data.object !== "session.github.merge_queue" || data.pr_url !== prUrl) return null;
  const enabled = optionalBoolean(data.enabled);
  const queued = optionalBoolean(data.queued);
  if (enabled === undefined || queued === undefined) return null;
  const position =
    typeof data.position === "number" && Number.isInteger(data.position) && data.position > 0
      ? data.position
      : null;
  return {
    prUrl,
    enabled,
    queued,
    position: queued ? position : null,
    state: queued ? optionalText(data.state) : null,
    headSha: optionalText(data.head_sha),
  };
}

/** Read the PR's queue state; an answer that can't be read is treated as no queue offer. */
export async function readMergeQueue(
  conversationId: string,
  prUrl: string,
  signal?: AbortSignal,
): Promise<MergeQueueState | null> {
  const answer = await readPullRequestRpc<unknown>(
    conversationId,
    "getMergeQueue",
    (client) => client.getMergeQueue({ prUrl }),
    signal,
  );
  return parseMergeQueueState(answer, prUrl);
}

/** Add the PR to its base's queue, only while its head is `expectedHead`. */
export function enqueueOnHost(
  conversationId: string,
  prUrl: string,
  expectedHead: string,
): Promise<MergeQueueState> {
  return hostWrite(
    conversationId,
    {
      method: "enqueuePullRequest",
      call: (client) => client.enqueuePullRequest({ prUrl, expectedHeadOid: expectedHead }),
    },
    (answer) => parseMergeQueueState(answer, prUrl),
  );
}

/** Take the PR out of its base's queue. */
export function dequeueOnHost(conversationId: string, prUrl: string): Promise<MergeQueueState> {
  return hostWrite(
    conversationId,
    {
      method: "dequeuePullRequest",
      call: (client) => client.dequeuePullRequest({ prUrl }),
    },
    (answer) => parseMergeQueueState(answer, prUrl),
  );
}
