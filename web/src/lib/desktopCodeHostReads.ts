// The desktop shell's reads from a code host as this Mac's own identity: which operations it
// offers, and each read, paced by GitHub's cooldowns and read budget.

import type { CodeHostProviderId } from "@/lib/codeHosts";
import {
  discoverRequestCooldownKey,
  githubCooldowns,
  requestCooldownKey,
} from "@/lib/githubCooldown";
import { githubReadBudgets } from "@/lib/githubReadBudget";
import { randomUUID } from "@/lib/randomUUID";
import { desktopCodeHostQuery, hasCodeHostBridge, hasCodeHostReadCancel } from "@/lib/macBridge";
import {
  CODE_HOST_READ_CANCELLED,
  getDesktopFeatures,
  type CodeHostQueryRequest,
  type CodeHostReadPriority,
  type GitHubFastTrackResult,
  type GitHubRateLimit,
} from "@/lib/nativeBridge";

async function desktopCodeHost(provider: CodeHostProviderId) {
  if (!hasCodeHostBridge()) return undefined;
  const features = await getDesktopFeatures();
  const hosts = Array.isArray(features?.codeHosts) ? features.codeHosts : [];
  return hosts.find((candidate) => candidate?.id === provider);
}

type DesktopCodeHost = Awaited<ReturnType<typeof desktopCodeHost>>;

const operationsOf = (entry: DesktopCodeHost): readonly string[] =>
  Array.isArray(entry?.operations) ? entry.operations : [];

const readsByPriority = (entry: DesktopCodeHost) =>
  hasCodeHostReadCancel() && entry?.readPriority === 1;

/**
 * Whether this shell reads GitHub list stats a row can follow: with each PR's snapshot, as the
 * row's own reader. Older shells answer without either, so their rows keep polling.
 */
export async function desktopRowSnapshotsSupport(): Promise<
  "supported" | "outdated" | "unavailable"
> {
  const stats = await desktopCodeHostStatsSupport("github");
  if (stats !== "supported") return stats;
  return (await desktopCodeHost("github"))?.rowSnapshots === 1 ? "supported" : "outdated";
}

/** The operations this shell runs for `provider`, or none on older shells and other clients. */
export async function desktopCodeHostOperations(
  provider: CodeHostProviderId,
): Promise<readonly string[]> {
  return operationsOf(await desktopCodeHost(provider));
}

/**
 * Whether this shell reads `provider`'s list stats with their reader: `outdated` when
 * it predates that, `unavailable` when it doesn't offer `provider` at all.
 */
export async function desktopCodeHostStatsSupport(
  provider: CodeHostProviderId,
): Promise<"supported" | "outdated" | "unavailable"> {
  if (!hasCodeHostBridge()) return "outdated";
  const entry = await desktopCodeHost(provider);
  if (!entry) return "unavailable";
  return entry.provenance === true &&
    Array.isArray(entry.operations) &&
    entry.operations.includes("pull_request_list_stats")
    ? "supported"
    : "outdated";
}

/**
 * How one read waits for its turn in the shell: `priority`, a read the user asked for when
 * unnamed, and `signal`, whose abort lets go of it if it hasn't run.
 */
export interface CodeHostReadScheduling {
  priority?: CodeHostReadPriority;
  signal?: AbortSignal;
}

/**
 * Whether this shell places `provider`'s reads by priority and can let go of a waiting one: its
 * query and cancel methods, and the priorities it advertises. Anything less gets today's reads.
 */
export async function desktopReadsByPriority(provider: CodeHostProviderId): Promise<boolean> {
  if (!hasCodeHostReadCancel()) return false;
  return readsByPriority(await desktopCodeHost(provider));
}

/**
 * `request` with its priority and a fresh `readId`, for a shell that advertises `readPriority`
 * and can cancel; an older or partial shell gets the request as it always did.
 */
function withReadPriority(
  request: CodeHostQueryRequest,
  entry: DesktopCodeHost,
  priority: CodeHostReadPriority = "interactive",
): CodeHostQueryRequest {
  if (!readsByPriority(entry)) return request;
  return { ...request, priority, readId: randomUUID() };
}

/** Whether this shell reads `provider` with every operation in `operations`. */
export async function isDesktopCodeHostAvailable(
  provider: CodeHostProviderId,
  operations: readonly string[],
): Promise<boolean> {
  const supported = await desktopCodeHostOperations(provider);
  return operations.every((operation) => supported.includes(operation));
}

/** The limit a partial answer met beside what it kept, as the shell named it; null without one. */
function partialRateLimit(
  data: unknown,
): { retryAt: number | null; limit: GitHubRateLimit | null } | null {
  const partial = (data as { partial?: { cause?: unknown; retryAt?: unknown; limit?: unknown } })
    ?.partial;
  if (!partial || partial.cause !== "rate_limited") return null;
  return {
    retryAt: typeof partial.retryAt === "number" ? partial.retryAt : null,
    limit: (partial.limit as GitHubRateLimit | undefined) ?? null,
  };
}

/**
 * Run one code-host read in the desktop shell, answering `unsupported` where the
 * shell doesn't offer that provider and operation.
 */
export async function queryCodeHost<Data>(
  request: CodeHostQueryRequest,
  scheduling: CodeHostReadScheduling = {},
): Promise<GitHubFastTrackResult<Data>> {
  // One look at what the shell offers serves both the operation check and the read's priority.
  const entry = await desktopCodeHost(request.provider);
  if (!operationsOf(entry).includes(request.operation)) {
    return {
      ok: false,
      error: { code: "unsupported", message: "This app can't read from that code host." },
    };
  }
  const { signal } = scheduling;
  // A read let go of before it's sent never reaches the shell.
  if (signal?.aborted) return { ok: false, error: CODE_HOST_READ_CANCELLED };
  // While GitHub limits this reader, the read waits it out here without reaching the shell.
  let cooldownKey = requestCooldownKey(request);
  // Not proven yet: find out who reads alongside the read, waiting on it only when a reader
  // on this host is held, as after switching back to an account still cooling down.
  const discovered = cooldownKey ? null : discoverRequestCooldownKey(request);
  if (discovered && githubCooldowns.holdsHost(request.host)) cooldownKey = await discovered;
  const refusal = cooldownKey ? githubCooldowns.refusal(cooldownKey) : null;
  if (refusal) return { ok: false, error: refusal };
  const result = await desktopCodeHostQuery<Data>(
    withReadPriority(request, entry, scheduling.priority),
    signal,
  );
  // Let go of before its turn: it never read, so it says nothing about the reader.
  if (!result.ok && result.error.code === "cancelled") return result;
  const settle = (key: string) => {
    // Polls as this reader slow down and pause by the hourly budget each answer reports.
    if (result.ok) githubReadBudgets.observe(key, result.budget);
    if (!result.ok && result.error.code === "rate_limited") {
      githubCooldowns.rateLimited(key, result.error.retryAt ?? null, result.error.limit ?? null);
      return;
    }
    // A partial answer kept what GitHub answered beside a limit: the reader waits it out too.
    const partial = result.ok ? partialRateLimit(result.data) : null;
    if (partial) githubCooldowns.rateLimited(key, partial.retryAt, partial.limit);
    else githubCooldowns.answered(key);
  };
  if (cooldownKey) settle(cooldownKey);
  else if (discovered) {
    void discovered.then((key) => {
      if (key) settle(key);
    });
  }
  return result;
}
