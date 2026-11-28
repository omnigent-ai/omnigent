// What the Pulls panel shows when the host's lookup didn't settle the branch's pull
// request: which of several it is, that the user said none, or that the lookup
// failed. The same choices show in ⌘K from one definition.

import { useMemo } from "react";
import { GitPullRequestIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { BranchDiscoveryNotice } from "@/hooks/useBranchPullRequest";
import {
  type BranchChoiceCommand,
  type BranchDiscovery,
  branchChoiceCommands,
  chooseBranchPullRequest,
} from "@/lib/branchPullRequest";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

/** The notice for `notice`, or nothing while the host is still looking. */
export function BranchPullRequestNotice({
  notice,
  onChanged,
}: {
  notice: BranchDiscoveryNotice | undefined;
  /** Read again after a failure. */
  onChanged: () => void;
}) {
  const discovery =
    notice && "discovery" in notice ? (notice.discovery as BranchDiscovery) : undefined;
  const commands = useMemo(() => (discovery ? branchChoiceCommands(discovery) : []), [discovery]);
  const choose = (command: BranchChoiceCommand) => {
    if (!discovery) return;
    chooseBranchPullRequest(discovery.choiceKey, command.choice);
  };
  usePaletteCommands(
    "branch-pull-request-choice",
    "Pull requests",
    useMemo<PaletteCommand[]>(
      () =>
        commands.map((command) => ({
          id: command.id,
          label: command.paletteLabel,
          icon: GitPullRequestIcon,
          keywords: ["branch", "pull request", "choose", "which"],
          run: () => choose(command),
        })),
      // `choose` reads only the discovery, which `commands` already depends on.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [commands],
    ),
  );
  if (!notice || notice.status === "searching") return null;

  if (notice.status === "failed") {
    return (
      <section
        aria-label="This branch’s pull request"
        className="flex shrink-0 flex-col gap-2 border-b border-border p-3 text-ui"
      >
        <p className="text-muted-foreground">
          {notice.error instanceof Error && notice.error.message
            ? notice.error.message
            : "Couldn’t look for this branch’s pull request."}
        </p>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={onChanged}>
            Try again
          </Button>
        </div>
      </section>
    );
  }

  if (notice.status === "none-chosen") {
    return (
      <section
        aria-label="This branch’s pull request"
        className="flex shrink-0 items-center gap-2 border-b border-border p-3 text-ui text-muted-foreground"
      >
        <span>No pull request for this branch.</span>
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
  return (
    <section
      aria-labelledby="branch-pr-question"
      className="flex shrink-0 flex-col gap-2 border-b border-border p-3 text-ui"
    >
      <h3 id="branch-pr-question" className="font-medium text-foreground">
        Which pull request is this branch?
      </h3>
      <p className="text-muted-foreground">
        {incomplete
          ? "These are the newest pull requests for this branch; older ones aren’t listed."
          : "More than one pull request matches this branch."}
      </p>
      <p className="text-muted-foreground">
        This app won’t remember your choice after it restarts.
      </p>
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
    </section>
  );
}
