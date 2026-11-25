// The comment box's notice when it can't post: why, and what fixes it here. Signing in reads
// the PR's host again, so the box enables itself without a reload, with the draft as it was.

import { useMacReadAs } from "@/hooks/macReadAs";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  macCouldWriteOnceReadHere,
  macWriteUnavailable,
  recheckMacWrites,
  useMacWriteRouteVersion,
  writeMacWritesIntent,
} from "@/lib/desktopPullRequestWrites";
import { signInToGitHubOnThisMac } from "@/lib/codeHostSignIn";
import {
  macAccountChanged,
  readOnThisMacAgain,
  readsOnSessionMachine,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
import { reportPullRequestCommentWrites } from "@/lib/nativeBridge";
import type { PullRequestCommentDraft } from "@/newPullRequestPage/hooks/usePullRequests";
import { useGitHubSignInAvailability } from "@/shell/githubSignInCommand";
import {
  commentWritesRecovery,
  type CommentWritesBlock,
  type CommentWritesProvider,
  type CommentWritesRecovery,
} from "./commentWritesRecovery";

/** Why the box can't post, decided again whenever the read path, flags or account change. */
function useCommentWritesBlock(
  prUrl: string,
  blocked: boolean,
  provider: CommentWritesProvider,
): CommentWritesBlock | null {
  useMacWriteRouteVersion();
  useDesktopReadPathVersion();
  const readAs = useMacReadAs();
  if (!blocked) return null;
  const unavailable = macWriteUnavailable(prUrl, readAs);
  if (unavailable) return { kind: "mac", unavailable };
  return {
    kind: "host",
    provider,
    canReadOnMac: readsOnSessionMachine(prUrl) && macCouldWriteOnceReadHere(prUrl, readAs),
  };
}

/**
 * Records each reason once it shows, and that it cleared while the box stayed open. A check
 * of the account under way is a moment, not a reason, so it's left out.
 */
function useCommentWritesReport(shownReason: string | null): void {
  const reason = shownReason === "account_checking" ? null : shownReason;
  const shown = useRef<string | null>(null);
  useEffect(() => {
    const before = shown.current;
    if (before === reason) return;
    if (before) reportPullRequestCommentWrites({ reason: before, outcome: "resolved" });
    if (reason) reportPullRequestCommentWrites({ reason, outcome: "shown" });
    shown.current = reason;
  }, [reason]);
}

/**
 * The box's recovery: whether posting waits on it and what it says. `blocked` is the panel's
 * own verdict that neither this Mac nor the session's machine can post here now.
 */
export function useCommentWritesRecovery({
  prUrl,
  blocked,
  provider,
  draft,
}: {
  prUrl: string;
  blocked: boolean;
  provider: CommentWritesProvider;
  draft: Pick<PullRequestCommentDraft, "refusal" | "pending">;
}): CommentWritesRecovery | null {
  const block = useCommentWritesBlock(prUrl, blocked, provider);
  // The same check that shows "Sign in to GitHub on this Mac…" in ⌘K.
  const signIn = useGitHubSignInAvailability(false);
  const recovery = commentWritesRecovery({
    block,
    refusal: draft.pending ? null : (draft.refusal ?? null),
    signInAvailable: signIn.available,
  });
  useCommentWritesReport(recovery?.reason ?? null);
  return recovery;
}

export function PullRequestCommentWritesNotice({
  recovery,
  refusal = null,
  prUrl,
  onSignedIn,
}: {
  recovery: CommentWritesRecovery;
  /** What this Mac said when it refused the post, shown first as the box's alert. */
  refusal?: string | null;
  prUrl: string;
  /** After a sign-in this Mac confirmed: e.g. forget the refusal it fixed. */
  onSignedIn?: () => void;
}) {
  const [signingIn, setSigningIn] = useState(false);
  const readAs = useMacReadAs();
  const { reason } = recovery;
  const signIn = async (host: string) => {
    setSigningIn(true);
    reportPullRequestCommentWrites({ reason, outcome: "sign_in_started" });
    const result = await signInToGitHubOnThisMac({ host });
    setSigningIn(false);
    reportPullRequestCommentWrites({
      reason,
      outcome: result.ok ? "sign_in_succeeded" : "sign_in_failed",
    });
    if (!result.ok) return;
    // Read the host again as the new account; the box enables once it answers.
    macAccountChanged("github", host);
    onSignedIn?.();
  };
  return (
    <div
      role={refusal ? "alert" : "status"}
      data-testid="pull-request-comment-writes-notice"
      className="flex shrink-0 flex-col gap-1 rounded-md border border-border bg-muted/40 px-2.5 py-1.5 text-ui"
    >
      {refusal && <p className="min-w-0 text-destructive">{refusal}</p>}
      <p className="min-w-0 text-foreground">{recovery.message}</p>
      {recovery.terminalCommand && (
        <p className="min-w-0 text-muted-foreground">
          {recovery.signInHost ? "Or in Terminal, run " : "In Terminal, run "}
          <code className="font-mono">{recovery.terminalCommand}</code>
          {recovery.checkAgain ? ", then check again." : ", then post again."}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {recovery.signInHost && (
          <Button
            type="button"
            size="sm"
            disabled={signingIn}
            onClick={() => void signIn(recovery.signInHost!)}
          >
            {signingIn ? "Signing in…" : `Sign in to ${recovery.signInHost}…`}
          </Button>
        )}
        {recovery.turnOnMacWrites && (
          <Button
            type="button"
            size="sm"
            onClick={() => {
              reportPullRequestCommentWrites({ reason, outcome: "turned_on" });
              writeMacWritesIntent("automatic");
            }}
          >
            Make changes as this Mac’s account
          </Button>
        )}
        {recovery.readOnThisMac && (
          <Button type="button" size="sm" onClick={() => readOnThisMacAgain(prUrl)}>
            Read on this Mac
          </Button>
        )}
        {recovery.checkAgain && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              reportPullRequestCommentWrites({ reason, outcome: "checked_again" });
              recheckMacWrites(prUrl, readAs);
            }}
          >
            Check again
          </Button>
        )}
        <Button asChild variant="link" size="sm" className="h-auto p-0">
          <a href={prUrl} target="_blank" rel="noreferrer">
            {recovery.openLabel}
          </a>
        </Button>
      </div>
    </div>
  );
}
