// Nesting a list's rows into their stacks: GitHub's own where its read says the PR has one,
// else the one its description describes. A row sits under another only when that edge is
// certain, from GitHub's positions or both descriptions agreeing, and both rows are in the same
// group; any other stacked row stays where it is, with a hint saying why.

import type { NativeStackRead } from "@/lib/pullRequestStacks/nativeStack";
import {
  type StackParse,
  type StackPullRequestRef,
  stackPullRequestRef,
  stackRefKey,
} from "@/lib/pullRequestStacks/stackModel";
import { treeGuides, type TreeGuides } from "@/lib/pullRequestStacks/stackNavigation";

/** GitHub's own stack for a row, whole or in part. */
export type NativeListStack = Extract<NativeStackRead, { kind: "stacked" | "stacked-partial" }>;

/** What a row's stack is, as far as this list read it. */
export type ListStackRead =
  /**
   * Read: its stack, or null when it lists none. `githubUnstacked` where GitHub's own read
   * says the PR isn't in a stack.
   */
  | { kind: "read"; parse: StackParse | null; githubUnstacked?: boolean }
  /** GitHub says it's in a stack: that decides, whatever its description says. */
  | { kind: "native"; stack: NativeListStack }
  /** Not read, or no usable description: it confirms nothing, for this row or one naming it. */
  | { kind: "unread"; githubUnstacked?: boolean };

export interface ListStackGroup {
  id: string;
  title: string;
  /** The group's rows in display order. */
  urls: readonly string[];
}

/** Why a stacked row isn't nested under its parent. */
export type ListStackHint =
  /** The parent isn't a row of this list now: filtered, hidden, in another tab, or not loaded. */
  | { kind: "absent"; parent: StackPullRequestRef }
  | { kind: "other-group"; parent: StackPullRequestRef; groupTitle: string }
  | { kind: "unpushed"; branch: string }
  | { kind: "external"; parent: StackPullRequestRef }
  /** The parent is listed here, but its own stack, as `source` keeps it, wasn't read. */
  | { kind: "unconfirmed"; parent: StackPullRequestRef; source: "description" | "github" }
  /**
   * The parent's own stack doesn't put this row directly above it: `descriptions` where both
   * descriptions disagree, `github` where GitHub's reads of the two disagree, and `mixed` where
   * this row's description names a parent GitHub stacks otherwise.
   */
  | {
      kind: "conflict";
      parent: StackPullRequestRef;
      source: "descriptions" | "github" | "mixed";
    }
  /**
   * In GitHub's stack, above a PR its read couldn't show: this row's `position` of `size`, each
   * null where GitHub didn't say.
   */
  | { kind: "native-gap"; position: number | null; size: number | null };

/** One row, in tree order, and how it's drawn. */
export interface ListStackPlacement extends TreeGuides {
  url: string;
  /** 0 for a row nested under nothing. */
  depth: number;
  parentUrl: string | null;
  /** Rows nested under this one, at any depth. */
  descendants: number;
  hint: ListStackHint | null;
  /** Its own stack info is only tolerated, and it shows as part of a stack. */
  tolerated: boolean;
}

const READ_NOTHING: ListStackRead = { kind: "unread" };

/** This PR's position in GitHub's stack, or null where the read can't say. */
function nativePosition(stack: NativeListStack): number | null {
  return stack.kind === "stacked" ? stack.parse.selfIndex + 1 : stack.position;
}

/** The PR GitHub lists at `position`, where its read showed one. */
function nativeEntryAt(stack: NativeListStack, position: number) {
  return stack.entries.find((entry) => entry.position === position) ?? null;
}

/** Whether GitHub's `stack` lists `childKey` directly above the PR it was read for. */
function nativeListsDirectlyAbove(stack: NativeListStack, childKey: string): boolean {
  const position = nativePosition(stack);
  const above = position === null ? null : nativeEntryAt(stack, position + 1);
  return !!above && stackRefKey(above.ref) === childKey;
}

function rowKey(url: string): string | null {
  const ref = stackPullRequestRef(url);
  return ref && stackRefKey(ref);
}

/** Where `parse` lists `childKey` first, if that entry sits directly below its own PR. */
function listsDirectlyBelow(parse: StackParse, childKey: string): boolean {
  const index = parse.nodes.findIndex((node) => node.ref && stackRefKey(node.ref) === childKey);
  return index !== -1 && parse.nodes[index]!.parentIndex === parse.selfIndex;
}

/** The position `parse` lists `childKey` at, for ordering siblings as the parent does. */
function listedAt(parse: StackParse | null, childKey: string | null): number {
  if (!parse || childKey === null) return Number.POSITIVE_INFINITY;
  const index = parse.nodes.findIndex((node) => node.ref && stackRefKey(node.ref) === childKey);
  return index === -1 ? Number.POSITIVE_INFINITY : index;
}

/**
 * Each group's rows in tree order: a parent first, then the rows nested under it, in the
 * order its description lists them, else in display order.
 */
export function nestStackGroups(
  groups: readonly ListStackGroup[],
  readOf: (url: string) => ListStackRead,
): ListStackPlacement[][] {
  const located = new Map<string, { url: string; group: number }>();
  groups.forEach((group, groupIndex) => {
    for (const url of group.urls) {
      const key = rowKey(url);
      if (key !== null && !located.has(key)) located.set(key, { url, group: groupIndex });
    }
  });
  // Each row GitHub stacks, by its stack and own position: a long stack's read lists only its
  // first entries and the row's own, so the row below is found by its own read instead. A
  // position two different PRs claim is disputed, and stands for no row.
  const nativeRowAt = new Map<string, string | null>();
  for (const group of groups) {
    for (const url of group.urls) {
      const read = readOf(url) ?? READ_NOTHING;
      const position = read.kind === "native" ? nativePosition(read.stack) : null;
      if (read.kind !== "native" || position === null || read.stack.key === null) continue;
      const at = `${read.stack.key}#${position}`;
      const claimed = nativeRowAt.get(at);
      if (claimed === undefined) nativeRowAt.set(at, url);
      else if (claimed !== null && rowKey(claimed) !== rowKey(url)) nativeRowAt.set(at, null);
    }
  }
  const parseOf = (url: string) => {
    const read = readOf(url) ?? READ_NOTHING;
    return read.kind === "read" ? read.parse : null;
  };

  return groups.map((group, groupIndex) => {
    const parentOf = new Map<string, string>();
    const hints = new Map<string, ListStackHint>();
    /** Nests a row GitHub stacks under the row at the position below it, or says why not. */
    const placeNative = (url: string, stack: NativeListStack): ListStackHint | null => {
      const position = nativePosition(stack);
      if (position === null) return { kind: "native-gap", position: null, size: stack.size };
      if (position === 1) return null;
      const belowRow =
        stack.key === null ? undefined : nativeRowAt.get(`${stack.key}#${position - 1}`);
      const parent =
        nativeEntryAt(stack, position - 1)?.ref ??
        (belowRow ? stackPullRequestRef(belowRow) : null);
      if (!parent) return { kind: "native-gap", position, size: stack.size };
      const at = located.get(stackRefKey(parent));
      if (!at) return { kind: "absent", parent };
      if (at.group !== groupIndex) {
        return { kind: "other-group", parent, groupTitle: groups[at.group]!.title };
      }
      if (at.url === url) return { kind: "conflict", parent, source: "github" };
      // Only the parent row's own read of GitHub's stack can confirm the edge: the same stack,
      // the parent just below, and no other PR between them.
      const theirs = readOf(at.url) ?? READ_NOTHING;
      if (theirs.kind !== "native") {
        return theirs.githubUnstacked
          ? { kind: "conflict", parent, source: "github" }
          : { kind: "unconfirmed", parent, source: "github" };
      }
      const childKey = rowKey(url);
      const aboveParent = nativeEntryAt(theirs.stack, position);
      // Where the parent's read lists this PR at all, it must be at the same position.
      const childInTheirs =
        childKey === null
          ? null
          : theirs.stack.entries.find((entry) => stackRefKey(entry.ref) === childKey);
      const agrees =
        theirs.stack.key === stack.key &&
        nativePosition(theirs.stack) === position - 1 &&
        (!childInTheirs || childInTheirs.position === position) &&
        (aboveParent === null || (childKey !== null && stackRefKey(aboveParent.ref) === childKey));
      if (!agrees) return { kind: "conflict", parent, source: "github" };
      parentOf.set(url, at.url);
      return null;
    };
    for (const url of group.urls) {
      const own = readOf(url) ?? READ_NOTHING;
      if (own.kind === "native") {
        const hint = placeNative(url, own.stack);
        if (hint) hints.set(url, hint);
        continue;
      }
      const parse = parseOf(url);
      if (!parse) continue;
      const parentIndex = parse.nodes[parse.selfIndex]!.parentIndex;
      if (parentIndex === null) continue;
      const parentNode = parse.nodes[parentIndex]!;
      const parent = parentNode.ref;
      if (!parent) {
        hints.set(url, { kind: "unpushed", branch: parentNode.branch });
        continue;
      }
      if (parentNode.external) {
        hints.set(url, { kind: "external", parent });
        continue;
      }
      const at = located.get(stackRefKey(parent));
      if (!at) {
        hints.set(url, { kind: "absent", parent });
      } else if (at.group !== groupIndex) {
        hints.set(url, { kind: "other-group", parent, groupTitle: groups[at.group]!.title });
      } else if (at.url === url) {
        hints.set(url, { kind: "conflict", parent, source: "descriptions" });
      } else if ((readOf(at.url) ?? READ_NOTHING).kind === "native") {
        // A description naming a parent in GitHub's stack agrees only where GitHub does.
        const theirs = readOf(at.url) as Extract<ListStackRead, { kind: "native" }>;
        const key = rowKey(url);
        if (key !== null && nativeListsDirectlyAbove(theirs.stack, key)) {
          parentOf.set(url, at.url);
        } else {
          hints.set(url, { kind: "conflict", parent, source: "mixed" });
        }
      } else if ((readOf(at.url) ?? READ_NOTHING).kind !== "read") {
        hints.set(url, { kind: "unconfirmed", parent, source: "description" });
      } else {
        const parentParse = parseOf(at.url);
        const key = rowKey(url);
        if (parentParse && key !== null && listsDirectlyBelow(parentParse, key)) {
          parentOf.set(url, at.url);
        } else {
          hints.set(url, { kind: "conflict", parent, source: "descriptions" });
        }
      }
    }

    // Two descriptions can agree on a loop; it's broken at the row shown first. Each row is
    // walked once, so a long chain costs no more than its length.
    const order = new Map(group.urls.map((url, index) => [url, index]));
    const walked = new Set<string>();
    for (const url of group.urls) {
      const path: string[] = [];
      const onPath = new Set<string>();
      let at: string | undefined = url;
      while (at !== undefined && !walked.has(at) && !onPath.has(at)) {
        path.push(at);
        onPath.add(at);
        at = parentOf.get(at);
      }
      for (const visited of path) walked.add(visited);
      if (at === undefined || !onPath.has(at)) continue;
      const loop = path.slice(path.indexOf(at));
      const first = loop.reduce((best, candidate) =>
        order.get(candidate)! < order.get(best)! ? candidate : best,
      );
      const parent = stackPullRequestRef(parentOf.get(first)!);
      parentOf.delete(first);
      const source = (readOf(first) ?? READ_NOTHING).kind === "native" ? "github" : "descriptions";
      if (parent) hints.set(first, { kind: "conflict", parent, source });
    }

    const children = new Map<string, string[]>();
    for (const url of group.urls) {
      const parent = parentOf.get(url);
      if (parent === undefined) continue;
      const siblings = children.get(parent);
      if (siblings) siblings.push(url);
      else children.set(parent, [url]);
    }
    for (const [parent, list] of children) {
      const parentParse = parseOf(parent);
      list.sort(
        (left, right) =>
          listedAt(parentParse, rowKey(left)) - listedAt(parentParse, rowKey(right)) ||
          order.get(left)! - order.get(right)!,
      );
    }

    const placed: Omit<ListStackPlacement, keyof TreeGuides>[] = [];
    const parentIndexes: (number | null)[] = [];
    // Parent first, then its rows in order, walked without recursion so a deep chain fits.
    const pending: { url: string; depth: number; parentIndex: number | null }[] = [];
    const roots = group.urls.filter((url) => !parentOf.has(url));
    for (let rootIndex = roots.length - 1; rootIndex >= 0; rootIndex -= 1) {
      pending.push({ url: roots[rootIndex]!, depth: 0, parentIndex: null });
    }
    while (pending.length > 0) {
      const { url, depth, parentIndex } = pending.pop()!;
      const index = placed.length;
      const parse = parseOf(url);
      const nested = children.get(url) ?? [];
      const hint = hints.get(url) ?? null;
      placed.push({
        url,
        depth,
        parentUrl: parentOf.get(url) ?? null,
        descendants: 0,
        hint,
        tolerated:
          parse?.trust === "tolerated" && (parentOf.has(url) || nested.length > 0 || hint !== null),
      });
      parentIndexes.push(parentIndex);
      for (let childIndex = nested.length - 1; childIndex >= 0; childIndex -= 1) {
        pending.push({ url: nested[childIndex]!, depth: depth + 1, parentIndex: index });
      }
    }
    // Children follow their parent, so counting from the end adds each subtree once.
    for (let index = placed.length - 1; index >= 0; index -= 1) {
      const parentIndex = parentIndexes[index];
      if (parentIndex !== null && parentIndex !== undefined) {
        placed[parentIndex]!.descendants += placed[index]!.descendants + 1;
      }
    }
    const guides = treeGuides(
      placed.map((row, index) => ({ parentIndex: parentIndexes[index]!, depth: row.depth })),
    );
    return placed.map((row, index) => ({ ...row, ...guides[index]! }));
  });
}

/** The rows shown once the stacks under `collapsed` rows fold away. */
export function withoutCollapsedStacks(
  rows: readonly ListStackPlacement[],
  collapsed: (url: string) => boolean,
): ListStackPlacement[] {
  const shown: ListStackPlacement[] = [];
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index]!;
    shown.push(row);
    if (row.descendants > 0 && collapsed(row.url)) index += row.descendants;
  }
  return shown;
}
