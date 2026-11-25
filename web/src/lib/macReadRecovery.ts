// What the Pulls panel offers when this Mac couldn't read a GitHub PR as its own
// account: sign in (where the app can), the Terminal steps, choosing another
// account, going back to Automatic, or reading through the session's machine.
// Every action targets the host of the read that failed, which can differ from
// the session's own repository host.

import { githubSignInCommand } from "@/lib/codeHostRecovery";
import { DesktopPullRequestError } from "@/lib/desktopPullRequests";
import type { MacAccountChoice } from "@/lib/nativeBridge";

/** Mac refusals a sign-in or account choice could fix. */
export const MAC_SIGN_IN_CODES: ReadonlySet<string> = new Set([
  "signed_out",
  "account_missing",
  "account_invalid",
]);

export interface MacReadRecovery {
  host: string;
  code: string;
  /** The shell's own message, naming the host. */
  message: string;
  /** The app can sign in itself; otherwise only the Terminal steps show. */
  signIn: boolean;
  /** What to run in Terminal, e.g. `gh auth login --hostname ghe.example.com`. */
  terminalCommand: string;
  /** Another signed-in account could read it instead. */
  chooseAccount: boolean;
  /** A chosen account is set; Automatic would follow gh's active one. */
  useAutomatic: boolean;
  /** The session's machine can read it instead. */
  useSessionMachine: boolean;
}

/** Whether `error` is a Mac GitHub read a sign-in or account choice could fix. */
export function isMacSignInError(error: unknown): error is DesktopPullRequestError {
  return (
    error instanceof DesktopPullRequestError &&
    error.provider === "github" &&
    !!error.host &&
    MAC_SIGN_IN_CODES.has(error.code)
  );
}

/**
 * The recovery for a failed Mac read, or null when `error` isn't one a sign-in or
 * account choice can fix (those show the shell's message as is).
 */
export function macReadRecovery(
  error: unknown,
  {
    signInAvailable,
    choice,
    hostCanAnswer,
  }: {
    signInAvailable: boolean;
    /** The failing host's accounts on this Mac, if known. */
    choice: MacAccountChoice | null;
    hostCanAnswer: boolean;
  },
): MacReadRecovery | null {
  if (!isMacSignInError(error) || !error.host) return null;
  // Accounts that could read it instead: signed in, and not the one that just failed.
  const inUse = choice?.effective?.login.toLowerCase();
  const working =
    choice?.accounts.filter(
      (account) => account.state === "success" && account.login.toLowerCase() !== inUse,
    ) ?? [];
  const manual = choice?.intent.mode === "manual";
  return {
    host: error.host,
    code: error.code,
    message: error.message,
    signIn: signInAvailable,
    terminalCommand: githubSignInCommand(error.host),
    chooseAccount: working.length > 0,
    useAutomatic: manual,
    useSessionMachine: hostCanAnswer,
  };
}
