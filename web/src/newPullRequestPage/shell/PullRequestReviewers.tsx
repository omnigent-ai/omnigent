// The Summary tab's Reviewers section: each reviewer's review state and, for editors,
// a picker that requests reviews once it closes. An edit shows as in progress until the
// provider answers.

import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import { announceChange } from "@/components/pendingChange/announceChange";
import { useChangeStatus } from "@/components/pendingChange/changeStatus";
import {
  ChangeFailedNotice,
  pendingChangeClassName,
  PendingChangeIcon,
} from "@/components/pendingChange/PendingChange";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  BellRingIcon,
  CheckIcon,
  CircleDotIcon,
  FileDiffIcon,
  MessageSquareIcon,
  PlusIcon,
  RotateCwIcon,
  UsersIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useTakenControlRequest } from "@/pullRequestPage/pullRequestDestination";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  type ReviewerPingPullRequest,
  type ReviewerPingAction,
  useReviewerPingActions,
} from "@/hooks/useReviewerPings";
import { isCommandAvailable, isCommandListed } from "@/store/paletteCommands";
import { cn } from "@/lib/utils";
import { mergePeople, preparePeople } from "@/lib/peopleSearch";
import {
  applyPullRequestReviewerChange,
  pullRequestReviewerKey,
  pullRequestReviewersMutationKey,
  usePendingPullRequestChanges,
  useUpdatePullRequestReviewers,
  type PullRequestReviewer,
  type PullRequestReviewerChange,
  type PullRequestReviewerTarget,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { PullRequestChipAction, PullRequestSectionHeader } from "../../shell/PullRequestSection";
import { SECTION_PICKER_TRIGGER_CLASS, useSectionPicker } from "@/shell/pullRequestSectionStyle";
import {
  REVIEWER_EDIT_LABEL,
  type ReviewerEdit,
  ReviewerPickerList,
  reviewerSubject,
  typedReviewer,
  useReviewerPeopleSource,
} from "../../shell/ReviewerPickerList";
import { usePickerDraft } from "@/hooks/usePickerDraft";
import { pickerDraftSummary, pickerDraftTargets } from "@/lib/pickerDraft";

const REVIEWER_DRAFT_WORDS = {
  add: "to request",
  remove: "to withdraw",
  sends: "sent when you close",
};

export const REVIEWER_STATE_META: Record<
  PullRequestReviewer["state"],
  { label: string; className: string; Icon: LucideIcon }
> = {
  PENDING: { label: "Awaiting review", className: "text-status-yellow", Icon: CircleDotIcon },
  APPROVED: { label: "Approved", className: "text-status-green", Icon: CheckIcon },
  COMMENTED: { label: "Commented", className: "text-muted-foreground", Icon: MessageSquareIcon },
  CHANGES_REQUESTED: {
    label: "Changes requested",
    className: "text-status-red",
    Icon: FileDiffIcon,
  },
  DISMISSED: { label: "Commented", className: "text-muted-foreground", Icon: MessageSquareIcon },
};

/** `a`, `a and b`, `a, b and c`. */
function subjects(targets: readonly PullRequestReviewerTarget[]): string {
  const names = targets.map(reviewerSubject);
  return names.length <= 1
    ? names.join("")
    : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/** What a reviewer change is doing, has done, or couldn't do, in words. */
function reviewerChangeText(
  change: Pick<PullRequestReviewerChange, "add" | "remove">,
  phase: "sending" | "done" | "failed",
): string {
  const { add, remove } = change;
  if (phase === "failed") {
    if (add.length > 0 && remove.length > 0) return "Couldn’t change the reviewers";
    if (remove.length > 0) return `Couldn’t withdraw the review request from ${subjects(remove)}`;
    if (add.length > 0) return `Couldn’t request a review from ${subjects(add)}`;
    return "Couldn’t change the reviewers";
  }
  const parts: string[] = [];
  if (add.length > 0) {
    parts.push(`${phase === "done" ? "requested" : "requesting"} a review from ${subjects(add)}`);
  }
  if (remove.length > 0) {
    parts.push(
      `${phase === "done" ? "withdrew" : "withdrawing"} the review request from ${subjects(remove)}`,
    );
  }
  const sentence = parts.join("; ");
  return `${sentence.charAt(0).toUpperCase()}${sentence.slice(1)}${phase === "done" ? "." : "…"}`;
}

const reviewerFailurePrefix = (change: Pick<PullRequestReviewerChange, "add" | "remove">) =>
  reviewerChangeText(change, "failed");

/** One reviewer: the login, then its row actions and review-state glyph at the end. */
function PullRequestReviewerRow({
  reviewer,
  edit,
  onToggle,
  ping,
}: {
  reviewer: PullRequestReviewer;
  edit?: ReviewerEdit;
  onToggle?: () => void;
  /** A ping for this reviewer, where a build's integration offers one. */
  ping?: ReviewerPingAction;
}) {
  const state = REVIEWER_STATE_META[reviewer.state];
  const stateLabel = edit ? REVIEWER_EDIT_LABEL[edit] : state.label;
  const subject = reviewerSubject(reviewer);
  const pending = reviewer.state === "PENDING";

  return (
    <li
      aria-label={`${subject}: ${stateLabel}`}
      aria-busy={edit ? true : undefined}
      className={cn(
        "group flex min-w-0 items-center gap-1.5 py-0.5 text-xs",
        edit && pendingChangeClassName(edit === "withdrawing" ? "removing" : "changing"),
      )}
    >
      {reviewer.kind === "team" && (
        <UsersIcon aria-hidden className="size-3 shrink-0 text-muted-foreground" />
      )}
      <span className="min-w-0 flex-1 truncate text-foreground" title={reviewer.login}>
        {reviewer.login}
      </span>
      {ping && !edit && isCommandListed(ping.command.availability) && (
        // Revealed like the row's other action; greyed out with its reason while it can't run.
        <span className="opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 pointer-coarse:opacity-100">
          <PullRequestChipAction
            label={ping.command.menuLabel}
            title={
              ping.command.availability.available
                ? ping.command.menuLabel
                : ping.command.availability.reason
            }
            icon={BellRingIcon}
            disabled={!isCommandAvailable(ping.command.availability)}
            onClick={ping.run}
          />
        </span>
      )}
      {onToggle && !edit && (
        // Revealed on hover or focus, and always on touch, like the PR list's row actions.
        <span className="opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 pointer-coarse:opacity-100">
          <PullRequestChipAction
            label={
              pending
                ? `Withdraw review request from ${subject}`
                : `Re-request review from ${subject}`
            }
            title={pending ? "Withdraw review request" : "Re-request review"}
            icon={pending ? XIcon : RotateCwIcon}
            onClick={onToggle}
          />
        </span>
      )}
      <span title={stateLabel} className="flex shrink-0 items-center">
        {edit ? (
          <PendingChangeIcon />
        ) : (
          <state.Icon aria-hidden className={cn("size-3.5", state.className)} />
        )}
      </span>
    </li>
  );
}

/**
 * Review candidates with the pending requests checked; choosing one checks or unchecks it,
 * and the section sends every change at once when the picker closes.
 * People come from the PR's reviewers, the repository's candidates and, where this Mac
 * keeps one, its organization's list, ranked locally as the user types.
 */
function PullRequestReviewerPicker({
  conversationId,
  prUrl,
  author,
  reviewers,
  requested,
  chosen,
  summary,
  edits,
  onToggle,
  waiting,
  picker,
}: {
  conversationId: string;
  prUrl: string;
  author: string | null;
  /** The PR's reviewers, offered first. */
  reviewers: readonly PullRequestReviewerTarget[];
  requested: ReadonlySet<string>;
  /** Everyone the picker has checked, and what closing it will send. */
  chosen: readonly PullRequestReviewerTarget[];
  summary: string | null;
  edits: ReadonlyMap<string, ReviewerEdit>;
  onToggle: (target: PullRequestReviewerTarget) => void;
  /** A change is still being made on this Mac; the picker opens once it lands. */
  waiting: boolean;
  picker: ReturnType<typeof useSectionPicker>;
}) {
  const { open, setOpen } = picker;
  const [query, setQuery] = useState("");
  // However it closed, from the trigger, the section or outside, it opens with no filter.
  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);
  const source = useReviewerPeopleSource(conversationId, prUrl, open, query);
  const { requested: requestedHere, assignable } = source;
  // Folded once per list change; each keystroke only ranks.
  const people = useMemo(() => {
    const merged = mergePeople({
      reviewer: reviewers.map(({ login, kind }) => ({ login, kind, name: null })),
      requested: requestedHere,
      assignable,
    });
    // GitHub refuses a review request from the PR's author.
    return preparePeople(
      merged.filter(
        ({ person }) =>
          person.kind === "team" || person.login.toLowerCase() !== author?.toLowerCase(),
      ),
    );
  }, [reviewers, requestedHere, assignable, author]);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Request reviewers"
          className={SECTION_PICKER_TRIGGER_CLASS}
          disabled={waiting}
          title={waiting ? "Wait for the last reviewer change to finish" : undefined}
        >
          <PlusIcon aria-hidden className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="p-0"
        onPointerDownOutside={picker.onPointerDownOutside}
      >
        <ReviewerPickerList
          people={people}
          query={query}
          onQueryChange={setQuery}
          typed={typedReviewer}
          checked={requested}
          chosen={chosen}
          summary={summary}
          sendsOn="close"
          edits={edits}
          onToggle={onToggle}
          pending={source.pending}
          notes={source.notes}
        />
      </PopoverContent>
    </Popover>
  );
}

/** Compact reviewer rows that keep the assignment and current review state together. */
export function PullRequestReviewers({
  conversationId,
  prUrl,
  reviewers,
  author,
  editable,
  pingTarget = null,
}: {
  conversationId: string;
  /** The selected PR; editing needs one. */
  prUrl: string | null;
  reviewers: PullRequestReviewer[];
  author: string | null;
  editable: boolean;
  /** The PR whose requested reviewers a ping can remind; null offers no pings. */
  pingTarget?: ReviewerPingPullRequest | null;
}) {
  const update = useUpdatePullRequestReviewers(conversationId);
  const pings = useReviewerPingActions(pingTarget, "reviewer_row");
  const pending = usePendingPullRequestChanges<PullRequestReviewerChange>(
    pullRequestReviewersMutationKey(conversationId),
  );
  // The host queues reviewer changes, so a new one never waits on the last.
  const waiting = false;
  const changes = useMemo(
    () => pending.filter((change) => change.prUrl === prUrl),
    [pending, prUrl],
  );
  // The state once every unanswered edit lands, so a toggle undoes the last one.
  const intended = useMemo(
    () => changes.reduce(applyPullRequestReviewerChange, reviewers),
    [changes, reviewers],
  );
  const requested = useMemo(
    () =>
      new Set(
        intended
          .filter((reviewer) => reviewer.state === "PENDING")
          .map((reviewer) => pullRequestReviewerKey(reviewer)),
      ),
    [intended],
  );
  // Each reviewer's latest unanswered edit; the rows keep the provider's state until it answers.
  const edits = useMemo(() => {
    const latest = new Map<string, ReviewerEdit>();
    for (const change of changes) {
      for (const target of change.add) latest.set(pullRequestReviewerKey(target), "requesting");
      for (const target of change.remove) latest.set(pullRequestReviewerKey(target), "withdrawing");
    }
    return latest;
  }, [changes]);
  const shown = useMemo(() => {
    const present = new Set(reviewers.map(pullRequestReviewerKey));
    const requesting = changes
      .flatMap((change) => change.add)
      .filter((target) => !present.has(pullRequestReviewerKey(target)))
      .map(({ login, kind }) => ({ login, kind, state: "PENDING" as const }));
    return [
      ...reviewers,
      ...new Map(requesting.map((row) => [pullRequestReviewerKey(row), row])).values(),
    ];
  }, [changes, reviewers]);
  const editPrUrl = editable ? prUrl : null;
  const picker = useSectionPicker(!!editPrUrl && !waiting);
  // A row's Request Review… aimed at this view opens the picker once it can edit.
  useTakenControlRequest(
    prUrl ?? "",
    "reviewers",
    !!editPrUrl && !waiting,
    () => picker.setOpen(true),
    picker.open,
  );
  /** Send `change` afresh: a toggle, or Retry after a refusal. */
  const send = (change: PullRequestReviewerChange) => {
    if (change.add.length === 0 && change.remove.length === 0) return;
    announceChange(reviewerChangeText(change, "sending"));
    update.mutate(change, {
      onSuccess: () => announceChange(reviewerChangeText(change, "done")),
      // A failure is said by its notice, an alert, so it isn't announced twice.
    });
  };
  const toggle = (target: PullRequestReviewerTarget) => {
    if (!editPrUrl || waiting) return;
    const withdraw = requested.has(pullRequestReviewerKey(target));
    const { login, kind } = target;
    send({
      prUrl: editPrUrl,
      add: withdraw ? [] : [{ login, kind }],
      remove: withdraw ? [{ login, kind }] : [],
    });
  };
  // The picker's checks while it's open, sent as one change once it closes and nothing else
  // is still being sent from this Mac, and only as whoever made them.
  const draft = usePickerDraft({
    prUrl: editPrUrl,
    current: requested,
    open: picker.open,
    waiting,
    keyOf: pullRequestReviewerKey,
    send,
  });
  // The latest reviewer change to this PR.
  const ownChange = useCallback(
    (change: PullRequestReviewerChange) => change.prUrl === prUrl,
    [prUrl],
  );
  const status = useChangeStatus(pullRequestReviewersMutationKey(conversationId), ownChange);
  // A refusal may be tried again; a change GitHub may have made waits for the next read.
  const retry =
    status.kind === "failed" &&
    editPrUrl &&
    !waiting &&
    !(status.error instanceof PullRequestWriteError && status.error.outcomeUnknown)
      ? () =>
          send({
            prUrl: status.variables.prUrl,
            add: status.variables.add.map(({ login, kind }) => ({ login, kind })),
            remove: status.variables.remove.map(({ login, kind }) => ({ login, kind })),
          })
      : undefined;

  return (
    <section
      {...picker.sectionProps}
      className={cn("space-y-1.5", picker.sectionProps.className)}
      aria-labelledby="pull-request-reviewers-heading"
    >
      <PullRequestSectionHeader
        id="pull-request-reviewers-heading"
        title="Reviewers"
        action={
          editPrUrl && (
            <PullRequestReviewerPicker
              conversationId={conversationId}
              prUrl={editPrUrl}
              author={author}
              reviewers={shown}
              requested={draft.checked}
              chosen={pickerDraftTargets(draft.draft)}
              summary={pickerDraftSummary(draft.change, REVIEWER_DRAFT_WORDS)}
              edits={edits}
              onToggle={(target) => draft.toggle({ login: target.login, kind: target.kind })}
              waiting={waiting}
              picker={picker}
            />
          )
        }
      />
      {shown.length === 0 ? (
        <p className="text-ui text-muted-foreground">No reviewers</p>
      ) : (
        <ul>
          {shown.map((reviewer) => (
            <PullRequestReviewerRow
              key={pullRequestReviewerKey(reviewer)}
              reviewer={reviewer}
              edit={edits.get(pullRequestReviewerKey(reviewer))}
              onToggle={editPrUrl && !waiting ? () => toggle(reviewer) : undefined}
              ping={reviewer.kind === "user" ? pings.get(reviewer.login.toLowerCase()) : undefined}
            />
          ))}
        </ul>
      )}
      {status.kind === "failed" && (
        <ChangeFailedNotice
          message={`${reviewerFailurePrefix(status.variables)}: ${status.error.message}`}
          onRetry={retry}
          onDismiss={status.dismiss}
        />
      )}
    </section>
  );
}
