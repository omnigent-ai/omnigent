// The stack the PR on screen belongs to, with where each member stands and the ways to move
// through it. GitHub's own stack, where it says the PR has one, is the only source; else the
// description's. Off where the desktop flag is, and in browsers.

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";

import { useDesktopFeatureFlag } from "@/hooks/useDesktopFeatureFlag";
import {
  forgetStackMembership,
  recordStackSnapshot,
  stackSnapshotOf,
} from "@/hooks/pullRequestStackSnapshots";
import { type NativeStackData, usePullRequestNativeStack } from "@/hooks/usePullRequestNativeStack";
import {
  pullRequestStackMembersQueryKey,
  usePullRequestStackMembers,
} from "@/hooks/usePullRequestStackMembers";
import {
  type NativeStackNeighbor,
  type PartialNativeStack,
  partialNativeStack,
  readNativeStack,
} from "@/lib/pullRequestStacks/nativeStack";
import { stackFormats } from "@/lib/pullRequestStacks/formats";
import { nativeStackMembers } from "@/lib/pullRequestStacks/nativeStackHost";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import {
  type ListedStack,
  listedStackExtends,
  listedStackMembers,
} from "@/lib/pullRequestStacks/listedStack";
import {
  stackMemberLookups,
  type StackMembersAnswer,
  stackMemberStatuses,
  type StackMembersRead,
  type StackMemberStatuses,
} from "@/lib/pullRequestStacks/stackMembers";
import {
  type StackParse,
  type StackPullRequestRef,
  stackPullRequestRef,
  stackRefKey,
} from "@/lib/pullRequestStacks/stackModel";
import {
  describedStackIdentity,
  describedStackSnapshot,
  nativeStackForMember,
  nativeStackSnapshot,
  snapshotDescribes,
  type NativeStackedRead,
  type StackSnapshot,
} from "@/lib/pullRequestStacks/stackSnapshot";
import {
  AT_STACK_BASE,
  CHILD_NOT_READ,
  END_NOT_READ,
  firstInStack,
  lastInStack,
  nextInStack,
  NOTHING_STACKED,
  PARENT_NOT_READ,
  POSITION_NOT_READ,
  previousInStack,
  type StackPosition,
  stackPosition,
  type StackStep,
  type StackTreeRow,
  stackTreeRows,
} from "@/lib/pullRequestStacks/stackNavigation";
import { navigableStack } from "@/lib/pullRequestStacks/stackTrust";
import type { StackReadAhead } from "@/lib/pullRequestStacks/stackWarmup";
import type { PullRequestSubtab } from "@/shell/pullRequestPanelView";

export const PULL_REQUEST_STACKS_FLAG = "pull-request-stacks";

export interface PullRequestStackView {
  parse: StackParse;
  /** The members' read the statuses came from, for a view of another revision of the stack. */
  members: StackMembersRead;
  statuses: StackMemberStatuses;
  position: StackPosition;
  previous: StackStep;
  next: StackStep;
  /** The stack's first and last entries, the base and the top of a straight stack. */
  first: StackStep;
  last: StackStep;
  rows: StackTreeRow[];
  /** The level each node of `parse` is numbered with, base first; null where unknown. */
  rowNumbers: (number | null)[];
  /** Show another PR of the stack where this one shows; false when this view can't. */
  selectInPlace: (url: string) => boolean;
  /** GitHub's own stack, read only in part: how many PRs it has, and where to see it whole. */
  partial: NativeStackPartial | null;
  /**
   * GitHub's stack as this view depends on it, its identity, base, size and this PR's place,
   * so what was read for another one isn't shown; null for a description's stack.
   */
  nativeRevision: string | null;
  /** The tab another member opens on where it can't show in place; Overview when unset. */
  subtab?: PullRequestSubtab;
  /** Moves a member's reads ahead while the user shows interest in its row; unset where none. */
  readAhead?: StackReadAhead;
  /** Shown in part while the PR's own read of GitHub's stack, which may list the rest, is coming. */
  completing?: boolean;
}

/** A GitHub stack the page could read only part of. */
export interface NativeStackPartial {
  /** Null when GitHub didn't say. */
  size: number | null;
  /** The PR on GitHub, whose own page shows the whole stack. */
  url: string;
}

/** What the stack is read from: the PR on screen as its details last came back. */
export interface StackedPullRequest {
  url: string;
  body?: string | null;
  head_sha?: string;
  head_ref?: string | null;
  base_sha?: string;
  base_ref?: string | null;
}

/**
 * The PR's revision as GitHub's own stack read keys it: a push or a retarget is another
 * revision, whose stack GitHub may say otherwise.
 */
export function nativeStackRevision(pr: StackedPullRequest): string {
  return JSON.stringify([pr.head_sha ?? null, pr.base_ref ?? null, pr.base_sha ?? null]);
}

/** A step out of a stack whose kind can't be moved through. */
const NOT_NAVIGABLE: StackStep = {
  kind: "unavailable",
  reason: "This stack can't be moved through",
};

/** Everything the page shows of a stack, from its parse and its members' read. */
export function pullRequestStackView(
  parse: StackParse,
  members: StackMembersRead,
  selectInPlace: (url: string) => boolean,
  partial: NativeStackPartial | null = null,
): PullRequestStackView {
  const navigable = navigableStack(parse);
  return {
    parse,
    members,
    statuses: stackMemberStatuses(parse, members),
    position: stackPosition(parse),
    previous: navigable ? previousInStack(parse) : NOT_NAVIGABLE,
    next: navigable ? nextInStack(parse) : NOT_NAVIGABLE,
    first: navigable ? firstInStack(parse) : NOT_NAVIGABLE,
    last: navigable ? lastInStack(parse) : NOT_NAVIGABLE,
    rows: stackTreeRows(parse),
    rowNumbers: parse.nodes.map((node) => node.depth + 1),
    selectInPlace,
    partial,
    nativeRevision: null,
  };
}

type NativeStacked = NativeStackedRead;

/** What of GitHub's stack a view of it shows, so another base or place reads as another. */
function nativeViewRevision(read: NativeStacked): string {
  const position =
    read.kind === "stacked" ? read.parse.selfIndex + 1 : (read.position ?? "unknown");
  return JSON.stringify([read.key, read.baseRefName, read.size, position]);
}

/** A move a partial read can't make: an end of the stack, or a neighbor it didn't read. */
function partialStep(
  neighbor: NativeStackNeighbor,
  partial: PartialNativeStack,
  end: string,
  unread: string,
): StackStep {
  if (neighbor === "end") return { kind: "unavailable", reason: end };
  return { kind: "unavailable", reason: partial.position === null ? POSITION_NOT_READ : unread };
}

const unreadEnd: StackStep = { kind: "unavailable", reason: END_NOT_READ };

/** The view of GitHub's stack read in part: its own position and size, and only read moves. */
export function partialNativeStackView(
  partial: PartialNativeStack,
  url: string,
  members: StackMembersRead,
  selectInPlace: (url: string) => boolean,
): PullRequestStackView {
  const view = pullRequestStackView(partial.parse, members, selectInPlace, {
    size: partial.size,
    url,
  });
  return {
    ...view,
    position: { level: partial.position, levels: partial.size },
    previous:
      partial.below === "read"
        ? view.previous
        : partialStep(partial.below, partial, AT_STACK_BASE, PARENT_NOT_READ),
    next:
      partial.above === "read"
        ? view.next
        : partialStep(partial.above, partial, NOTHING_STACKED, CHILD_NOT_READ),
    // An end goes only where the rows read reach it: GitHub's first position, or its last.
    first: partial.rowNumbers[0] === 1 ? view.first : unreadEnd,
    last:
      partial.size !== null && partial.rowNumbers.at(-1) === partial.size ? view.last : unreadEnd,
    rowNumbers: partial.rowNumbers,
  };
}

/** A view of GitHub's stack for `self`, read whole or in part, with what it says of members. */
function nativeStackView(
  nativeStacked: NativeStacked,
  self: StackPullRequestRef,
  headRef: string | null,
  members: StackMembersAnswer | null,
  selectInPlace: (url: string) => boolean,
): PullRequestStackView {
  const read = members
    ? ({ kind: "loaded", answer: members } as const)
    : ({ kind: "unavailable" } as const);
  const view =
    nativeStacked.kind === "stacked"
      ? pullRequestStackView(nativeStacked.parse, read, selectInPlace)
      : partialNativeStackView(
          partialNativeStack(nativeStacked, self, headRef ?? ""),
          self.url,
          read,
          selectInPlace,
        );
  return { ...view, nativeRevision: nativeViewRevision(nativeStacked) };
}

/**
 * The stack the PR's own reads put it in: GitHub's stack where GitHub says it has one, else the
 * one its description lists, with that description's shared members read. Null for none.
 */
export function pullRequestOwnStackView({
  pr,
  native,
  describedMembers,
  selectInPlace,
}: {
  pr: StackedPullRequest;
  /** GitHub's answer for the PR, or null where it wasn't read. */
  native: NativeStackData | null;
  describedMembers: StackMembersRead;
  selectInPlace: (url: string) => boolean;
}): PullRequestStackView | null {
  const self = stackPullRequestRef(pr.url);
  if (!self) return null;
  const read = native ? readNativeStack(native.read?.answer, self) : null;
  if (native && (read?.kind === "stacked" || read?.kind === "stacked-partial")) {
    return nativeStackView(read, self, pr.head_ref ?? null, native.members, selectInPlace);
  }
  const described = parseStack(stackFormats(), pr.body ?? null, self);
  return described ? pullRequestStackView(described, describedMembers, selectInPlace) : null;
}

/** The PR as its own details last said, to tell whether a stack's record still describes it. */
interface ArrivalInputs {
  self: StackPullRequestRef;
  body: string | null;
  headRef: string | null;
  revision: { head_sha?: string; base_ref?: string | null; base_sha?: string };
  /** The shared members read for a description's stack, as cached. */
  describedMembers: (parse: StackParse) => StackMembersAnswer | undefined;
  now: number;
}

/**
 * The stack `snapshot` says the PR is in, as this PR would show it, while its own reads can't
 * say yet: GitHub's stack placed by its own positions, or this PR's own description where it
 * lists the same stack. Null where the record doesn't describe this PR at its head and base.
 */
export function arrivalStackView(
  snapshot: StackSnapshot,
  inputs: ArrivalInputs,
  selectInPlace: (url: string) => boolean,
): PullRequestStackView | null {
  const { self, revision, now } = inputs;
  if (snapshot.evidence.kind === "native") {
    if (!snapshotDescribes(snapshot, self.url, { now, revision })) return null;
    const forMember = nativeStackForMember(snapshot.evidence, self);
    if (!forMember) return null;
    const members = nativeStackMembers(forMember.answer, self);
    return nativeStackView(forMember.read, self, inputs.headRef, members, selectInPlace);
  }
  // A description's stack shows only as this PR's own description lists it.
  const parse = parseStack(stackFormats(), inputs.body, self);
  if (!parse || describedStackIdentity(parse) !== snapshot.identity) return null;
  const members = inputs.describedMembers(parse);
  if (!members || !snapshotDescribes(snapshot, self.url, { now, revision, members })) return null;
  return pullRequestStackView(parse, { kind: "loaded", answer: members }, selectInPlace);
}

/** What the PR's page knows of it before its own stack reads answer. */
export interface ListedStackInputs {
  self: StackPullRequestRef;
  body: string | null;
  headRef: string | null;
}

/**
 * The stack the page's list puts the PR in, as this PR would show it: GitHub's stack where the
 * list's read has one, else the one its description lists, each member as the list shows it.
 * Null where the list doesn't put it in a stack.
 */
export function listedStackView(
  listed: ListedStack,
  { self, body, headRef }: ListedStackInputs,
  selectInPlace: (url: string) => boolean,
): PullRequestStackView | null {
  const native = readNativeStack(listed.native, self);
  if (native.kind === "stacked" || native.kind === "stacked-partial") {
    const urls = (listed.native?.stack?.entries.nodes ?? []).flatMap(({ pullRequest }) =>
      pullRequest ? [pullRequest.url] : [],
    );
    return nativeStackView(native, self, headRef, listedStackMembers(listed, urls), selectInPlace);
  }
  const parse = parseStack(stackFormats(), body, self);
  if (!parse) return null;
  const urls = [...parse.nodes, parse.dependency].flatMap((node) =>
    node?.ref && !node.external ? [node.ref.url] : [],
  );
  return pullRequestStackView(
    parse,
    { kind: "loaded", answer: listedStackMembers(listed, urls) },
    selectInPlace,
  );
}

/**
 * The stack the list nests the PR at `url` in, before its details have read, where stacks
 * show: GitHub's, which needs no description.
 */
export function useListedStack(
  listed: ListedStack | undefined,
  url: string,
  selectInPlace: (url: string) => boolean,
): PullRequestStackView | null {
  const enabled = useDesktopFeatureFlag(PULL_REQUEST_STACKS_FLAG, false).value;
  return useMemo(() => {
    const self = enabled && listed ? stackPullRequestRef(url) : null;
    const view =
      self && listed
        ? listedStackView(listed, { self, body: null, headRef: null }, selectInPlace)
        : null;
    // Before its details, the PR's own reads are all still to come.
    return view?.partial ? { ...view, completing: true } : view;
  }, [enabled, listed, url, selectInPlace]);
}

/** The stack `pr` belongs to, or null when it isn't stacked or stacks aren't shown. */
export function usePullRequestStack(
  pr: StackedPullRequest | null,
  selectInPlace: (url: string) => boolean,
): PullRequestStackView | null {
  return usePullRequestStackRead(pr, selectInPlace).stack;
}

/** The stack `pr` belongs to, and whether GitHub's own answer, which may decide it, is pending. */
export interface PullRequestStackRead {
  stack: PullRequestStackView | null;
  /** GitHub hasn't said yet whether it stacks this PR, so what merges with it isn't known. */
  waiting: boolean;
  /**
   * While GitHub hasn't said, the stack another member's read says this PR is in, for the
   * header and Overview to show; never what merging, comparing or editing rely on.
   */
  arrival: PullRequestStackView | null;
  /**
   * What the header and Overview show: the PR's own stack, else its arrival. While its own
   * stack's members are still being read, the list's same stack, so the card keeps its rows.
   */
  shown: PullRequestStackView | null;
  /**
   * Nothing shows yet, but the PR's description lists a stack while GitHub's answer, which may
   * set it aside, is pending: the header holds the stack's place.
   */
  loading: boolean;
}

/** Whether two views list the same members in the same places. */
function sameStackRows(left: PullRequestStackView, right: PullRequestStackView): boolean {
  const key = ({ node }: PullRequestStackView["rows"][number]) =>
    node.ref ? stackRefKey(node.ref) : `branch:${node.branch}`;
  return (
    left.rows.length === right.rows.length &&
    left.rows.every((row, index) => key(row) === key(right.rows[index]!))
  );
}

/** How a view reads its PR's stack. */
export interface PullRequestStackReadOptions {
  /** The tab another member opens on where it can't show in place. */
  subtab?: PullRequestSubtab;
  /** When the PR's details, and so its description, last came back; 0 while unknown. */
  detailsReadAt?: number;
  /** What the page's list knows of the PR's stack, shown while GitHub's answer is pending. */
  listed?: ListedStack;
  /** The session whose host reads the stack's members; without one, none are read. */
  conversationId?: string;
}

/** {@link usePullRequestStack}, saying too while GitHub's own stack is still being read. */
export function usePullRequestStackRead(
  pr: StackedPullRequest | null,
  selectInPlace: (url: string) => boolean,
  { subtab, detailsReadAt = 0, listed, conversationId = "" }: PullRequestStackReadOptions = {},
): PullRequestStackRead {
  const enabled = useDesktopFeatureFlag(PULL_REQUEST_STACKS_FLAG, false).value;
  const queryClient = useQueryClient();
  const url = pr?.url ?? null;
  const body = pr?.body ?? null;
  const self = useMemo(() => (enabled && url ? stackPullRequestRef(url) : null), [enabled, url]);
  // Stack records are kept per session, whose host read them.
  const scopeKey = JSON.stringify(url && conversationId ? ["host", conversationId] : []);
  const prRevision = pr ? nativeStackRevision(pr) : null;
  const native = usePullRequestNativeStack(conversationId, url, prRevision, enabled && !!self);
  const nativeAnswer = native.kind === "settled" ? native.read?.answer : undefined;
  const nativeRead = useMemo(
    () => (self && native.kind === "settled" ? readNativeStack(nativeAnswer, self) : null),
    [self, native.kind, nativeAnswer],
  );
  // GitHub saying this PR is stacked decides; the description's stack is set aside then.
  const nativeStacked =
    nativeRead?.kind === "stacked" || nativeRead?.kind === "stacked-partial" ? nativeRead : null;
  const described = useMemo(
    () => (self && !nativeStacked ? parseStack(stackFormats(), body, self) : null),
    [self, nativeStacked, body],
  );
  // Waiting on GitHub shows no stack rather than one the answer might replace.
  const waiting = native.kind === "loading";
  const headSha = pr?.head_sha;
  const baseRef = pr?.base_ref;
  const baseSha = pr?.base_sha;
  const hasPr = pr !== null;
  const memberRevision = useMemo(
    () => (hasPr ? { head_sha: headSha, base_ref: baseRef, base_sha: baseSha } : null),
    [hasPr, headSha, baseRef, baseSha],
  );
  const describedMembers = usePullRequestStackMembers(
    conversationId,
    url,
    waiting ? null : described,
    memberRevision,
  );
  const headRef = pr?.head_ref ?? null;
  const nativeData = native.kind === "settled" ? native : null;
  const own = useMemo(
    () =>
      waiting || !self || !pr
        ? null
        : pullRequestOwnStackView({ pr, native: nativeData, describedMembers, selectInPlace }),
    // The PR's fields the stack depends on, not its object, which each read makes anew.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      waiting,
      self,
      nativeData?.read,
      nativeData?.members,
      body,
      headRef,
      describedMembers,
      selectInPlace,
    ],
  );

  // Each settled read is the stack's record for its other members to open with, as of when that
  // read came back: GitHub's answer for its stack, the details for a description's.
  const nativeReadAnswer = native.kind === "settled" ? native.read : undefined;
  const nativeReadAt = native.kind === "settled" ? native.readAt : 0;
  useEffect(() => {
    if (waiting || !self || !url) return;
    const readScope = JSON.parse(scopeKey) as unknown[];
    if (nativeStacked) {
      const snapshot =
        nativeReadAnswer && nativeReadAt > 0
          ? nativeStackSnapshot(nativeReadAnswer, url, nativeReadAt)
          : null;
      if (snapshot) recordStackSnapshot(queryClient, readScope, snapshot);
      return;
    }
    if (detailsReadAt <= 0) return;
    if (described) {
      recordStackSnapshot(
        queryClient,
        readScope,
        describedStackSnapshot(described, url, detailsReadAt),
      );
    } else {
      // Neither GitHub nor the description puts it in a stack, as of the newer of the two.
      forgetStackMembership(queryClient, readScope, url, Math.max(detailsReadAt, nativeReadAt));
    }
  }, [
    waiting,
    self,
    url,
    scopeKey,
    nativeStacked,
    nativeReadAnswer,
    nativeReadAt,
    described,
    detailsReadAt,
    queryClient,
  ]);

  const arrivalSnapshot =
    waiting && self && pr ? stackSnapshotOf(queryClient, JSON.parse(scopeKey), url!) : null;
  const recorded = useMemo(() => {
    if (!arrivalSnapshot || !self || !memberRevision) return null;
    return arrivalStackView(
      arrivalSnapshot,
      {
        self,
        body,
        headRef,
        revision: memberRevision,
        describedMembers: (parse) =>
          queryClient.getQueryData<StackMembersAnswer | null>(
            pullRequestStackMembersQueryKey(
              conversationId,
              describedStackIdentity(parse),
              stackMemberLookups(parse),
            ),
          ) ?? undefined,
        now: Date.now(),
      },
      selectInPlace,
    );
  }, [
    arrivalSnapshot,
    self,
    memberRevision,
    conversationId,
    body,
    headRef,
    queryClient,
    selectInPlace,
  ]);
  // Without another member's record, the stack the list nests the PR in, from its first frame,
  // and while the PR's own stack still reads its members.
  const ownMembersLoading = own?.members.kind === "loading";
  // GitHub's own read stops at its first entries; the list's whole read of the same stack, agreeing
  // with every entry it lists, keeps showing the rest.
  const listedExtends =
    !waiting &&
    !!own?.partial &&
    !!listed?.native &&
    !!nativeAnswer &&
    listedStackExtends(listed.native, nativeAnswer);
  const listedView = useMemo(
    () =>
      (waiting || ownMembersLoading || listedExtends) && self && hasPr && listed
        ? listedStackView(listed, { self, body, headRef }, selectInPlace)
        : null,
    [waiting, ownMembersLoading, listedExtends, self, hasPr, listed, body, headRef, selectInPlace],
  );
  const arrival = waiting ? (recorded ?? listedView) : null;

  return useMemo(() => {
    const withSubtab = (view: PullRequestStackView | null) =>
      view && subtab ? { ...view, subtab } : view;
    // The list's agreeing stack stands in only for the statuses the PR's own read still reads.
    const standIn =
      (own && ownMembersLoading && listedView && sameStackRows(own, listedView)) ||
      (listedExtends && listedView && !listedView.partial)
        ? listedView
        : (own ?? arrival);
    // Another read's partial stack stands in while GitHub's own answer may bring the rest.
    const shown = waiting && standIn?.partial ? { ...standIn, completing: true } : standIn;
    return {
      stack: withSubtab(own),
      waiting,
      arrival: withSubtab(arrival),
      shown: withSubtab(shown),
      loading: waiting && !shown && described !== null,
    };
  }, [own, ownMembersLoading, listedExtends, listedView, waiting, arrival, subtab, described]);
}
