// GitHub's own record of the stack a PR is in, read into the shared stack model. GitHub is
// authoritative when it answers: a complete answer becomes a stack, and an incomplete one keeps
// only what it said, never parents or counts it didn't read.

import type { StackMember } from "./stackMembers";
import {
  type NativeStackParse,
  type StackNode,
  type StackPullRequestRef,
  type StackSelf,
  stackPullRequestRef,
  stackRefKey,
} from "./stackModel";

/** A stack member as GitHub's answer lists it. */
export interface NativeStackMember {
  url: string;
  number: number;
  state: string;
  isDraft: boolean;
  headRefName: string;
  /** The member as the stack members read gives it, for its status and its head commit. */
  details?: StackMember;
}

/** GitHub's answer for one PR: its own entry and its stack, each null when it has none. */
export interface NativeStackAnswer {
  stackEntry: { position: number } | null;
  stack: {
    id: string;
    number: number;
    size: number;
    baseRefName: string;
    entries: {
      hasNextPage: boolean;
      nodes: { position: number; pullRequest: NativeStackMember | null }[];
    };
  } | null;
}

/** Why GitHub's stack for a PR couldn't be read whole. */
export type NativeStackGap =
  /** More entries are on GitHub than one read lists. */
  | "more-entries"
  /** Fewer entries were listed than the stack's size. */
  | "missing-entries"
  /** An entry's PR isn't visible to this account, or no longer exists. */
  | "invisible-member"
  /** Positions repeat, skip, or fall outside the stack. */
  | "positions"
  /** An entry's link isn't a PR in this repository. */
  | "member-link"
  /** The same PR is listed at more than one position. */
  | "duplicate-member"
  /** This PR isn't among the entries, or its own position disagrees with them. */
  | "self-missing";

/** An entry read with its PR. */
export interface NativeStackEntry {
  position: number;
  ref: StackPullRequestRef;
  member: NativeStackMember;
}

export type NativeStackRead =
  /** The host or shell can't say: today's description path decides. */
  | { kind: "unsupported" }
  /** GitHub says this PR isn't in a stack. */
  | { kind: "not-stacked" }
  | {
      kind: "stacked";
      key: string;
      parse: NativeStackParse;
      /** Every entry, in position order, each read with its PR. */
      entries: NativeStackEntry[];
      size: number;
      baseRefName: string;
      fingerprint: string;
    }
  | {
      kind: "stacked-partial";
      /** Null when GitHub named this PR's position but not its stack. */
      key: string | null;
      size: number | null;
      baseRefName: string | null;
      /** This PR's own position, null unless the answer agrees with itself on it. */
      position: number | null;
      /** The entries read with a PR, in position order; only these may be navigated to. */
      entries: NativeStackEntry[];
      gaps: NativeStackGap[];
      fingerprint: string;
    };

/** A stack's identity: GitHub's node id on its host, never its per-repository number. */
export function nativeStackKey(host: string, stackId: string): string {
  return `${host.toLowerCase()}\n${stackId}`;
}

const sameRepository = (ref: StackPullRequestRef, self: StackSelf) =>
  ref.host === self.host.toLowerCase() &&
  ref.repository.toLowerCase() === self.repository.toLowerCase();

/**
 * Everything a stack's view depends on, so a retarget, a reorder, a move of this PR, or a
 * member merged or added reads again even when this PR's head and description are the same.
 */
export function nativeStackFingerprint(answer: NativeStackAnswer | undefined): string {
  if (answer === undefined) return "unsupported";
  const { stack, stackEntry } = answer;
  if (stack === null && stackEntry === null) return "none";
  // Each member by the identity the read checks, so fixing a link or a duplicate reads again.
  const identity = (member: NativeStackMember) => {
    const ref = stackPullRequestRef(member.url);
    return ref ? stackRefKey(ref).replaceAll("\n", "/") : `unparsed:${member.url}`;
  };
  const entries = (stack?.entries.nodes ?? [])
    .map(({ position, pullRequest }) =>
      pullRequest
        ? `${position}:${pullRequest.number}:${identity(pullRequest)}:${pullRequest.state}:${pullRequest.isDraft ? "draft" : "ready"}`
        : `${position}:-`,
    )
    .sort();
  return [
    stack
      ? `${stack.id}:${stack.size}:${stack.baseRefName}:${stack.entries.hasNextPage ? "more" : "all"}`
      : "-",
    `self:${stackEntry?.position ?? "-"}`,
    entries.join(","),
  ].join("|");
}

/** What GitHub's `answer` says about the stack `self` is in, as the page and list may use it. */
export function readNativeStack(
  answer: NativeStackAnswer | undefined,
  self: StackSelf,
): NativeStackRead {
  if (answer === undefined) return { kind: "unsupported" };
  const { stack, stackEntry } = answer;
  if (stack === null && stackEntry === null) return { kind: "not-stacked" };
  const fingerprint = nativeStackFingerprint(answer);
  const gaps = new Set<NativeStackGap>();
  const named = stackEntry?.position ?? null;
  if (stack === null) {
    return {
      kind: "stacked-partial",
      key: null,
      size: null,
      baseRefName: null,
      position: named !== null && Number.isInteger(named) && named >= 1 ? named : null,
      entries: [],
      gaps: ["missing-entries"],
      fingerprint,
    };
  }

  const nodes = [...stack.entries.nodes].sort((left, right) => left.position - right.position);
  if (stack.entries.hasNextPage) gaps.add("more-entries");
  if (nodes.length < stack.size) gaps.add("missing-entries");
  // A position that repeats or falls outside the stack can't say where its PR sits.
  const seen = new Map<number, number>();
  for (const node of nodes) seen.set(node.position, (seen.get(node.position) ?? 0) + 1);
  const certain = (value: number) =>
    Number.isInteger(value) && value >= 1 && value <= stack.size && seen.get(value) === 1;
  if (nodes.some((node) => !certain(node.position))) gaps.add("positions");

  const candidates: NativeStackEntry[] = [];
  for (const node of nodes) {
    if (!certain(node.position)) continue;
    if (!node.pullRequest) {
      gaps.add("invisible-member");
      continue;
    }
    const ref = stackPullRequestRef(node.pullRequest.url);
    if (!ref || !sameRepository(ref, self) || ref.number !== node.pullRequest.number) {
      gaps.add("member-link");
      continue;
    }
    candidates.push({ position: node.position, ref, member: node.pullRequest });
  }
  // A PR listed twice can't say which place is its own: neither is kept.
  const listed = new Map<string, number>();
  for (const entry of candidates) {
    const key = stackRefKey(entry.ref);
    listed.set(key, (listed.get(key) ?? 0) + 1);
  }
  const entries = candidates.filter((entry) => listed.get(stackRefKey(entry.ref)) === 1);
  if (entries.length !== candidates.length) gaps.add("duplicate-member");
  const selfPosition =
    entries.find((entry) => entry.ref.number === self.number && sameRepository(entry.ref, self))
      ?.position ?? null;
  if (selfPosition === null || named === null || selfPosition !== named) {
    gaps.add("self-missing");
  }
  // This PR's position counts only where nothing in the answer contradicts it: in range,
  // listed nowhere else, and any entry at it is this PR.
  const isSelf = (member: NativeStackMember | null) => {
    const ref = member && stackPullRequestRef(member.url);
    return !!ref && sameRepository(ref, self) && ref.number === self.number;
  };
  const position =
    named !== null &&
    Number.isInteger(named) &&
    named >= 1 &&
    named <= stack.size &&
    nodes.every((node) =>
      isSelf(node.pullRequest) ? node.position === named : node.position !== named,
    ) &&
    nodes.filter((node) => isSelf(node.pullRequest)).length <= 1
      ? named
      : null;

  const key = nativeStackKey(self.host, stack.id);
  if (gaps.size > 0 || entries.length !== stack.size) {
    return {
      kind: "stacked-partial",
      key,
      size: stack.size,
      baseRefName: stack.baseRefName,
      position,
      entries,
      gaps: [...gaps],
      fingerprint,
    };
  }
  // Complete: positions 1..size each once, so each entry stacks on the one before it.
  const stackNodes: StackNode[] = entries.map((entry, index) => ({
    depth: index,
    parentIndex: index === 0 ? null : index - 1,
    branch: entry.member.headRefName,
    ref: entry.ref,
    external: false,
    markers: {
      current: entry.ref.number === self.number,
      merged: entry.member.state === "MERGED",
    },
  }));
  return {
    kind: "stacked",
    key,
    parse: {
      format: "github-native",
      trust: "native",
      nodes: stackNodes,
      dependency: null,
      selfIndex: stackNodes.findIndex((node) => node.markers.current),
    },
    entries,
    size: stack.size,
    baseRefName: stack.baseRefName,
    fingerprint,
  };
}

/** Whether a partial read has this PR's neighbor on one side, ends there, or didn't read it. */
export type NativeStackNeighbor = "read" | "end" | "unread";

/** What a stack read in part can still show and be moved through. */
export interface PartialNativeStack {
  /** This PR with the entry directly below it and the one directly above it, where read. */
  parse: NativeStackParse;
  /** GitHub's position of this PR and its stack's size, each null when unknown. */
  position: number | null;
  size: number | null;
  /** GitHub's position of each node of `parse`, for numbering its rows. */
  rowNumbers: (number | null)[];
  below: NativeStackNeighbor;
  above: NativeStackNeighbor;
}

/**
 * What a partial read can still be moved through: this PR, and its neighbors only where its
 * own position is certain and each was read with its PR. Nothing further is guessed.
 */
export function partialNativeStack(
  read: Extract<NativeStackRead, { kind: "stacked-partial" }>,
  self: StackPullRequestRef,
  selfBranch: string,
): PartialNativeStack {
  const { position, size } = read;
  const neighbor = (at: number) =>
    position === null
      ? undefined
      : read.entries.find(
          (entry) => entry.position === at && stackRefKey(entry.ref) !== stackRefKey(self),
        );
  const below = position === null ? undefined : neighbor(position - 1);
  const above = position === null ? undefined : neighbor(position + 1);
  const window: {
    ref: StackPullRequestRef;
    branch: string;
    position: number | null;
    current: boolean;
    merged: boolean;
  }[] = [];
  if (below) {
    window.push({
      ref: below.ref,
      branch: below.member.headRefName,
      position: below.position,
      current: false,
      merged: below.member.state === "MERGED",
    });
  }
  window.push({ ref: self, branch: selfBranch, position, current: true, merged: false });
  if (above) {
    window.push({
      ref: above.ref,
      branch: above.member.headRefName,
      position: above.position,
      current: false,
      merged: above.member.state === "MERGED",
    });
  }
  const nodes: StackNode[] = window.map((entry, depth) => ({
    depth,
    parentIndex: depth === 0 ? null : depth - 1,
    branch: entry.branch,
    ref: entry.ref,
    external: false,
    markers: { current: entry.current, merged: entry.merged },
  }));
  return {
    parse: {
      format: "github-native",
      trust: "native",
      nodes,
      dependency: null,
      selfIndex: below ? 1 : 0,
    },
    position,
    size,
    rowNumbers: window.map((entry) => entry.position),
    below: below ? "read" : position === 1 ? "end" : "unread",
    above: above ? "read" : position !== null && position === size ? "end" : "unread",
  };
}
