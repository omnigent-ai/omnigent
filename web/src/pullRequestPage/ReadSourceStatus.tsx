// The header's one line about where the PR is read, kept on every tab: a switch that
// failed, a chosen source that can't answer now, each with its one fix, or, on the Pull
// Requests page, that this app can't make changes as the account it reads the PR as.

import { Button } from "@/components/ui/button";
import { useMacReadAs } from "@/hooks/macReadAs";
import { useMacWriteDispatch } from "@/newPullRequestPage/hooks/usePullRequests";

import { repoAccountProblem } from "./repoAccountChoices";
import { useReadSource, useReadSourceFailure } from "./useReadSource";
import { useRepoAccount } from "./useRepoAccount";

export function ReadSourceStatus({
  prUrl,
  hostCanAnswer,
}: {
  prUrl: string;
  /** The session's machine can answer for this session now. */
  hostCanAnswer: boolean;
}) {
  const source = useReadSource(prUrl);
  const failure = useReadSourceFailure(prUrl);
  // Chosen for the session's machine, which can't answer, while this Mac could read it.
  const sessionAway =
    source.offered && source.choice === "session" && !hostCanAnswer && source.route.kind === "host";
  const readAs = useMacReadAs(source.route.kind === "mac" ? source.host : null);
  const dispatch = useMacWriteDispatch(prUrl);
  // Read as the page's own login on a shell that can't make changes as it: nothing is made.
  const needsUpdate =
    !!readAs &&
    dispatch.kind === "blocked" &&
    dispatch.unavailable.reason === "named_writes_need_update";
  // The repository's chosen login can't be used: nothing reads it as another account meanwhile.
  const repoAccount = useRepoAccount(prUrl);
  const repoProblem = repoAccount
    ? repoAccountProblem(repoAccount.state, repoAccount.signedOut)
    : null;
  const repoWaiting =
    repoAccount?.state.kind === "blocked" &&
    (repoAccount.state.reason === "pending" || repoAccount.state.reason === "checking");
  if (!failure && !sessionAway && !needsUpdate && !repoProblem) return null;
  return (
    <div
      role="status"
      className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-4 py-1.5 text-ui"
    >
      {failure && <span className="text-destructive">{failure}</span>}
      {repoProblem && repoAccount && (
        <>
          <span className="text-muted-foreground">{repoProblem}</span>
          {!repoWaiting && (
            <>
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0"
                onClick={() => repoAccount.choose({ mode: "automatic" })}
              >
                Use Automatic
              </Button>
              {source.offered && repoAccount.state.kind === "blocked" && (
                <Button
                  variant="link"
                  size="sm"
                  className="h-auto p-0"
                  onClick={() => source.choose("session")}
                >
                  Read on the session’s machine
                </Button>
              )}
            </>
          )}
        </>
      )}
      {needsUpdate && (
        <span className="text-muted-foreground">
          Update the app to comment, review and merge as @{readAs.login} here.
        </span>
      )}
      {sessionAway && (
        <>
          <span className="text-muted-foreground">The session’s machine can’t be reached.</span>
          <Button
            variant="link"
            size="sm"
            className="h-auto p-0"
            onClick={() => source.choose(source.automaticPick === "mac" ? "automatic" : "mac")}
          >
            Use this Mac
          </Button>
        </>
      )}
    </div>
  );
}
