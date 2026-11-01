// The Summary tab's Reviewers section: each reviewer's review state and, for editors,
// a picker that requests reviews. An edit shows as in progress until the provider answers.

import { useMemo, useState } from "react";
import { useMutationState } from "@tanstack/react-query";
import {
  AlertCircleIcon,
  CircleCheckIcon,
  CircleDotIcon,
  CircleXIcon,
  Loader2Icon,
  MessageCircleIcon,
  PlusIcon,
  RotateCwIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import {
  applyPullRequestReviewerChange,
  pullRequestReviewerKey,
  pullRequestReviewersMutationKey,
  usePullRequestReviewCandidates,
  useUpdatePullRequestReviewers,
  type PullRequestReviewCandidate,
  type PullRequestReviewer,
  type PullRequestReviewerChange,
  type PullRequestReviewerTarget,
} from "@/hooks/usePullRequests";
import {
  PULL_REQUEST_CHIP_CLASS,
  PullRequestChipAction,
  PullRequestSectionHeader,
} from "./PullRequestSection";

const REVIEWER_STATE_META: Record<
  PullRequestReviewer["state"],
  { label: string; className: string; Icon: LucideIcon }
> = {
  PENDING: {
    label: "Pending",
    className: "text-amber-700 dark:text-amber-400",
    Icon: CircleDotIcon,
  },
  APPROVED: {
    label: "Approved",
    className: "text-green-700 dark:text-green-400",
    Icon: CircleCheckIcon,
  },
  COMMENTED: {
    label: "Commented",
    className: "text-blue-700 dark:text-blue-400",
    Icon: MessageCircleIcon,
  },
  CHANGES_REQUESTED: {
    label: "Changes requested",
    className: "text-red-700 dark:text-red-400",
    Icon: CircleXIcon,
  },
  DISMISSED: {
    label: "Dismissed",
    className: "text-muted-foreground",
    Icon: AlertCircleIcon,
  },
};

function reviewerSubject(reviewer: PullRequestReviewerTarget): string {
  return reviewer.kind === "team" ? `Team ${reviewer.login}` : reviewer.login;
}

/** A typed `login` or `org/team` the picker can request a review from, or null. */
export function typedReviewer(query: string): PullRequestReviewerTarget | null {
  const text = query.trim().replace(/^@/, "");
  const team = /^[A-Za-z0-9-]+\/([A-Za-z0-9_.-]+)$/.exec(text);
  if (team) return { login: team[1], kind: "team" };
  return /^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(text) ? { login: text, kind: "user" } : null;
}

/** An edit the provider has not answered yet: a review request or its withdrawal. */
type ReviewerEdit = "requesting" | "withdrawing";

const REVIEWER_EDIT_LABEL: Record<ReviewerEdit, string> = {
  requesting: "Requesting…",
  withdrawing: "Withdrawing…",
};

/** Keep reviewer colors for review state; author colors belong in comments. */
function PullRequestReviewerRow({
  reviewer,
  edit,
  onToggle,
}: {
  reviewer: PullRequestReviewer;
  edit?: ReviewerEdit;
  onToggle?: () => void;
}) {
  const state = REVIEWER_STATE_META[reviewer.state];
  const StateIcon = edit ? Loader2Icon : state.Icon;
  const stateLabel = edit ? REVIEWER_EDIT_LABEL[edit] : state.label;
  const subject = reviewerSubject(reviewer);
  const pending = reviewer.state === "PENDING";

  return (
    <li
      aria-label={`${subject}: ${stateLabel}`}
      aria-busy={edit ? true : undefined}
      className={cn(
        PULL_REQUEST_CHIP_CLASS,
        "rounded-md",
        edit && "border-dashed",
        edit === "withdrawing" && "opacity-60",
      )}
    >
      <span
        className={cn(
          "min-w-0 truncate font-medium text-foreground",
          edit === "withdrawing" && "line-through",
        )}
      >
        {reviewer.login}
      </span>
      {reviewer.kind === "team" && <span className="shrink-0 text-muted-foreground">(team)</span>}
      <span
        className={cn(
          "inline-flex shrink-0 items-center gap-1",
          edit ? "text-muted-foreground" : state.className,
        )}
      >
        <StateIcon aria-hidden className={cn("size-3", edit && "animate-spin")} />
        <span>{stateLabel}</span>
      </span>
      {onToggle && !edit && (
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
      )}
    </li>
  );
}

function CandidateItem({
  candidate,
  requested,
  edits,
  onToggle,
}: {
  candidate: PullRequestReviewCandidate;
  requested: ReadonlySet<string>;
  edits: ReadonlyMap<string, ReviewerEdit>;
  onToggle: (target: PullRequestReviewerTarget) => void;
}) {
  const edit = edits.get(pullRequestReviewerKey(candidate));
  return (
    <CommandItem
      value={candidate.kind === "team" ? `team ${candidate.login}` : candidate.login}
      keywords={candidate.name ? [candidate.name] : undefined}
      data-checked={requested.has(pullRequestReviewerKey(candidate))}
      onSelect={() => onToggle(candidate)}
    >
      <span className="min-w-0 flex-1">
        <span className="block truncate">{candidate.login}</span>
        {candidate.name && (
          <span className="block truncate text-xs text-muted-foreground">{candidate.name}</span>
        )}
      </span>
      {edit && (
        <Loader2Icon
          role="img"
          aria-label={REVIEWER_EDIT_LABEL[edit]}
          className="size-3.5 animate-spin text-muted-foreground"
        />
      )}
    </CommandItem>
  );
}

/** Review candidates with the pending requests checked; choosing one toggles its request. */
function PullRequestReviewerPicker({
  conversationId,
  prUrl,
  author,
  requested,
  edits,
  onToggle,
}: {
  conversationId: string;
  prUrl: string;
  author: string | null;
  requested: ReadonlySet<string>;
  edits: ReadonlyMap<string, ReviewerEdit>;
  onToggle: (target: PullRequestReviewerTarget) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const candidates = usePullRequestReviewCandidates(conversationId, prUrl, open);
  // GitHub refuses a review request from the PR's author.
  const listed = (candidates.data?.data ?? []).filter(
    (candidate) =>
      candidate.kind === "team" || candidate.login.toLowerCase() !== author?.toLowerCase(),
  );
  const users = listed.filter((candidate) => candidate.kind === "user");
  const teams = listed.filter((candidate) => candidate.kind === "team");
  const typed = typedReviewer(query);
  const typedUnlisted =
    typed &&
    !listed.some((candidate) => pullRequestReviewerKey(candidate) === pullRequestReviewerKey(typed))
      ? typed
      : null;

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setQuery("");
      }}
    >
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Request reviewers"
          className="text-muted-foreground"
        >
          <PlusIcon aria-hidden className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="p-0">
        <Command label="Filter reviewers">
          <CommandInput
            placeholder="Filter or type a login…"
            value={query}
            onValueChange={setQuery}
          />
          <CommandList>
            {typedUnlisted && (
              // Force-mounted: the typed entry always matches what was typed.
              <CommandGroup forceMount>
                <CommandItem
                  forceMount
                  value={`request ${typedUnlisted.kind} ${typedUnlisted.login}`}
                  data-checked={requested.has(pullRequestReviewerKey(typedUnlisted))}
                  onSelect={() => onToggle(typedUnlisted)}
                >
                  <span className="min-w-0 flex-1 truncate">
                    Request review from {reviewerSubject(typedUnlisted)}
                  </span>
                </CommandItem>
              </CommandGroup>
            )}
            {candidates.isPending ? (
              <p role="status" className="py-6 text-center text-ui text-muted-foreground">
                Loading reviewers…
              </p>
            ) : candidates.isError ? (
              <p role="alert" className="px-2 py-6 text-center text-ui text-destructive">
                {candidates.error.message}
              </p>
            ) : (
              <>
                {!typedUnlisted && (
                  <CommandEmpty>
                    {listed.length === 0 ? "No one to request." : "No reviewers match."}
                  </CommandEmpty>
                )}
                {users.length > 0 && (
                  <CommandGroup heading="People">
                    {users.map((candidate) => (
                      <CandidateItem
                        key={candidate.login}
                        candidate={candidate}
                        requested={requested}
                        edits={edits}
                        onToggle={onToggle}
                      />
                    ))}
                  </CommandGroup>
                )}
                {teams.length > 0 && (
                  <CommandGroup heading="Teams">
                    {teams.map((candidate) => (
                      <CandidateItem
                        key={candidate.login}
                        candidate={candidate}
                        requested={requested}
                        edits={edits}
                        onToggle={onToggle}
                      />
                    ))}
                  </CommandGroup>
                )}
              </>
            )}
          </CommandList>
          {candidates.data?.has_more && (
            <p className="border-t border-border px-3 py-1.5 text-xs text-muted-foreground">
              Showing the first {listed.length}. Type a login to request anyone else.
            </p>
          )}
        </Command>
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
}: {
  conversationId: string;
  /** The selected PR; editing needs one. */
  prUrl: string | null;
  reviewers: PullRequestReviewer[];
  author: string | null;
  editable: boolean;
}) {
  const update = useUpdatePullRequestReviewers(conversationId);
  const pending = useMutationState({
    filters: { mutationKey: pullRequestReviewersMutationKey(conversationId), status: "pending" },
    select: (mutation) => mutation.state.variables as PullRequestReviewerChange | undefined,
  });
  const changes = useMemo(
    () => pending.filter((change): change is PullRequestReviewerChange => change?.prUrl === prUrl),
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
  const toggle = (target: PullRequestReviewerTarget) => {
    if (!editPrUrl) return;
    const withdraw = requested.has(pullRequestReviewerKey(target));
    update.mutate({
      prUrl: editPrUrl,
      add: withdraw ? [] : [target],
      remove: withdraw ? [target] : [],
    });
  };

  return (
    <section className="space-y-1.5" aria-labelledby="pull-request-reviewers-heading">
      <PullRequestSectionHeader
        id="pull-request-reviewers-heading"
        title="Reviewers"
        action={
          editPrUrl && (
            <PullRequestReviewerPicker
              conversationId={conversationId}
              prUrl={editPrUrl}
              author={author}
              requested={requested}
              edits={edits}
              onToggle={toggle}
            />
          )
        }
      />
      {shown.length === 0 ? (
        <p className="text-ui text-muted-foreground">No reviewers assigned.</p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {shown.map((reviewer) => (
            <PullRequestReviewerRow
              key={pullRequestReviewerKey(reviewer)}
              reviewer={reviewer}
              edit={edits.get(pullRequestReviewerKey(reviewer))}
              onToggle={editPrUrl ? () => toggle(reviewer) : undefined}
            />
          ))}
        </ul>
      )}
      {update.isError && update.variables?.prUrl === prUrl && (
        <p role="alert" className="text-ui text-destructive">
          {update.error.message}
        </p>
      )}
    </section>
  );
}
