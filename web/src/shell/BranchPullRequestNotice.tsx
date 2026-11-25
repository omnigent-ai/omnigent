// What the Pulls panel shows when this Mac couldn't settle the branch's pull
// request on its own: which of several it is, that the user said none, or why
// it couldn't look. The same choices show in ⌘K from one definition.

import { useMemo, useState } from "react";
import { GitPullRequestIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { BranchDiscoveryNotice } from "@/hooks/useBranchPullRequest";
import {
  type BranchChoiceCommand,
  type BranchDiscovery,
  branchChoiceCommands,
  choiceSubject,
  chooseBranchPullRequest,
} from "@/lib/branchPullRequest";
import {
  findBranchOnSessionMachine,
  findBranchOnThisMacAgain,
  findsBranchOnSessionMachine,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
import { isMacSignInError } from "@/lib/macReadRecovery";
import { MacReadRecoveryState } from "@/shell/MacReadRecoveryState";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

const SESSION_MACHINE_LABEL = "Use the session’s machine instead";

/** The notice for `notice`, or nothing while the Mac is still looking. */
export function BranchPullRequestNotice({
  sessionId,
  notice,
  hostCanAnswer,
  onChanged,
}: {
  sessionId: string;
  notice: BranchDiscoveryNotice | undefined;
  /** The session's machine is reachable, so it can find the branch's PR instead. */
  hostCanAnswer: boolean;
  /** Read again after a choice or a retry. */
  onChanged: () => void;
}) {
  const discovery =
    notice && "discovery" in notice ? (notice.discovery as BranchDiscovery) : undefined;
  const commands = useMemo(() => (discovery ? branchChoiceCommands(discovery) : []), [discovery]);
  const [failed, setFailed] = useState(false);
  const choose = (command: BranchChoiceCommand) => {
    if (!discovery?.choiceKey) return;
    setFailed(false);
    void chooseBranchPullRequest(discovery.choiceKey, command.choice).then((saved) => {
      if (saved) onChanged();
      else setFailed(true);
    });
  };
  usePaletteCommands(
    "branch-pull-request-choice",
    "Pull requests",
    useMemo<PaletteCommand[]>(
      () =>
        discovery?.choiceKey
          ? commands.map((command) => ({
              id: command.id,
              label: command.paletteLabel,
              icon: GitPullRequestIcon,
              keywords: ["branch", "pull request", "choose", "which"],
              run: () => choose(command),
            }))
          : [],
      // `choose` reads only the discovery, which `commands` already depends on.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [commands, discovery?.choiceKey],
    ),
  );
  useDesktopReadPathVersion();
  if (findsBranchOnSessionMachine(sessionId)) {
    return (
      <p className="flex shrink-0 items-center gap-2 border-b border-border p-2 text-xs text-muted-foreground">
        The session’s machine finds this branch’s pull request.
        <Button
          variant="link"
          className="h-auto p-0 text-xs"
          onClick={() => {
            findBranchOnThisMacAgain(sessionId);
            onChanged();
          }}
        >
          Find it on this Mac
        </Button>
      </p>
    );
  }
  if (!notice || notice.status === "searching") return null;
  const useSessionMachine = hostCanAnswer
    ? () => {
        findBranchOnSessionMachine(sessionId);
        onChanged();
      }
    : undefined;

  if (notice.status === "failed") {
    return (
      <section aria-label="This branch’s pull request" className="shrink-0 border-b border-border">
        <MacReadRecoveryState
          error={notice.error}
          hostCanAnswer={hostCanAnswer}
          onRetry={onChanged}
          onUseSessionMachine={useSessionMachine}
          sessionMachineLabel={SESSION_MACHINE_LABEL}
        />
        <GenericFailure
          error={notice.error}
          onRetry={onChanged}
          onUseSessionMachine={useSessionMachine}
        />
      </section>
    );
  }

  if (notice.status === "incomplete") {
    return (
      <section
        aria-label="This branch’s pull request"
        className="flex shrink-0 flex-col gap-2 border-b border-border p-3 text-ui"
      >
        <p className="text-muted-foreground">
          This Mac couldn’t check every repository for this branch’s pull request.
        </p>
        <Actions onRetry={onChanged} onUseSessionMachine={useSessionMachine} />
      </section>
    );
  }

  if (notice.status === "none-chosen") {
    return (
      <section
        aria-label="This branch’s pull request"
        className="flex shrink-0 items-center gap-2 border-b border-border p-3 text-ui text-muted-foreground"
      >
        <span>No pull request for this {choiceSubject(notice.discovery)}.</span>
        {commands.map((command) => (
          <Button
            key={command.id}
            variant="link"
            className="h-auto p-0 text-ui"
            onClick={() => choose(command)}
          >
            {command.label}
          </Button>
        ))}
      </section>
    );
  }

  const ambiguous = notice.discovery.result;
  const incomplete = ambiguous.status === "ambiguous" && ambiguous.incomplete;
  const subject = choiceSubject(notice.discovery);
  return (
    <section
      aria-labelledby="branch-pr-question"
      className="flex shrink-0 flex-col gap-2 border-b border-border p-3 text-ui"
    >
      <h3 id="branch-pr-question" className="font-medium text-foreground">
        Which pull request is this {subject}?
      </h3>
      <p className="text-muted-foreground">
        {incomplete
          ? "Some repositories couldn’t be checked, so this may not be every pull request."
          : `More than one pull request matches this ${subject}.`}
      </p>
      {!notice.discovery.remembered && (
        <p className="text-muted-foreground">
          This app won’t remember your choice after it restarts.
        </p>
      )}
      <ul className="flex flex-col gap-1">
        {commands.map((command) => (
          <li key={command.id}>
            <Button
              variant={command.choice && "url" in command.choice ? "outline" : "ghost"}
              size="sm"
              className="w-full justify-start truncate"
              onClick={() => choose(command)}
            >
              {command.label}
            </Button>
          </li>
        ))}
      </ul>
      {failed && (
        <p role="status" className="text-destructive">
          Couldn’t save your choice. Try again.
        </p>
      )}
    </section>
  );
}

function Actions({
  onRetry,
  onUseSessionMachine,
}: {
  onRetry: () => void;
  onUseSessionMachine?: () => void;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="outline" onClick={onRetry}>
        Try again
      </Button>
      {onUseSessionMachine && (
        <Button size="sm" variant="outline" onClick={onUseSessionMachine}>
          {SESSION_MACHINE_LABEL}
        </Button>
      )}
    </div>
  );
}

/** A failure that isn't about this Mac's sign-in, which the recovery above handles. */
function GenericFailure({
  error,
  onRetry,
  onUseSessionMachine,
}: {
  error: unknown;
  onRetry: () => void;
  onUseSessionMachine?: () => void;
}) {
  if (isMacSignInError(error)) return null;
  return (
    <div className="flex flex-col gap-2 p-3 text-ui">
      <p className="text-muted-foreground">
        {error instanceof Error && error.message
          ? error.message
          : "This Mac couldn’t look for this branch’s pull request."}
      </p>
      <Actions onRetry={onRetry} onUseSessionMachine={onUseSessionMachine} />
    </div>
  );
}
