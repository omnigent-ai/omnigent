// The Pulls panel's recovery when this Mac couldn't read a GitHub PR as its own
// account. Every action goes to the host of the read that failed.

import { useState } from "react";
import { KeyRoundIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useMacAccountChoice } from "@/hooks/useMacAccounts";
import { signInToGitHubOnThisMac } from "@/lib/codeHostSignIn";
import { DesktopPullRequestError } from "@/lib/desktopPullRequests";
import {
  macAccountChanged,
  readOnSessionMachine,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
import { chooseMacAccount, macAccountCommands } from "@/lib/macAccountCommands";
import { macReadRecovery } from "@/lib/macReadRecovery";
import { useGitHubSignInAvailability } from "@/shell/githubSignInCommand";

/** The recovery for `error`, or null when it isn't a Mac sign-in problem. */
export function MacReadRecoveryState({
  error,
  hostCanAnswer,
  onRetry,
  onUseSessionMachine,
  sessionMachineLabel = "Read with the session’s machine",
}: {
  error: unknown;
  hostCanAnswer: boolean;
  onRetry: () => void;
  /** What "use the session's machine" does, when it isn't reading one PR there. */
  onUseSessionMachine?: () => void;
  sessionMachineLabel?: string;
}) {
  useDesktopReadPathVersion();
  const host = error instanceof DesktopPullRequestError ? error.host : null;
  const choice = useMacAccountChoice(host);
  const [signingIn, setSigningIn] = useState(false);
  // The same check that shows "Sign in to GitHub on this Mac…" in the command palette.
  const signInAvailability = useGitHubSignInAvailability(false);
  const recovery = macReadRecovery(error, {
    signInAvailable: signInAvailability.available,
    choice,
    hostCanAnswer,
  });
  if (!recovery) return null;
  const url = error instanceof DesktopPullRequestError ? error.url : null;
  const inUse = choice?.effective?.login.toLowerCase();
  const accounts = choice
    ? macAccountCommands(recovery.host, choice)
        .slice(1)
        .filter(
          (command) =>
            command.intent.mode === "manual" && command.intent.login.toLowerCase() !== inUse,
        )
    : [];

  const signIn = async () => {
    setSigningIn(true);
    const result = await signInToGitHubOnThisMac({ host: recovery.host });
    setSigningIn(false);
    if (!result.ok) return;
    macAccountChanged("github", recovery.host);
    onRetry();
  };

  return (
    <div
      className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center"
      data-testid="mac-read-recovery"
    >
      <KeyRoundIcon aria-hidden className="size-8 text-muted-foreground/50" />
      <p className="text-ui font-medium text-foreground">{recovery.message}</p>
      {recovery.signIn && (
        <Button size="sm" disabled={signingIn} onClick={() => void signIn()}>
          {signingIn ? "Signing in…" : `Sign in to ${recovery.host}…`}
        </Button>
      )}
      {/* The app signs in only to hosts it can vouch for, so the Terminal steps always show. */}
      <p className="max-w-xs text-ui text-muted-foreground">
        {recovery.signIn ? "Or in Terminal, run " : "In Terminal, run "}
        <code className="font-mono">{recovery.terminalCommand}</code>, then try again.
      </p>
      <div className="flex flex-wrap justify-center gap-2">
        <Button size="sm" variant="outline" onClick={onRetry}>
          Try again
        </Button>
        {recovery.useAutomatic && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => void chooseMacAccount(recovery.host, { mode: "automatic" })}
          >
            Use gh’s active account
          </Button>
        )}
        {recovery.useSessionMachine && (onUseSessionMachine || url) && (
          <Button
            size="sm"
            variant="outline"
            onClick={() =>
              onUseSessionMachine ? onUseSessionMachine() : readOnSessionMachine(url!)
            }
          >
            {sessionMachineLabel}
          </Button>
        )}
      </div>
      {recovery.chooseAccount && accounts.length > 0 && (
        <div className="flex flex-col items-center gap-1">
          <p className="text-ui text-muted-foreground">Or read as another account on this Mac:</p>
          <div className="flex flex-wrap justify-center gap-2">
            {accounts.map((account) => (
              <Button
                key={account.id}
                size="sm"
                variant="ghost"
                onClick={() => void chooseMacAccount(recovery.host, account.intent)}
              >
                {account.label}
              </Button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
