import { githubFastTrackQuery, hasGitHubFastTrackBridge } from "@/lib/githubFastTrackBridge";
import {
  getDesktopFeatures,
  type GitHubFastTrackAccount,
  type GitHubFastTrackError,
  type GitHubFastTrackResult,
} from "@/lib/nativeBridge";

/**
 * The GitHub fast track: on macOS with Apple silicon, the desktop shell runs a
 * bundled `gh` for a closed set of reads. Features built on it stay hidden
 * wherever it's unavailable, so every other client keeps the host-only path.
 */

/** What a feature built on the fast track can do for one account. */
export type GitHubFastTrackState =
  | { status: "unsupported" }
  | { status: "checking" }
  | { status: "ready"; account: GitHubFastTrackAccount }
  | { status: "unavailable"; error: GitHubFastTrackError };

const UNSUPPORTED: GitHubFastTrackResult<never> = {
  ok: false,
  error: { code: "unsupported", message: "The GitHub fast track isn't available in this app." },
};

/** The operation every fast-track shell runs, including the first ones. */
const BASELINE_OPERATIONS = ["account_status"];

/**
 * Whether this client offers the fast track with every operation in `operations`:
 * a shell with the bridge method that reports `githubFastTrack` and lists the
 * operations. The first fast-track shells list none and run only `account_status`.
 */
export async function isGitHubFastTrackAvailable(
  operations: readonly string[] = [],
): Promise<boolean> {
  if (!hasGitHubFastTrackBridge()) return false;
  const features = await getDesktopFeatures();
  if (features?.githubFastTrack !== true) return false;
  const supported = Array.isArray(features.githubFastTrackOperations)
    ? features.githubFastTrackOperations
    : BASELINE_OPERATIONS;
  return operations.every((operation) => supported.includes(operation));
}

/**
 * Whether this shell runs `operation` on the fast track: `outdated` when it predates
 * the operation, `unavailable` when the fast track is off (no bundled `gh`, another
 * platform, or turned off by policy), which no update of the app fixes on its own.
 */
export async function githubFastTrackSupport(
  operation: string,
): Promise<"supported" | "outdated" | "unavailable"> {
  if (!hasGitHubFastTrackBridge()) return "outdated";
  const features = await getDesktopFeatures();
  if (features?.githubFastTrack !== true) return "unavailable";
  const supported = Array.isArray(features.githubFastTrackOperations)
    ? features.githubFastTrackOperations
    : BASELINE_OPERATIONS;
  return supported.includes(operation) ? "supported" : "outdated";
}

/**
 * The account a PR's fast-track reads run as: exactly the login the session's host
 * reports for it. Without one there is none, so a local account is never guessed.
 */
export function pullRequestFastTrackAccount(input: {
  provider: string | null | undefined;
  prHost: string | null | undefined;
  selectedAccount: string | null | undefined;
}): GitHubFastTrackAccount | null {
  if (input.provider !== "github" || !input.prHost || !input.selectedAccount) return null;
  return { host: input.prHost.toLowerCase(), login: input.selectedAccount };
}

/** Run one fast-track read, answering `unsupported` where the fast track is off. */
export async function queryGitHubFastTrack<Data>(
  operation: string,
  account: GitHubFastTrackAccount,
  params?: unknown,
): Promise<GitHubFastTrackResult<Data>> {
  if (!(await isGitHubFastTrackAvailable([operation]))) return UNSUPPORTED;
  return githubFastTrackQuery<Data>(operation, account, params);
}

/**
 * Check that the shell runs every operation in `operations` and that the account
 * is signed in to its `gh`, as a fast-track state.
 */
export async function checkGitHubFastTrackAccount(
  account: GitHubFastTrackAccount | null,
  operations: readonly string[] = [],
): Promise<Exclude<GitHubFastTrackState, { status: "checking" }>> {
  if (!account || !(await isGitHubFastTrackAvailable(operations))) {
    return { status: "unsupported" };
  }
  const result = await queryGitHubFastTrack<{ login: string }>("account_status", account);
  if (result.ok) return { status: "ready", account: { ...account, login: result.data.login } };
  return result.error.code === "unsupported"
    ? { status: "unsupported" }
    : { status: "unavailable", error: result.error };
}
