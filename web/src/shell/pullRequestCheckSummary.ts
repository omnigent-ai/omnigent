// The Summary tab's CI checks: a headline for the head commit's results, the jobs that
// need attention, and the completed jobs folded below them. Pure, so the wording is tested.

import type {
  PullRequestCheckRun,
  PullRequestChecks,
} from "@/newPullRequestPage/hooks/usePullRequests";

export type CheckSummaryTone = "failing" | "pending" | "passing" | "cancelled" | "skipped" | "none";

export interface CheckSummary {
  tone: CheckSummaryTone;
  /** The headline, e.g. "2 failing checks". */
  title: string;
  /** The counts behind it, e.g. "2 failing, 18 successful, 3 skipped". */
  detail: string;
  /** Failing and running jobs, always listed. */
  attention: PullRequestCheckRun[];
  /** Everything else, folded under "N completed checks". */
  completed: PullRequestCheckRun[];
  /** A short all-quiet list starts unfolded. */
  completedOpen: boolean;
  /** Failing or running checks counted but not listed, e.g. past the host's job cap. */
  unlistedAttention: number;
  /** Completed checks counted but not listed. */
  unlistedCompleted: number;
}

const BUCKET_ORDER: Record<PullRequestCheckRun["bucket"], number> = {
  failing: 0,
  pending: 1,
  cancelled: 2,
  skipped: 3,
  passing: 4,
};

/** Failures first, then running, cancelled, skipped, and passing jobs, each by name. */
export function orderCheckRuns(runs: readonly PullRequestCheckRun[]): PullRequestCheckRun[] {
  return runs
    .filter((run) => run.name)
    .sort(
      (left, right) =>
        BUCKET_ORDER[left.bucket] - BUCKET_ORDER[right.bucket] ||
        left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: "base" }),
    );
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function completedChecksLabel(count: number): string {
  return plural(count, "completed check");
}

/** Why some counted checks have no row, e.g. "1 failing or running check isn't listed here." */
export function unlistedChecksLabel(unlistedAttention: number, unlistedCompleted: number): string {
  if (unlistedAttention > 0) {
    return unlistedAttention === 1
      ? "1 failing or running check isn’t listed here."
      : `${unlistedAttention} failing or running checks aren’t listed here.`;
  }
  return unlistedCompleted === 1
    ? "1 completed check isn’t listed here."
    : `${unlistedCompleted} completed checks aren’t listed here.`;
}

export function summarizeChecks(checks: PullRequestChecks, providerLabel: string): CheckSummary {
  const { total, passing, failing, pending } = checks;
  const cancelled = checks.cancelled ?? 0;
  const skipped = checks.skipped ?? 0;
  const ordered = orderCheckRuns(checks.runs);
  const attention = ordered.filter((run) => run.bucket === "failing" || run.bucket === "pending");
  const completed = ordered.filter((run) => run.bucket !== "failing" && run.bucket !== "pending");
  // The counts stay exact when the host caps how many jobs it lists, so the gaps come from them.
  const unlistedAttention = Math.max(0, failing + pending - attention.length);
  const unlistedCompleted = Math.max(0, passing + cancelled + skipped - completed.length);
  const completedOpen = failing + pending === 0 && completed.length <= 5;
  // Counts that don't add up mean some results weren't read, so nothing claims "all passed".
  const incomplete = !!checks.partial || passing + failing + pending + cancelled + skipped < total;

  const counts = [
    failing && `${failing} failing`,
    pending && `${pending} running`,
    passing && `${passing} successful`,
    cancelled && `${cancelled} cancelled`,
    skipped && `${skipped} skipped`,
  ]
    .filter(Boolean)
    .join(", ");
  const detail = incomplete && counts ? `${counts} · some checks couldn’t be loaded` : counts;

  const result = (tone: CheckSummaryTone, title: string, summaryDetail = detail) => ({
    tone,
    title,
    detail: summaryDetail,
    attention,
    completed,
    completedOpen,
    unlistedAttention,
    unlistedCompleted,
  });

  if (total === 0) {
    return incomplete
      ? result("none", "Checks couldn’t be loaded", "Open the pull request to see its checks.")
      : result("none", "No checks reported", `${providerLabel} has no CI results for this commit.`);
  }
  if (failing > 0) return result("failing", plural(failing, "failing check"));
  if (pending > 0) return result("pending", "Checks are still running");
  if (incomplete) return result("none", "Some checks couldn’t be loaded", counts);
  if (cancelled > 0) return result("cancelled", "Checks were cancelled");
  if (passing === 0) return result("skipped", "Checks were skipped");
  return result("passing", "All checks have passed");
}
