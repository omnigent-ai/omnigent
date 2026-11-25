// Which code host serves a pull request link, and the desktop shell's reads
// from it as this Mac's own identity. Hosts are only recognized from evidence:
// github.com and registered named hosts by name, a GitHub Enterprise host only when
// the caller already knows it serves GitHub.

import {
  discoverRequestCooldownKey,
  githubCooldowns,
  requestCooldownKey,
} from "@/lib/githubCooldown";
import { githubReadBudgets } from "@/lib/githubReadBudget";
import { pullRequestIntegrations } from "@/lib/pullRequestIntegrations";
import { randomUUID } from "@/lib/randomUUID";
import {
  CODE_HOST_READ_CANCELLED,
  desktopCodeHostQuery,
  getDesktopFeatures,
  hasCodeHostBridge,
  hasCodeHostReadCancel,
  type CodeHostQueryRequest,
  type CodeHostReadPriority,
  type GitHubFastTrackResult,
  type GitHubRateLimit,
} from "@/lib/nativeBridge";

/** A code host the desktop shell can read from as this Mac's own identity. */
export type CodeHostProviderId = "github" | "gitspoke" | "narnia";

const GITHUB_DOT_COM = "github.com";
const PULL_PATH = /^\/([^/]+)\/([^/]+)\/pull\/(\d+)\/?$/;

/** The provider a registered named code host says serves `host`, or null. */
function namedHostProvider(host: string): CodeHostProviderId | null {
  const name = host.toLowerCase();
  return (
    pullRequestIntegrations().namedCodeHosts.find((named) => named.matchesHost(name))?.provider ??
    null
  );
}

/** Whether `host` is a GitSpoke server, which only a registered named host says. */
export function isGitSpokeHost(host: string): boolean {
  return namedHostProvider(host) === "gitspoke";
}

/**
 * The provider serving `host`: registered named hosts and github.com by name, any host
 * in `githubHosts` as GitHub Enterprise, otherwise null.
 */
export function codeHostProvider(
  host: string,
  githubHosts: Iterable<string> = [],
): CodeHostProviderId | null {
  const name = host.toLowerCase();
  const named = namedHostProvider(name);
  if (named) return named;
  if (name === GITHUB_DOT_COM) return "github";
  for (const known of githubHosts) {
    if (known.toLowerCase() === name) return "github";
  }
  return null;
}

/** A pull request link on a recognized code host. */
export interface CodeHostPullRequest {
  provider: CodeHostProviderId;
  host: string;
  /** `owner/repo`. */
  repository: string;
  number: number;
  /** The link without query, fragment, or trailing path. */
  url: string;
  /**
   * GitHub, read on this Mac: one of its logins the Pull Requests page reads as by name,
   * for the reads main allows that way; absent everywhere else.
   */
  readAs?: string;
}

/**
 * The provider, host, repository, and number in an https pull request link on
 * a recognized code host, or null. Hosts are matched as in `codeHostProvider`.
 */
export function parseCodeHostPullRequestUrl(
  url: string | null | undefined,
  githubHosts: Iterable<string> = [],
): CodeHostPullRequest | null {
  if (!url) return null;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  // A port or credentials would make this a different server than the one recognized.
  if (parsed.protocol !== "https:" || parsed.port || parsed.username || parsed.password) {
    return null;
  }
  const match = PULL_PATH.exec(parsed.pathname);
  if (!match) return null;
  const host = parsed.hostname.toLowerCase();
  const provider = codeHostProvider(host, githubHosts);
  if (!provider) return null;
  const [, owner, repo, number] = match;
  return {
    provider,
    host,
    repository: `${owner}/${repo}`,
    number: Number(number),
    url: `https://${host}/${owner}/${repo}/pull/${number}`,
  };
}

async function desktopCodeHost(provider: CodeHostProviderId) {
  if (!hasCodeHostBridge()) return undefined;
  const features = await getDesktopFeatures();
  const hosts = Array.isArray(features?.codeHosts) ? features.codeHosts : [];
  return hosts.find((candidate) => candidate?.id === provider);
}

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
  const entry = await desktopCodeHost(provider);
  return Array.isArray(entry?.operations) ? entry.operations : [];
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
 * `request` with its priority and a fresh `readId`, for a shell that advertises `readPriority`
 * and can cancel; an older or partial shell gets the request as it always did.
 */
/**
 * Whether this shell places `provider`'s reads by priority and can let go of a waiting one: its
 * query and cancel methods, and the priorities it advertises. Anything less gets today's reads.
 */
export async function desktopReadsByPriority(provider: CodeHostProviderId): Promise<boolean> {
  if (!hasCodeHostReadCancel()) return false;
  return (await desktopCodeHost(provider))?.readPriority === 1;
}

async function withReadPriority(
  request: CodeHostQueryRequest,
  priority: CodeHostReadPriority = "interactive",
): Promise<CodeHostQueryRequest> {
  if (!(await desktopReadsByPriority(request.provider))) return request;
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
  if (!(await isDesktopCodeHostAvailable(request.provider, [request.operation]))) {
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
    await withReadPriority(request, scheduling.priority),
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
