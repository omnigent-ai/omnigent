// Which of this Mac's GitHub accounts a repository's pull requests are read and changed as,
// picked by the user and kept on this device per server and workspace user. A preference only:
// main still checks the login against gh's accounts on every read and change, and nothing here
// is sent to a session or a server. The read path combines it with what the shell can do.

import { z } from "zod";

import { createDeviceRecentList } from "@/lib/deviceRecentList";
import { durableServerScope } from "@/lib/durableServerScope";
import type { MacNamedReader } from "@/lib/macNamedReads";
import { pullRequestPageUrl } from "@/lib/pullRequestReadRoute";

/** The hosts a repository's account can be picked for; github.com for now. */
export const REPO_ACCOUNT_HOSTS: readonly string[] = ["github.com"];

const LOGIN_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;
/** How many repositories keep a choice; the one used longest ago goes first. */
export const MAX_REPO_ACCOUNTS = 200;

/** A repository on a GitHub host, as a pull request's canonical URL names it. */
export interface PullRequestRepository {
  host: string;
  owner: string;
  repo: string;
}

/** What the user chose for a repository: Automatic, or one of this Mac's logins by name. */
export type RepoAccountIntent = { mode: "automatic" } | { mode: "account"; login: string };

/**
 * What's saved for a repository: `pending` while the workspace user isn't known and this device
 * keeps choices, so nothing reads before learning which account it should use. A server with no
 * stable identity keeps no choices, so its repositories are on Automatic.
 */
export type SavedRepoAccount = { kind: "pending" } | { kind: "saved"; intent: RepoAccountIntent };

/** Why a repository with a chosen login can't be read or changed as it right now. */
export type RepoReaderBlockedReason =
  /** The workspace user isn't known yet, so their choice can't be read. */
  | "pending"
  /** The shell hasn't said yet whether it can read as a named login. */
  | "checking"
  /** Reading as a named login is turned off. */
  | "turned_off"
  /** This app can't read as a named login. */
  | "update_needed";

/**
 * Who a pull request's repository is read as: `none` where the repository has no choice that
 * applies, `named` for its chosen login, or `blocked` where that login can't be used now.
 * A blocked repository is read and changed by nobody, never by another account.
 */
export type RepoReaderState =
  | { kind: "none" }
  | { kind: "named"; reader: MacNamedReader }
  | { kind: "blocked"; reason: RepoReaderBlockedReason; reader: MacNamedReader | null };

export const NO_REPO_READER: RepoReaderState = { kind: "none" };

const AUTOMATIC: RepoAccountIntent = { mode: "automatic" };

const accounts = createDeviceRecentList({
  storagePrefix: "omnigent:pull-request-repo-account",
  field: "accounts",
  maxItems: 1,
  maxScopes: MAX_REPO_ACCOUNTS,
  item: z.object({ login: z.string().regex(LOGIN_PATTERN) }),
  same: (left, right) => left.login.toLowerCase() === right.login.toLowerCase(),
  // A choice is the user's until they change it: never dropped by another user or server.
  retention: "every-owner",
});

/** The repository a pull request URL names, on a host a choice can be made for; else null. */
export function pullRequestRepository(
  url: string | null | undefined,
): PullRequestRepository | null {
  const page = pullRequestPageUrl(url);
  if (!page) return null;
  const { hostname, pathname } = new URL(page);
  if (!REPO_ACCOUNT_HOSTS.includes(hostname)) return null;
  const [, owner, repo] = pathname.split("/");
  return owner && repo ? { host: hostname, owner, repo } : null;
}

/** A repository's key, the same for every spelling: GitHub names don't differ by case alone. */
export function repositoryKey({ host, owner, repo }: PullRequestRepository): string {
  return `${host}/${owner}/${repo}`.toLowerCase();
}

const scopeOf = ({ host, owner, repo }: PullRequestRepository) => [host, owner, repo];

/** The choice saved for `repository`, for this server and workspace user. */
export function savedRepoAccount(repository: PullRequestRepository): SavedRepoAccount {
  // An embedded server with no stable identity never gets one, so no choice is kept for it.
  if (durableServerScope() === null) return { kind: "saved", intent: AUTOMATIC };
  // Unknown whose choices apply: wait, unless this device keeps none for anyone.
  if (accounts.owner() === null) {
    return accounts.hasSaved() ? { kind: "pending" } : { kind: "saved", intent: AUTOMATIC };
  }
  const [saved] = accounts.read(scopeOf(repository));
  return { kind: "saved", intent: saved ? { mode: "account", login: saved.login } : AUTOMATIC };
}

/**
 * Save the choice for `repository`; false where it couldn't be kept, e.g. no user yet. Where the
 * server keeps no choices, Automatic is already in effect and a login can't be kept.
 */
export function setRepoAccount(
  repository: PullRequestRepository,
  intent: RepoAccountIntent,
): boolean {
  if (durableServerScope() === null) return intent.mode === "automatic";
  if (accounts.owner() === null) return false;
  if (intent.mode === "account" && !LOGIN_PATTERN.test(intent.login)) return false;
  accounts.write(scopeOf(repository), intent.mode === "account" ? [{ login: intent.login }] : []);
  const saved = savedRepoAccount(repository);
  return (
    saved.kind === "saved" &&
    (intent.mode === "automatic"
      ? saved.intent.mode === "automatic"
      : saved.intent.mode === "account" &&
        saved.intent.login.toLowerCase() === intent.login.toLowerCase())
  );
}

/** Call `listener` after any repository's choice changes, here or in another window. */
export function subscribeRepoAccounts(listener: () => void): () => void {
  return accounts.subscribe(listener);
}

/** Forget subscriptions, so a test starts clean. */
export function resetRepoAccountsForTests(): void {
  accounts.resetForTests();
}
