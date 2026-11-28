// Sending label changes to one PR: what its labels will be once every unanswered change lands,
// and one send path, with its announcements, for the Labels section and ⌘K alike.

import { useLayoutEffect, useMemo, useRef } from "react";

import { announceChange } from "@/components/pendingChange/announceChange";
import {
  applyPullRequestLabelChange,
  pullRequestLabelsMutationKey,
  usePendingPullRequestChanges,
  useUpdatePullRequestLabels,
  type LabelChangeAccount,
  type PullRequestLabel,
  type PullRequestLabelChange,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { draftOwner, useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";

/** An edit the provider has not answered yet: adding a label or removing it. */
export type LabelEdit = "adding" | "removing";

/** Why a label can't change yet: the last change is still going. */
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
 * Whether a label change, as sent or cached, belongs to the account in use now (`now`): made by
 * the same workspace user. A change no one stamped belongs to no one.
 */
export function labelChangeIsCurrent(change: unknown, now: LabelChangeAccount): boolean {
  const madeBy = (change as Pick<PullRequestLabelChange, "madeBy"> | undefined)?.madeBy;
  return !!madeBy && madeBy.owner === now.owner;
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
  // The host queues label changes, so a new one never waits on the last.
  const waiting = false;
  const shownLabels = labels ?? NO_LABELS;
  const owner = useWorkspaceDraftOwner();
  // Only the account in use now sees its own unanswered changes.
  const changes = useMemo(
    () =>
      prUrl
        ? pending.filter(
            (change) => change?.prUrl === prUrl && labelChangeIsCurrent(change, { owner }),
          )
        : [],
    [pending, prUrl, owner],
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
  // Once this sender has gone, no one it knows of is still reading, so its results show nowhere.
  const mounted = useRef(true);
  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const stillTheirs = (sent: unknown) =>
    mounted.current && labelChangeIsCurrent(sent, { owner: draftOwner() });
  /** Send `change` afresh: a toggle, or Retry after a refusal, each with its own route and ticket. */
  const send = (change: PullRequestLabelChange) => {
    if (change.add.length === 0 && change.remove.length === 0) return;
    const stamped = { ...change, madeBy: { owner: draftOwner() } };
    announceChange(labelChangeText(change, "sending"));
    update.mutate(stamped, {
      onSuccess: (sent) => {
        // Answered after a switch of workspace user or GitHub account: not theirs.
        if (!stillTheirs(sent ?? stamped)) return;
        announceChange(labelChangeText(change, "done"));
      },
      // A failure is said by its notice, an alert, so it isn't announced twice.
    });
  };
  return { changes, applied, waiting, send };
}
