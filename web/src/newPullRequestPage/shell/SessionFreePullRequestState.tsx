// What a session-free pull request view shows for a PR no session of it can read: a plain
// sentence, and a way to the session that linked it when one is known. Never an error.

import { GitPullRequestIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { SourceSession } from "@/pullRequestList/subAgentPullRequests";
import { useOpenSessionPage } from "@/shell/OpenSessionPageContext";

export const SESSION_FREE_PULL_REQUEST_TITLE = "This pull request isn’t linked to a session here.";

export function SessionFreePullRequestState({
  linkingSession,
}: {
  /** The session that linked the PR, when one is known. */
  linkingSession: SourceSession | null | undefined;
}) {
  const openSessionPage = useOpenSessionPage();
  return (
    <div
      role="status"
      className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center"
    >
      <GitPullRequestIcon aria-hidden className="size-8 text-muted-foreground/50" />
      <p className="text-ui text-foreground">{SESSION_FREE_PULL_REQUEST_TITLE}</p>
      {linkingSession && (
        <Button variant="link" size="sm" onClick={() => openSessionPage(linkingSession.id)}>
          Open {linkingSession.name}
        </Button>
      )}
    </div>
  );
}
