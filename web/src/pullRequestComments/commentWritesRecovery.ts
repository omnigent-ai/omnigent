// What a pull request's comment box says, and offers, when it can't post: why, in words the
// user can act on, and only the actions that can work on this shell. The draft stays editable.

import { githubSignInCommand } from "@/lib/codeHostRecovery";
import { gitProviderCopy, type GitProviderCopy } from "@/lib/gitProviders";
import type {
  MacWriteUnavailable,
  MacWriteUnavailableReason,
} from "@/lib/desktopPullRequestWrites";
import { MAC_SIGN_IN_CODES } from "@/lib/macReadRecovery";

/** Why the box can't post, as the diagnostics log names it. */
export type CommentWritesReason =
  | MacWriteUnavailableReason
  | "host_cannot_comment"
  | "signed_out"
  | "account_missing"
  | "account_invalid";

/** The provider as the panel names it, from its shared copy. */
export type CommentWritesProvider = Pick<GitProviderCopy, "id" | "label" | "requestName">;

/** Why the box can't post before anything is sent. */
export type CommentWritesBlock =
  /** Read on this Mac, which can't post here, and the session's machine can't either. */
  | { kind: "mac"; unavailable: MacWriteUnavailable }
  /** Read through the session's machine, which can't post here. */
  | { kind: "host"; provider: CommentWritesProvider; canReadOnMac: boolean };

export interface CommentWritesRecovery {
  reason: CommentWritesReason;
  message: string;
  /** Whether posting waits on this; a refused post can be sent again. */
  blocksPosting: boolean;
  /** The host the app can sign in to itself; null where this shell can't. */
  signInHost: string | null;
  /** What to run in Terminal to sign in, for every GitHub sign-in problem. */
  terminalCommand: string | null;
  /** Turn "Make changes as this Mac's account" back to Automatic. */
  turnOnMacWrites: boolean;
  /** Ask the shell and the account again, for problems that can clear by themselves. */
  checkAgain: boolean;
  /** Read the PR on this Mac again, after the user sent it to the session's machine. */
  readOnThisMac: boolean;
  openLabel: string;
}

const COMMENT_ON_GITHUB = "Comment on GitHub instead.";

/** "Open on GitLab", from the provider's own name; a provider the panel can't name opens in the browser. */
function openLabel(provider: CommentWritesProvider): string {
  return provider.id ? `Open on ${provider.label}` : "Open in Browser";
}

function macCopy(reason: MacWriteUnavailableReason, host: string, login: string | null): string {
  // Read as the Pull Requests page's own login: the comment goes as it, or not at all.
  if (login) {
    switch (reason) {
      case "named_writes_need_update":
        return `Update the app to comment as @${login} here. Your draft stays here.`;
      case "account_checking":
        return `Checking @${login}’s sign-in on this Mac…`;
      case "no_account":
        return `Sign in to ${host} as @${login} on this Mac to comment. Your draft stays here.`;
      case "named_reads_off":
        return `Commenting as @${login} isn’t available right now. ${COMMENT_ON_GITHUB}`;
      case "named_not_read_here":
        return `Read this pull request on this Mac to comment as @${login}. Your draft stays here.`;
      case "writes_off":
        return `Changes as this Mac’s accounts are turned off, so this can’t be posted as @${login}.`;
      default:
        break;
    }
  }
  switch (reason) {
    case "provider_unsupported":
      return "Comments on GitSpoke pull requests can’t be posted from this Mac yet. Comment on GitSpoke instead.";
    case "release_off":
      return `Commenting from this Mac isn’t turned on in this version of the app. ${COMMENT_ON_GITHUB}`;
    case "writes_off":
      return "Changes as this Mac’s account are turned off, and the session’s machine can’t post here.";
    case "shell_too_old":
      return `This version of the app can’t post comments from this Mac. Update the app, or comment on GitHub.`;
    case "shell_writes_unavailable":
      return "This Mac isn’t posting comments right now. Your organization may have turned it off, or the app’s GitHub tools aren’t available. Check again, or comment on GitHub.";
    case "page_unsupported":
      return `This page can’t post comments from this Mac. ${COMMENT_ON_GITHUB}`;
    case "account_checking":
      return `Checking the ${host} account on this Mac…`;
    case "no_account":
      return `Sign in to ${host} on this Mac to comment. Your draft stays here.`;
    case "named_writes_need_update":
      return `Update the app to comment from this Mac. Your draft stays here.`;
    case "named_reads_off":
      return `Commenting from this Mac isn’t available right now. ${COMMENT_ON_GITHUB}`;
    case "named_not_read_here":
      return `Read this pull request on this Mac to comment. Your draft stays here.`;
  }
}

/**
 * The box's recovery: why it can't post now comes first, since posting must wait on it, while
 * an earlier refusal's own words stay beside the draft. A refusal leads only when nothing
 * blocks a retry. Null when the box can post.
 */
export function commentWritesRecovery({
  block,
  refusal,
  signInAvailable,
}: {
  block: CommentWritesBlock | null;
  /** The last post's refusal: its code and the PR host it was sent to on this Mac. */
  refusal: { code: string; host: string } | null;
  signInAvailable: boolean;
}): CommentWritesRecovery | null {
  if (!block && refusal && MAC_SIGN_IN_CODES.has(refusal.code)) {
    return {
      reason: refusal.code as CommentWritesReason,
      message: `Sign in to ${refusal.host} again, then post your draft.`,
      blocksPosting: false,
      signInHost: signInAvailable ? refusal.host : null,
      terminalCommand: githubSignInCommand(refusal.host),
      turnOnMacWrites: false,
      checkAgain: false,
      readOnThisMac: false,
      openLabel: openLabel(gitProviderCopy("github")),
    };
  }
  if (!block) return null;
  if (block.kind === "host") {
    return {
      reason: "host_cannot_comment",
      message: block.canReadOnMac
        ? `The session’s machine can’t post comments on this ${block.provider.requestName}. Read it on this Mac to comment as this Mac’s account.`
        : `The session’s machine can’t post comments on this ${block.provider.requestName}.`,
      blocksPosting: true,
      signInHost: null,
      terminalCommand: null,
      turnOnMacWrites: false,
      checkAgain: false,
      readOnThisMac: block.canReadOnMac,
      openLabel: openLabel(block.provider),
    };
  }
  const { reason, target, reader } = block.unavailable;
  const github = target.provider === "github";
  return {
    reason,
    message: macCopy(reason, target.host, reader?.login ?? null),
    blocksPosting: true,
    signInHost: reason === "no_account" && signInAvailable ? target.host : null,
    terminalCommand: reason === "no_account" && github ? githubSignInCommand(target.host) : null,
    turnOnMacWrites: reason === "writes_off",
    checkAgain: reason === "no_account" || reason === "shell_writes_unavailable",
    readOnThisMac: false,
    openLabel: openLabel(gitProviderCopy(target.provider)),
  };
}
