// What a reviewer picker lists and says: people from a PR's reviewers, those requested before and
// the repository's candidates, ranked as the user types, plus a typed login or team. One PR's picker and the Pull Requests page's picker for a selection both show this list.

import { useHostReviewerSearch } from "@/hooks/useHostReviewerSearch";
import { type ReactNode, useMemo, useState } from "react";
import { Loader2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandList,
} from "@/components/ui/command";
import {
  pullRequestReviewerKey,
  usePullRequestReviewCandidates,
  type PullRequestReviewCandidate,
  type PullRequestReviewerTarget,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { useRequestedReviewers } from "@/lib/requestedReviewers";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import {
  type Person,
  type PreparedPerson,
  rankPeople,
  reviewCandidatesFailureNote,
} from "@/lib/peopleSearch";
import { CheckboxCommandItem } from "@/pullRequestPage/CheckboxCommandItem";

/** An edit the provider has not answered yet: a review request or its withdrawal. */
export type ReviewerEdit = "requesting" | "withdrawing";

export const REVIEWER_EDIT_LABEL: Record<ReviewerEdit, string> = {
  requesting: "Requesting…",
  withdrawing: "Withdrawing…",
};

export function reviewerSubject(reviewer: PullRequestReviewerTarget): string {
  return reviewer.kind === "team" ? `Team ${reviewer.login}` : reviewer.login;
}

/** A typed `login`, or a typed `org/team` with its organization, or null. */
export function typedReviewerIn(
  query: string,
): (PullRequestReviewerTarget & { organization: string | null }) | null {
  const text = query.trim().replace(/^@/, "");
  const team = /^([A-Za-z0-9-]+)\/([A-Za-z0-9_.-]+)$/.exec(text);
  if (team) return { login: team[2], kind: "team", organization: team[1] };
  return /^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(text)
    ? { login: text, kind: "user", organization: null }
    : null;
}

/** A typed `login` or `org/team` one PR's picker can request a review from, or null. */
export function typedReviewer(query: string): PullRequestReviewerTarget | null {
  const typed = typedReviewerIn(query);
  return typed && { login: typed.login, kind: typed.kind };
}

/** A line under the list; a failure's line may offer Try again. */
export interface ReviewerPickerNote {
  text: string;
  /** Announced as it changes: a failure, or a wait. */
  status?: boolean;
  retry?: { run: () => void; retrying: boolean };
}

/** What one PR's picker reads while it's open: its people and what to say about them. */
export interface ReviewerPeopleSource {
  /** The repository's users and teams that can be asked; teams by slug. */
  assignable: readonly PullRequestReviewCandidate[];
  /** Who this user requested reviews from on this device, most recent first. */
  requested: readonly Person[];
  /** Nothing has answered yet, so an empty list means "still reading". */
  pending: boolean;
  /** Notes in the order they show: teams left unlisted, then a failure or a partial list. */
  notes: ReviewerPickerNote[];
}

/** The footer line for a failed candidates read: never transport text, always a way on. */
function candidatesFailureNote(prUrl: string): string {
  return reviewCandidatesFailureNote(parseCodeHostPullRequestUrl(prUrl)?.repository ?? null);
}

/** The people `prUrl`'s picker offers, read while `open`. */
export function useReviewerPeopleSource(
  conversationId: string,
  prUrl: string,
  open: boolean,
  query = "",
): ReviewerPeopleSource {
  const candidates = usePullRequestReviewCandidates(conversationId, prUrl, open);
  // The host searches for the typed text beyond the listed candidates.
  const hostSearch = useHostReviewerSearch(conversationId, prUrl, query, open);
  const requested = useRequestedReviewers(prUrl);
  const notes: ReviewerPickerNote[] = [];
  // The account the list was read as can't see the repository's teams: say whose list it is.
  if (candidates.data?.teams_unavailable) {
    notes.push({ text: "Teams aren’t listed for this account." });
  }
  if (candidates.isError) {
    notes.push({ text: candidatesFailureNote(prUrl), status: true });
  } else if (candidates.data?.has_more) {
    notes.push({
      text: `Showing the first ${candidates.data.data.length}. Type a login to request anyone else.`,
    });
  }
  const listed = candidates.data?.data ?? NO_CANDIDATES;
  return {
    assignable: hostSearch.candidates.length > 0 ? [...listed, ...hostSearch.candidates] : listed,
    requested,
    pending: candidates.isPending || hostSearch.searching,
    notes,
  };
}

/**
 * `people` with those in `checkedKeys` moved to the front, each group keeping its order, so
 * the reviewers already asked show first however long the list is.
 */
export function checkedReviewersFirst(
  people: readonly PreparedPerson[],
  checkedKeys: ReadonlySet<string>,
): PreparedPerson[] {
  if (checkedKeys.size === 0) return [...people];
  const checked: PreparedPerson[] = [];
  const rest: PreparedPerson[] = [];
  for (const entry of people) {
    (checkedKeys.has(pullRequestReviewerKey(entry.person)) ? checked : rest).push(entry);
  }
  return [...checked, ...rest];
}

const NO_CANDIDATES: readonly PullRequestReviewCandidate[] = [];
const NO_TARGETS: readonly PullRequestReviewerTarget[] = [];

function CandidateItem({
  candidate,
  checked,
  edits,
  onToggle,
  namesKind = false,
}: {
  candidate: Person;
  checked: ReadonlySet<string>;
  edits?: ReadonlyMap<string, ReviewerEdit>;
  onToggle: (target: PullRequestReviewerTarget) => void;
  /** Says a team is one, where no Teams heading does, as a user may share its name. */
  namesKind?: boolean;
}) {
  const edit = edits?.get(pullRequestReviewerKey(candidate));
  return (
    <CheckboxCommandItem
      checked={checked.has(pullRequestReviewerKey(candidate))}
      value={candidate.kind === "team" ? `team ${candidate.login}` : candidate.login}
      keywords={candidate.name ? [candidate.name] : undefined}
      onSelect={() => onToggle(candidate)}
    >
      <span className="min-w-0 flex-1">
        <span className="block truncate">
          {namesKind ? reviewerSubject(candidate) : candidate.login}
        </span>
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
    </CheckboxCommandItem>
  );
}

/** A typed login or team the lists don't name: asked anyway, GitHub checks it when sent. */
function UnlistedItem({
  target,
  checked,
  onToggle,
  sendsOn,
}: {
  target: PullRequestReviewerTarget;
  checked: boolean;
  onToggle: (target: PullRequestReviewerTarget) => void;
  sendsOn: "close" | "request";
}) {
  return (
    <CheckboxCommandItem
      // Force-mounted: it always matches what was typed, or was checked earlier.
      forceMount
      checked={checked}
      value={`unlisted ${target.kind} ${target.login}`}
      onSelect={() => onToggle(target)}
    >
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-baseline gap-2">
          <span className="min-w-0 truncate">{reviewerSubject(target)}</span>
          <span className="ml-auto shrink-0 text-xs text-muted-foreground">Not in list</span>
        </span>
        <span className="block truncate text-xs text-muted-foreground">
          GitHub checks it when you {sendsOn === "close" ? "close" : "request"}
        </span>
      </span>
    </CheckboxCommandItem>
  );
}

function PickerNote({ note }: { note: ReviewerPickerNote }) {
  return (
    <p
      role={note.status ? "status" : undefined}
      className="border-t border-border px-3 py-1.5 text-xs text-muted-foreground"
    >
      {note.text}
      {note.retry && (
        <>
          {" "}
          <Button
            type="button"
            variant="link"
            size="xs"
            className="h-auto p-0 text-xs"
            disabled={note.retry.retrying}
            onClick={note.retry.run}
          >
            {note.retry.retrying ? "Trying again…" : "Try again"}
          </Button>
        </>
      )}
    </p>
  );
}

/**
 * The filter and the ranked People and Teams, checked where `checked` says, with a typed login
 * the lists don't name as a row of its own; choosing one calls `onToggle`. `summary`, `account`
 * and notes show beneath.
 */
export function ReviewerPickerList({
  people,
  query,
  onQueryChange,
  typed,
  checked,
  chosen = NO_TARGETS,
  edits,
  onToggle,
  sendsOn,
  summary,
  pending,
  account,
  notes,
}: {
  /** Everyone to offer, prepared once per list change. */
  people: readonly PreparedPerson[];
  query: string;
  onQueryChange: (query: string) => void;
  /** What the typed text asks for, where it names someone. */
  typed: (query: string) => PullRequestReviewerTarget | null;
  checked: ReadonlySet<string>;
  /** Everyone checked, so one the lists don't name stays listed while it's checked. */
  chosen?: readonly PullRequestReviewerTarget[];
  edits?: ReadonlyMap<string, ReviewerEdit>;
  onToggle: (target: PullRequestReviewerTarget) => void;
  /** When checks are sent: as the picker closes, or by the surrounding Request button. */
  sendsOn: "close" | "request";
  /** What closing will send, such as "1 to request · sent when you close". */
  summary?: string | null;
  /** Nothing has answered yet. */
  pending: boolean;
  /** Who changes are made as. */
  account?: ReactNode;
  notes: readonly ReviewerPickerNote[];
}) {
  const known = useMemo(
    () => new Set(people.map(({ person }) => pullRequestReviewerKey(person))),
    [people],
  );
  // Who was checked as the list opened leads it; checking more while it's open moves no rows.
  const [checkedAtOpen] = useState(() => new Set(checked));
  const ranked = useMemo(
    () =>
      query.trim() === ""
        ? rankPeople(query, checkedReviewersFirst(people, checkedAtOpen))
        : rankPeople(query, people),
    [query, people, checkedAtOpen],
  );
  // Unfiltered, those checked as it opened lead the list, people and teams alike.
  const leading = (person: Person) =>
    query.trim() === "" && checkedAtOpen.has(pullRequestReviewerKey(person));
  const requestedRows = ranked.map(({ person }) => person).filter(leading);
  const rest = ranked.filter(({ person }) => !leading(person));
  const users = rest.filter(({ person }) => person.kind === "user").map(({ person }) => person);
  const teams = rest.filter(({ person }) => person.kind === "team").map(({ person }) => person);
  const typedTarget = typed(query);
  const filter = query.trim().replace(/^@/, "").toLowerCase();
  // Logins the lists don't name: checked ones that still match the filter, then the typed one.
  const unlisted = new Map<string, PullRequestReviewerTarget>();
  for (const target of chosen) {
    const key = pullRequestReviewerKey(target);
    if (!known.has(key) && checked.has(key) && target.login.toLowerCase().includes(filter)) {
      unlisted.set(key, target);
    }
  }
  if (typedTarget && !known.has(pullRequestReviewerKey(typedTarget))) {
    unlisted.set(pullRequestReviewerKey(typedTarget), typedTarget);
  }
  const unlistedRows = (kind: PullRequestReviewerTarget["kind"]) =>
    [...unlisted.entries()]
      .filter(([, target]) => target.kind === kind)
      .map(([key, target]) => (
        <UnlistedItem
          key={`unlisted:${key}`}
          target={target}
          checked={checked.has(key)}
          onToggle={onToggle}
          sendsOn={sendsOn}
        />
      ));
  const unlistedUsers = unlistedRows("user");
  const unlistedTeams = unlistedRows("team");
  // Only while nothing at all is known; typing and the typed row never wait for it.
  const loading = pending && people.length === 0;
  const item = (candidate: Person, namesKind = false) => (
    <CandidateItem
      // A user and a team can share a name, so the key names its kind too.
      key={pullRequestReviewerKey(candidate)}
      candidate={candidate}
      checked={checked}
      edits={edits}
      onToggle={onToggle}
      namesKind={namesKind}
    />
  );
  return (
    <Command label="Filter reviewers" shouldFilter={false}>
      <CommandInput
        placeholder="Filter or type a login…"
        value={query}
        onValueChange={onQueryChange}
      />
      <CommandList>
        {loading && unlisted.size === 0 ? (
          <p role="status" className="py-6 text-center text-ui text-muted-foreground">
            Loading reviewers…
          </p>
        ) : (
          <>
            {unlisted.size === 0 && (
              <CommandEmpty>
                {people.length === 0 ? "No one to request." : "No reviewers match."}
              </CommandEmpty>
            )}
            {requestedRows.length > 0 && (
              <CommandGroup heading="Requested">
                {requestedRows.map((person) => item(person, true))}
              </CommandGroup>
            )}
            {users.length + unlistedUsers.length > 0 && (
              <CommandGroup heading="People" forceMount={unlistedUsers.length > 0}>
                {users.map((person) => item(person))}
                {unlistedUsers}
              </CommandGroup>
            )}
            {teams.length + unlistedTeams.length > 0 && (
              <CommandGroup heading="Teams" forceMount={unlistedTeams.length > 0}>
                {teams.map((person) => item(person))}
                {unlistedTeams}
              </CommandGroup>
            )}
          </>
        )}
      </CommandList>
      {summary && (
        <p
          aria-live="polite"
          className="border-t border-border px-3 py-1.5 text-xs text-muted-foreground"
        >
          {summary}
        </p>
      )}
      {account}
      {notes.map((note) => (
        <PickerNote key={note.text} note={note} />
      ))}
    </Command>
  );
}
