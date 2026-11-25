// Sending label changes to one PR: what its labels will be once every unanswered change lands,
// and one send path, with its announcements, for the Labels section and ⌘K alike.

import { useLayoutEffect, useMemo, useRef } from "react";

import { announceChange } from "@/components/pendingChange/announceChange";
import { useMacReadAs } from "@/hooks/macReadAs";
import {
  applyPullRequestLabelChange,
  changeShownToCurrentAccount,
  pullRequestLabelsMutationKey,
  usePendingPullRequestChanges,
  useUpdatePullRequestLabels,
  type LabelChangeAccount,
  type PullRequestLabel,
  type PullRequestLabelChange,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  draftOwner,
  macWriteInFlightFor,
  useMacWriteInFlight,
  useMacWritesInFlightVersion,
  useWorkspaceDraftOwner,
} from "@/lib/desktopPullRequestWrites";
import { sameNamedReader } from "@/lib/macNamedReads";

/** An edit the provider has not answered yet: adding a label or removing it. */
export type LabelEdit = "adding" | "removing";

/** Why a label can't change yet: the last change sent from this Mac is still going. */
export const LABEL_CHANGE_WAITING_REASON = "Wait for the last label change to finish";

/** GitHub label names are case-insensitive. */
export const labelKey = (label: Pick<PullRequestLabel, "name">) => label.name.toLowerCase();

/** `the label a`, `the labels a and b`, `the labels a, b and c`. */
function labelsText(labels: readonly Pick<PullRequestLabel, "name">[]): string {
  const names = labels.map((label) => label.name);
  if (names.length <= 1) return `the label ${names.join("")}`;
  return `the labels ${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/** What a refused label change says it couldn't do. */
export function labelFailurePrefix(change: Pick<PullRequestLabelChange, "add" | "remove">): string {
  const { add, remove } = change;
  if (add.length > 0 && remove.length > 0) return "Couldn’t change the labels";
  if (remove.length > 0) return `Couldn’t remove ${labelsText(remove)}`;
  if (add.length > 0) return `Couldn’t add ${labelsText(add)}`;
  return "Couldn’t change the labels";
}

/** What a label change is doing or has done, in words. */
export function labelChangeText(
  change: Pick<PullRequestLabelChange, "add" | "remove">,
  phase: "sending" | "done",
): string {
  const parts: string[] = [];
  if (change.add.length > 0) {
    parts.push(`${phase === "done" ? "added" : "adding"} ${labelsText(change.add)}`);
  }
  if (change.remove.length > 0) {
    parts.push(`${phase === "done" ? "removed" : "removing"} ${labelsText(change.remove)}`);
  }
  const text = parts.join(" and ");
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}${phase === "done" ? "." : "…"}`;
}

const NO_LABELS: readonly PullRequestLabel[] = [];

/**
 * Whether a label change to a PR is still going from this Mac, for callers checking many PRs;
 * a new checker each time a write starts or ends, so memos built on it re-run.
 */
export function useLabelChangesInFlight(): {
  version: number;
  waiting: (prUrl: string) => boolean;
} {
  const version = useMacWritesInFlightVersion();
  return useMemo(
    () => ({
      version,
      waiting: (prUrl: string) => macWriteInFlightFor(prUrl, "pull_request_labels_update"),
    }),
    [version],
  );
}

/**
 * Whether a label change, as sent or cached, belongs to the account in use now (`now`): made by
 * the same workspace user as the page reads as and, for one sent from this Mac, as the GitHub
 * account still in use. A change no one stamped belongs to no one.
 */
export function labelChangeIsCurrent(change: unknown, now: LabelChangeAccount): boolean {
  const madeBy = (change as Pick<PullRequestLabelChange, "madeBy"> | undefined)?.madeBy;
  return (
    !!madeBy &&
    madeBy.owner === now.owner &&
    sameNamedReader(madeBy.reader, now.reader) &&
    changeShownToCurrentAccount(change)
  );
}

/**
 * Label changes to `prUrl`, sent as `conversationId`'s: the unanswered ones, the labels once
 * they land, and `send` for a new one. `prUrl` null sends nothing.
 */
export function usePullRequestLabelChanges(
  conversationId: string,
  prUrl: string | null,
  labels: readonly PullRequestLabel[] | undefined,
) {
  const update = useUpdatePullRequestLabels(conversationId);
  const pending = usePendingPullRequestChanges<PullRequestLabelChange>(
    pullRequestLabelsMutationKey(conversationId),
  );
  // A label change on this Mac, from any session, lands before the next; the host queues them.
  const waiting = useMacWriteInFlight(prUrl, "pull_request_labels_update");
  const shownLabels = labels ?? NO_LABELS;
  const owner = useWorkspaceDraftOwner();
  const reader = useMacReadAs();
  // Only the account in use now sees its own unanswered changes.
  const changes = useMemo(
    () =>
      prUrl
        ? pending.filter(
            (change) => change?.prUrl === prUrl && labelChangeIsCurrent(change, { owner, reader }),
          )
        : [],
    [pending, prUrl, owner, reader],
  );
  // The labels once every unanswered edit lands, so a toggle undoes the last one.
  const applied = useMemo(
    () =>
      new Set(
        changes
          .reduce<PullRequestLabel[]>(applyPullRequestLabelChange, [...shownLabels])
          .map(labelKey),
      ),
    [changes, shownLabels],
  );
  // The page's reader as of its latest commit, for a result that comes back after a switch; once
  // this sender has gone, no one it knows of is still reading, so its results show nowhere.
  const readerNow = useRef(reader);
  const mounted = useRef(true);
  useLayoutEffect(() => {
    readerNow.current = reader;
  }, [reader]);
  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const stillTheirs = (sent: unknown) =>
    mounted.current &&
    labelChangeIsCurrent(sent, { owner: draftOwner(), reader: readerNow.current });
  /** Send `change` afresh: a toggle, or Retry after a refusal, each with its own route and ticket. */
  const send = (change: PullRequestLabelChange) => {
    if (change.add.length === 0 && change.remove.length === 0) return;
    const stamped = { ...change, madeBy: { owner: draftOwner(), reader: readerNow.current } };
    announceChange(labelChangeText(change, "sending"));
    update.mutate(stamped, {
      onSuccess: (sent) => {
        // Answered after a switch of workspace user, page reader or GitHub account: not theirs.
        if (!stillTheirs(sent ?? stamped)) return;
        announceChange(labelChangeText(change, "done"));
      },
      onError: (error, sent) => {
        // Answered after a switch of workspace user, page reader or GitHub account: not theirs.
        if (!stillTheirs(sent)) return;
        announceChange(`${labelFailurePrefix(change)}: ${error.message}`);
      },
    });
  };
  return { changes, applied, waiting, send };
}
