// One PR's status from every read that reports it: the live overview, the
// batched list stats, and the server's stored association. Each read is ordered
// by when it was sent, never when it answered, so a slow older read can't
// overwrite a newer one. Pure, shared by the Pulls rows, sidebar and canvas.

import type { PullRequestChecks } from "@/newPullRequestPage/hooks/usePullRequests";
import type { PullRequestCiRollup, PullRequestListStats } from "@/lib/pullRequestListStats";
import {
  hasNewerPullRequestLifecycle,
  type PullRequestStatusSummary,
} from "@/lib/pullRequestStatus";

/** A live overview read and the client time it was sent. */
export interface LivePullRequestObservation {
  pr: {
    state: string;
    is_draft: boolean;
    head_sha?: string;
    checks?: PullRequestChecks;
  };
  dispatchedAt: number;
}

/** What the server stored for the PR, checked at `state_checked_at` (epoch seconds). */
export interface StoredPullRequestObservation {
  state?: string | null;
  is_draft?: boolean;
  state_checked_at?: number | null;
}

export interface PullRequestListStatusInput {
  live?: LivePullRequestObservation;
  batch?: PullRequestListStats;
  stored?: StoredPullRequestObservation;
}

/** The CI a PR shows: live check counts, a batch rollup, or none. */
export type ResolvedPullRequestCi =
  | { source: "live"; checks: PullRequestChecks }
  | { source: "batch"; rollup: PullRequestCiRollup }
  | null;

export interface ResolvedPullRequestListStatus {
  /** `OPEN | MERGED | CLOSED`, or null when nothing reported one. */
  state: string | null;
  /** Which read the lifecycle came from; null when none reported one. */
  lifecycleSource: "live" | "batch" | "stored" | null;
  isDraft: boolean | undefined;
  /** The newest head any read reported. */
  headSha: string | undefined;
  /** Only from a read at {@link headSha}; null shows no CI badge. */
  ci: ResolvedPullRequestCi;
}

interface Observation {
  dispatchedAt: number;
  /** Live wins a tie: its read is the richer one. */
  rank: number;
  state?: string;
  isDraft?: boolean;
  headSha?: string;
  ciHeadSha?: string;
  ci?: Exclude<ResolvedPullRequestCi, null> | null;
}

function newest(candidates: readonly Observation[]): Observation | undefined {
  let best: Observation | undefined;
  for (const observation of candidates) {
    if (
      !best ||
      observation.dispatchedAt > best.dispatchedAt ||
      (observation.dispatchedAt === best.dispatchedAt && observation.rank > best.rank)
    ) {
      best = observation;
    }
  }
  return best;
}

function observationsOf({ live, batch }: PullRequestListStatusInput): Observation[] {
  const found: Observation[] = [];
  if (live) {
    found.push({
      dispatchedAt: live.dispatchedAt,
      rank: 1,
      state: live.pr.state,
      isDraft: live.pr.is_draft,
      headSha: live.pr.head_sha,
      // Live checks were read with the head beside them.
      ciHeadSha: live.pr.head_sha,
      ci: live.pr.checks ? { source: "live", checks: live.pr.checks } : undefined,
    });
  }
  if (batch) {
    // A batch from an older shell reports a head but no lifecycle or CI.
    found.push({
      dispatchedAt: batch.dispatchedAt,
      rank: 0,
      state: batch.extended ? batch.state : undefined,
      isDraft: batch.extended ? batch.isDraft : undefined,
      headSha: batch.headSha,
      ciHeadSha: batch.extended ? batch.ciHeadSha : undefined,
      ci: batch.extended ? (batch.ci ? { source: "batch", rollup: batch.ci } : null) : undefined,
    });
  }
  return found;
}

/**
 * The PR's status. Lifecycle comes from the newest-sent read, unless the stored
 * association recorded a newer change; CI only from a read at the newest known
 * head, so a stale pass never lands on an advanced head, preferring the live checks. A passing rollup is
 * only CI: it never makes a PR ready to merge.
 */
export function resolvePullRequestListStatus(
  input: PullRequestListStatusInput,
): ResolvedPullRequestListStatus {
  const found = observationsOf(input);
  const lifecycle = newest(found.filter((observation) => observation.state !== undefined));
  let state = lifecycle?.state ?? null;
  let isDraft = lifecycle?.isDraft;
  let lifecycleSource: ResolvedPullRequestListStatus["lifecycleSource"] = lifecycle
    ? lifecycle.rank === 1
      ? "live"
      : "batch"
    : null;
  const stored = input.stored;
  if (
    stored &&
    (!lifecycle ||
      hasNewerPullRequestLifecycle(
        stored,
        { state: lifecycle.state, is_draft: lifecycle.isDraft },
        lifecycle.dispatchedAt,
      ))
  ) {
    state = stored.state ?? null;
    isDraft = stored.is_draft;
    lifecycleSource = state ? "stored" : null;
  }

  const headSha = newest(found.filter((observation) => observation.headSha))?.headSha;
  const withCi = found.filter((observation) => observation.ci !== undefined);
  // Checks rerun and finish at the same head, so the newest read at the head wins.
  // Live counts stand in for a newer rollup only when they agree with it.
  const liveCi = withCi.find((observation) => observation.rank === 1);
  let ciRead: Observation | undefined;
  if (headSha === undefined) {
    // No read knows the head (an older host): the live checks are all there is.
    ciRead = liveCi;
  } else {
    const atHead = withCi.filter((observation) => observation.ciHeadSha === headSha);
    ciRead = newest(atHead);
    if (
      ciRead?.ci?.source === "batch" &&
      liveCi?.ci?.source === "live" &&
      atHead.includes(liveCi) &&
      checksRollup(liveCi.ci.checks) === ciRead.ci.rollup
    ) {
      ciRead = liveCi;
    }
  }
  return { state, isDraft, lifecycleSource, headSha, ci: ciRead?.ci ?? null };
}

/** Live check counts as the rollup GitHub would report for them; null without checks. */
function checksRollup(checks: PullRequestChecks): PullRequestCiRollup | null {
  if (checks.failing > 0 || (checks.cancelled ?? 0) > 0) return "failing";
  if (checks.pending > 0) return "pending";
  return checks.total > 0 ? "passing" : null;
}

/**
 * A resolved status as the shared status display reads it, or null when no read
 * reported a lifecycle. A batch rollup counts as one failing or running check and
 * stays partial, so a passing rollup never reads as ready to merge.
 */
export function pullRequestStatusSummaryOf(
  status: ResolvedPullRequestListStatus,
): PullRequestStatusSummary | null {
  if (!status.state) return null;
  const summary: PullRequestStatusSummary = {
    state: status.state,
    ...(status.isDraft === undefined ? {} : { is_draft: status.isDraft }),
  };
  const { ci } = status;
  if (ci?.source === "live") return { ...summary, checks: ci.checks };
  if (ci?.source === "batch") {
    return {
      ...summary,
      checks: {
        failing: ci.rollup === "failing" ? 1 : 0,
        pending: ci.rollup === "pending" ? 1 : 0,
        partial: true,
      },
    };
  }
  return summary;
}
