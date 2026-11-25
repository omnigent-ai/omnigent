// The Diffs view's Commits tab: the branch's own commits, newest first, with a
// filter, filling the pane. Choosing one shows what that commit changed.

import { CheckIcon, Loader2Icon } from "lucide-react";
import { useEffect, useRef } from "react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  type BranchCommit,
  type BranchCommitsView,
  MAXIMUM_BRANCH_COMMITS,
} from "@/lib/branchCommits";
import { shortCommit } from "@/lib/customComparison";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";

export const COMMITS_FILTER_LABEL = "Filter commits";
/** ⌘K's command that opens the Commits tab on its filter. */
export const SHOW_A_COMMIT_LABEL = "Show a Commit…";

export function BranchCommitList({
  view,
  retry,
  shown,
  onChoose,
}: {
  view: BranchCommitsView;
  retry: () => void;
  /** The commit on screen, if the comparison shows one on its own. */
  shown: string | null;
  onChoose: (commit: BranchCommit) => void;
}) {
  const section = useRef<HTMLDivElement>(null);
  const listed = view.kind === "listed" && view.commits.length > 0;
  // Asked for while still listing: the filter takes focus once it's there, but only if the reader
  // is still on the list, so a late answer never pulls focus from elsewhere.
  useEffect(() => {
    const element = section.current;
    if (!listed || !element || document.activeElement !== element) return;
    element.querySelector<HTMLElement>("[data-commits-filter]")?.focus();
  }, [listed]);
  return (
    <div
      ref={section}
      // Holds focus while the commits list, so their status is read and the filter can take it.
      tabIndex={-1}
      className="flex min-h-0 flex-1 flex-col gap-1 p-2 outline-none"
      data-testid="changes-review-commits"
    >
      <SectionBody view={view} retry={retry} shown={shown} onChoose={onChoose} />
    </div>
  );
}

function SectionBody({
  view,
  retry,
  shown,
  onChoose,
}: {
  view: BranchCommitsView;
  retry: () => void;
  shown: string | null;
  onChoose: (commit: BranchCommit) => void;
}) {
  if (view.kind === "loading") {
    return (
      <p role="status" className="flex items-center gap-2 px-2 text-xs text-muted-foreground">
        <Loader2Icon aria-hidden="true" className="size-3 animate-spin" />
        Listing this branch's commits…
      </p>
    );
  }
  if (view.kind === "unavailable") {
    return (
      <div className="flex items-center gap-2 px-2">
        <p className="min-w-0 flex-1 text-xs text-muted-foreground">{view.message}</p>
        {view.canRetry && (
          <Button type="button" variant="ghost" size="xs" onClick={retry}>
            Try Again
          </Button>
        )}
      </div>
    );
  }
  const base = view.baseDisplay || "the default branch";
  if (view.commits.length === 0) {
    return (
      <p className="px-2 text-xs text-muted-foreground">
        {`This branch has no commits of its own since it left ${base}.`}
      </p>
    );
  }
  return (
    <Command
      // cmdk names its input by this label.
      label={COMMITS_FILTER_LABEL}
      className="min-h-0 flex-1 gap-1 bg-transparent"
      // The whole row's text is matched, so a sha, subject or author finds it.
      filter={(value, search) =>
        value.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()) ? 1 : 0
      }
    >
      <CommandInput placeholder={COMMITS_FILTER_LABEL} data-commits-filter="" />
      <CommandList className="max-h-none min-h-0 flex-1">
        <CommandEmpty className="py-3 text-xs text-muted-foreground">
          No commits match.
        </CommandEmpty>
        <CommandGroup className="p-0">
          {view.commits.map((commit) => {
            const active = commit.oid === shown;
            return (
              <CommandItem
                key={commit.oid}
                value={`${commit.oid} ${commit.subject} ${commit.authorName}`}
                onSelect={() => onChoose(commit)}
                aria-current={active || undefined}
                data-branch-commit={commit.oid}
                className="items-start"
              >
                <CheckIcon
                  aria-hidden="true"
                  className={cn("mt-0.5 size-3.5 shrink-0", !active && "invisible")}
                />
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="truncate text-ui text-foreground">
                    {commit.subject || shortCommit(commit.oid)}
                  </span>
                  <span className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
                    <span className="shrink-0 font-mono">{shortCommit(commit.oid)}</span>
                    {commit.authorName && <span className="truncate">{commit.authorName}</span>}
                    {commit.authoredAtMs !== null && (
                      <time
                        dateTime={new Date(commit.authoredAtMs).toISOString()}
                        className="shrink-0"
                      >
                        {relativeTime(commit.authoredAtMs)}
                      </time>
                    )}
                  </span>
                </span>
              </CommandItem>
            );
          })}
        </CommandGroup>
      </CommandList>
      {view.truncated && (
        <p className="px-2 text-xs text-muted-foreground">
          {`Showing the newest ${MAXIMUM_BRANCH_COMMITS} commits since this branch left ${base}.`}
        </p>
      )}
    </Command>
  );
}
