// The Changes tab row naming the PR a stacked PR builds on, with its PR-only toggle.

import { LayersIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { PullRequestStack, PullRequestStackParent } from "@/hooks/usePullRequests";

/** The stack lookup's parent; a failed lookup, even with older data, leaves only the full diff. */
export function stackParentOf(
  stack: { data?: PullRequestStack | null; error: unknown } | undefined,
): PullRequestStackParent | null {
  return stack?.error ? null : (stack?.data?.parent ?? null);
}

/** Whether the parent offers a PR-only view; a PR on its parent's branch already shows one. */
export function canShowOwnChanges(parent: PullRequestStackParent | null | undefined): boolean {
  return !!parent && parent.source !== "base_branch";
}

function ParentLabel({ parent }: { parent: PullRequestStackParent }) {
  if (parent.number === null) {
    return <span>{`Stacked on ${parent.head_sha.slice(0, 7)}`}</span>;
  }
  const number = `#${parent.number}`;
  return (
    <span className="min-w-0 truncate">
      {"Stacked on "}
      {parent.url?.startsWith("https://") ? (
        <a
          href={parent.url}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-foreground hover:underline"
        >
          {number}
        </a>
      ) : (
        <span className="font-medium text-foreground">{number}</span>
      )}
      {parent.title && ` ${parent.title}`}
      {parent.state === "MERGED" && " (merged)"}
    </span>
  );
}

export function PullRequestStackBar({
  parent,
  ownChangesOnly,
  onOwnChangesOnlyChange,
}: {
  parent: PullRequestStackParent;
  ownChangesOnly: boolean;
  onOwnChangesOnlyChange: (ownChangesOnly: boolean) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Stacked pull request"
      className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-3 py-1 text-ui text-muted-foreground"
    >
      <LayersIcon className="size-3.5 shrink-0" aria-hidden />
      <ParentLabel parent={parent} />
      {canShowOwnChanges(parent) && (
        <>
          <Button
            variant={ownChangesOnly ? "secondary" : "ghost"}
            size="xs"
            className="ml-auto"
            aria-pressed={ownChangesOnly}
            onClick={() => onOwnChangesOnlyChange(!ownChangesOnly)}
          >
            This PR only
          </Button>
          {ownChangesOnly && (
            <p className="w-full text-xs">
              Showing changes since the parent's head. Review conversations and viewed marks are in
              the full diff.
            </p>
          )}
        </>
      )}
    </div>
  );
}
