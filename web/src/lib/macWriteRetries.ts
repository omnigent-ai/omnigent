// Automatic retries for a bulk run's changes that failed for a passing reason: GitHub's rate
// limit, a change already in flight, a check that couldn't reach GitHub, or an unknown outcome
// of a change main reads again before it writes. The rule matches main's for its run grant, so
// a retry is only ever asked for where main keeps the change's grant armed.

/** At most this many retries after a change's first send. */
export const BULK_RETRY_LIMIT = 3;

/** Changes main reads the state of again before it writes, so an unknown outcome can retry. */
export const RECHECKED_OPERATIONS: ReadonlySet<string> = new Set([
  "pull_request_close",
  "pull_request_reopen",
  "pull_request_ready_for_review",
  "pull_request_convert_to_draft",
]);

/** A check before the write that failed for a reason that says nothing about the change. */
const TRANSIENT_CAUSES = new Set(["network", "timeout", "unavailable"]);

/** Codes a retry of a change that may already have landed reads as that earlier send's success. */
const LANDED_CODES = new Set(["already_closed", "already_open", "already_ready", "already_draft"]);

/** Why one send failed, as main answered it. */
export interface BulkSendFailure {
  code: string;
  cause?: string | null;
}

/** Whether a change that failed so may be sent again on its run's grant. */
export function bulkRetryAllowed(
  failure: BulkSendFailure,
  { rechecksBeforeWrite, batch = false }: { rechecksBeforeWrite: boolean; batch?: boolean },
): boolean {
  if (batch) return false;
  if (failure.code === "rate_limited" || failure.code === "busy") return true;
  if (failure.code === "query_failed") return TRANSIENT_CAUSES.has(failure.cause ?? "");
  return failure.code === "outcome_unknown" && rechecksBeforeWrite;
}

/** Whether `code`, answered by a retry after an unknown outcome, means that send landed. */
export function landedEarlier(code: string): boolean {
  return LANDED_CODES.has(code);
}

/**
 * How long to wait before retry `retry` (1 for the first): a rate limit waits longer. Each
 * wait is spread by half either way, so a batch's retries don't arrive together.
 */
export function bulkRetryDelayMs(code: string, retry: number, random: () => number = Math.random) {
  const base = code === "rate_limited" ? [10_000, 30_000, 30_000] : [1_000, 3_000, 9_000];
  const delay = base[Math.min(retry, base.length) - 1]!;
  return Math.round(delay * (0.5 + random()));
}

/** How a bulk run's change may be sent again: when it may, and as that run's own send. */
export interface BulkRetry {
  /** Whether a retry may go now: the run still holds, its grant covers it, and the route holds. */
  mayRetry: () => boolean;
  /** Make the send as the run's own, so it carries the run's grant. */
  within?: <Result>(send: () => Result) => Result;
  sleep?: (ms: number) => Promise<void>;
}

export interface BulkRetryOptions extends BulkRetry {
  rechecksBeforeWrite: boolean;
  random?: () => number;
}

const wait = (ms: number) =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });

/**
 * Send once, then again while `failureOf` names a failure a retry may get past and `mayRetry`
 * allows it, waiting between. Each send is the caller's own, with nothing kept from the last.
 * Resolves with the last outcome, and whether an earlier send's outcome was unknown.
 */
export async function withBulkRetries<Outcome>(
  send: () => Promise<Outcome>,
  failureOf: (outcome: Outcome) => BulkSendFailure | null,
  {
    rechecksBeforeWrite,
    mayRetry,
    within = (resend) => resend(),
    sleep = wait,
    random = Math.random,
  }: BulkRetryOptions,
): Promise<{ outcome: Outcome; afterUnknown: boolean }> {
  let outcome = await send();
  let afterUnknown = false;
  for (let retry = 1; retry <= BULK_RETRY_LIMIT; retry += 1) {
    const failure = failureOf(outcome);
    if (!failure || !bulkRetryAllowed(failure, { rechecksBeforeWrite })) break;
    if (!mayRetry()) break;
    // oxlint-disable-next-line no-await-in-loop -- each retry waits on the last one's answer
    await sleep(bulkRetryDelayMs(failure.code, retry, random));
    // The run may have ended, or the route moved, while it waited.
    if (!mayRetry()) break;
    if (failure.code === "outcome_unknown") afterUnknown = true;
    // oxlint-disable-next-line no-await-in-loop -- one send at a time for one change
    outcome = await within(send);
  }
  return { outcome, afterUnknown };
}
