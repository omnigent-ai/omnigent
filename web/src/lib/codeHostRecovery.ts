// What the Pull Requests panel can truthfully say when the host's GitHub CLI
// can't reach the repo, from the fields the host already sends. Each state
// claims only what the payload proves: an empty account list can also mean the
// check failed, so it is never read as "signed out".

import type {
  NormalizedPullRequestInfo,
  PullRequestAccount,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { gitProviderCopy } from "@/lib/gitProviders";
import { namedCodeHost } from "@/lib/codeHosts";

/** The GitHub provider's general guidance, kept for hosts too old to say more. */
const GITHUB_COPY = gitProviderCopy("github");

/** How gh's own check of one account went, as `gh auth status` reported it. */
export type AccountCheck = "passed" | "failed" | "timed-out" | "unchecked";

export interface RecoveryAccount {
  login: string;
  host: string | null;
  check: AccountCheck;
}

export type CodeHostRecovery =
  /** gh reports no working sign-in and lists no accounts, or its check failed. */
  | { kind: "not-signed-in-or-unchecked"; signInHost: string | null }
  /** gh lists accounts, but none passed its check. */
  | { kind: "sign-in-not-working"; accounts: RecoveryAccount[]; signInHost: string | null }
  /** Every account's check timed out: GitHub didn't answer the host in time. */
  | { kind: "check-timed-out" }
  /**
   * An account works, but the repository didn't open. The host doesn't say why,
   * so missing access and a network or GitHub failure read the same.
   */
  | { kind: "repo-check-failed"; accounts: RecoveryAccount[]; canPickAccount: boolean }
  /** Signed in, but gh didn't say which accounts it has (an older gh). */
  | { kind: "repo-unreachable" }
  /** A host too old to list accounts; it may not even know whether gh is signed in. */
  | { kind: "unknown" };

export type UnsupportedRemoteRecovery =
  /** A registered named host whose pull requests this view can't show; `label` names it. */
  | { kind: "named-host-remote"; host: string; label: string }
  | { kind: "other-host-remote"; host: string }
  | { kind: "unknown-remote" };

const HOST =
  /^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;
const GITHUB_DOT_COM = "github.com";

function accountCheck(state: string | null): AccountCheck {
  if (state === "success") return "passed";
  if (state === "error") return "failed";
  if (state === "timeout") return "timed-out";
  return "unchecked";
}

function recoveryAccount(account: PullRequestAccount): RecoveryAccount {
  return { login: account.login, host: account.host, check: accountCheck(account.state) };
}

/** The lower-cased host of an https URL, or null. */
function httpsHost(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    return parsed.protocol === "https:" && HOST.test(host) ? host : null;
  } catch {
    return null;
  }
}

/**
 * The host to sign in to, from evidence about the repo only: the selected PR's
 * URL, or the host every tracked PR shares. Account hosts aren't evidence, since
 * a github.com repo can sit next to an Enterprise-only sign-in.
 */
export function signInHostEvidence(info: NormalizedPullRequestInfo): string | null {
  const selected = httpsHost(info.selected_pr_url);
  if (selected) return selected;
  const hosts = new Set((info.prs ?? []).map((pr) => httpsHost(pr.url)));
  if (hosts.size !== 1) return null;
  return [...hosts][0];
}

/**
 * The recovery state for a GitHub workspace whose repo didn't resolve, or null
 * when the repo resolved or another provider serves the workspace (it keeps its
 * own guidance).
 */
export function githubRecovery(info: NormalizedPullRequestInfo): CodeHostRecovery | null {
  if (info.provider !== "github") return null;
  const { auth } = info;
  if (auth.authenticated && info.repo?.name_with_owner) return null;
  const accounts = auth.accounts;
  if (accounts === null) {
    // Older hosts count a missing `authenticated` as signed in, so this proves nothing more.
    return auth.authenticated
      ? { kind: "unknown" }
      : { kind: "not-signed-in-or-unchecked", signInHost: signInHostEvidence(info) };
  }
  if (accounts.length === 0) {
    return auth.authenticated
      ? { kind: "repo-unreachable" }
      : { kind: "not-signed-in-or-unchecked", signInHost: signInHostEvidence(info) };
  }
  const listed = accounts.map(recoveryAccount);
  const working = listed.filter((account) => account.check === "passed");
  if (working.length === 0) {
    if (listed.every((account) => account.check === "timed-out"))
      return { kind: "check-timed-out" };
    return { kind: "sign-in-not-working", accounts: listed, signInHost: signInHostEvidence(info) };
  }
  // The panel's account selector offers every listed account where the provider can switch.
  const canPickAccount = info.capabilities.account_switching && accounts.length > 1;
  return { kind: "repo-check-failed", accounts: working, canPickAccount };
}

/** What a remote no provider claims says about where its pull requests live. */
export function unsupportedRemoteRecovery(
  remoteHost: string | undefined,
): UnsupportedRemoteRecovery {
  const host = remoteHost?.toLowerCase();
  if (!host || !HOST.test(host)) return { kind: "unknown-remote" };
  const named = namedCodeHost(host);
  if (named) return { kind: "named-host-remote", host, label: named.copy.label };
  return { kind: "other-host-remote", host };
}

/** The sign-in command for a host, naming it only when it isn't github.com. */
export function githubSignInCommand(host: string | null): string {
  return host && host !== GITHUB_DOT_COM ? `gh auth login --hostname ${host}` : "gh auth login";
}

/** How an account's check reads beside its login. */
export function accountCheckLabel(check: AccountCheck): string {
  switch (check) {
    case "passed":
      return "signed in";
    case "failed":
      return "failed gh’s sign-in check";
    case "timed-out":
      return "sign-in check timed out";
    case "unchecked":
      return "sign-in couldn’t be checked";
  }
}

/**
 * Where the panel runs. Desktop reads GitHub with the computer's own account,
 * so it never sends people to sign in on the session's host; web and mobile do.
 */
export type RecoverySurface = { kind: "web" } | { kind: "desktop"; device: string };

const SSO_HINT = "For an organization with SSO, authorize the account for that organization.";
const REPO_CHECK_FAILED_TITLE = "Couldn’t open this repository";
const CHECK_TIMED_OUT = {
  title: "Checking GitHub timed out",
  hint: "The session’s host didn’t hear back from GitHub in time. Check again in a moment.",
};

/** Missing access and a failed request look the same from the host, so the copy names both. */
function repoCheckFailedHint(subject: string, canPickAccount: boolean): string {
  const next = canPickAccount ? "Check again, or pick another account." : "Check again.";
  return `${subject} is signed in to GitHub but couldn’t open this repository. The account may not have access, or GitHub didn’t answer. ${next} ${SSO_HINT}`;
}

/** The copy for a recovery state; `backticked` text renders as code. */
export function githubRecoveryCopy(
  recovery: CodeHostRecovery,
  surface: RecoverySurface = { kind: "web" },
): { title: string; hint: string } {
  if (surface.kind === "desktop") return desktopRecoveryCopy(recovery, surface.device);
  return webRecoveryCopy(recovery);
}

function desktopRecoveryCopy(
  recovery: CodeHostRecovery,
  device: string,
): { title: string; hint: string } {
  const notYet = `Omnigent can’t load pull requests with ${device}’s GitHub account yet.`;
  switch (recovery.kind) {
    case "not-signed-in-or-unchecked":
      return {
        title: "Can’t load this session’s pull requests",
        hint: `The GitHub CLI on the session’s host has no working sign-in, or Omnigent couldn’t check it. ${notYet}`,
      };
    case "sign-in-not-working":
      return {
        title: "GitHub sign-in on the session’s host isn’t working",
        hint: notYet,
      };
    case "check-timed-out":
      return CHECK_TIMED_OUT;
    case "repo-check-failed":
      return {
        title: REPO_CHECK_FAILED_TITLE,
        hint: repoCheckFailedHint("The session’s host", recovery.canPickAccount),
      };
    case "repo-unreachable":
      return {
        title: "Can’t reach this repository",
        hint: `The GitHub CLI on the session’s host is signed in but couldn’t open this repository. ${notYet}`,
      };
    case "unknown":
      return {
        title: "Can’t reach this repository",
        hint: `The session’s host couldn’t open this repository. ${notYet}`,
      };
  }
}

function webRecoveryCopy(recovery: CodeHostRecovery): { title: string; hint: string } {
  switch (recovery.kind) {
    case "not-signed-in-or-unchecked":
      return {
        title: "Sign in to GitHub on the host",
        hint: `The GitHub CLI on the session’s host has no working sign-in, or Omnigent couldn’t check it. Run \`${githubSignInCommand(recovery.signInHost)}\` on the host, then check again.`,
      };
    case "sign-in-not-working":
      return {
        title: "GitHub sign-in on the host isn’t working",
        hint: `Run \`${githubSignInCommand(recovery.signInHost)}\` on the host to sign in again, or \`gh auth status\` to see what failed.`,
      };
    case "check-timed-out":
      return CHECK_TIMED_OUT;
    case "repo-check-failed":
      return {
        title: REPO_CHECK_FAILED_TITLE,
        hint: recovery.canPickAccount
          ? repoCheckFailedHint("The session’s host", true)
          : `${repoCheckFailedHint("The session’s host", false)} To add another account, run \`gh auth login\` on the host.`,
      };
    case "repo-unreachable":
      return {
        title: "Can’t reach this repository",
        hint: "The GitHub CLI on the host is signed in but couldn’t open this repository. Run `gh auth status` on the host to see which account it uses.",
      };
    case "unknown":
      return {
        title: "Can’t reach the upstream repo",
        hint: GITHUB_COPY.repoUnresolvedHint ?? GITHUB_COPY.authHint,
      };
  }
}
