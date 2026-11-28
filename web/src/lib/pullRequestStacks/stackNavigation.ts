// Moving through a stack from the PR on screen: where it stands, its parent, its children, and
// the tree a stack card draws. Only PRs in the same repository are ever opened in place.

import type { StackNode, StackParse } from "@/lib/pullRequestStacks/stackModel";

/** The deepest level the tree indents to; deeper entries name their level instead. */
export const MAX_STACK_INDENT = 4;

/** Where this PR sits: its level, base first, of the stack's levels; null where unknown. */
export interface StackPosition {
  level: number | null;
  levels: number | null;
}

/** A stack entry this PR's page could move to. */
export interface StackDestination {
  index: number;
  node: StackNode;
  /** The PR to open in place, or null when it can't be. */
  url: string | null;
  /** Why it can't be opened, when `url` is null. */
  unavailableReason: string | null;
}

export type StackStep =
  | { kind: "go"; destination: StackDestination }
  /** Several PRs are stacked on this one: the person picks, never the app. */
  | { kind: "choose"; destinations: StackDestination[] }
  | { kind: "unavailable"; reason: string };

export const PARENT_NOT_PUSHED = "The PR below this one isn't pushed yet";
export const AT_STACK_TOP = "This PR is at the top of its stack";
export const END_NOT_READ = "That end of the stack couldn't be read here";
export const AT_STACK_BASE = "This PR is at the base of its stack";
export const NOTHING_STACKED = "No PR is stacked on this one";
export const CHILD_NOT_PUSHED = "The PR stacked on this one isn't pushed yet";
export const IN_OTHER_REPOSITORY = "That PR is in another repository";
export const PARENT_NOT_READ = "The PR below this one couldn't be read here";
export const CHILD_NOT_READ = "The PR above this one couldn't be read here";
export const POSITION_NOT_READ = "Where this PR sits in its stack couldn't be read here";

/** How a stack's position reads, leaving out what isn't known. */
export function stackPositionName({ level, levels }: StackPosition): string {
  if (level === null) return "Stack";
  return levels === null ? `Stack: level ${level}` : `Stack: level ${level} of ${levels}`;
}

export function stackPosition(parse: StackParse): StackPosition {
  const deepest = Math.max(...parse.nodes.map((node) => node.depth));
  return { level: parse.nodes[parse.selfIndex].depth + 1, levels: deepest + 1 };
}

export const NOT_PUSHED = "Not pushed yet";

function destination(parse: StackParse, index: number): StackDestination {
  const node = parse.nodes[index];
  if (!node.ref) return { index, node, url: null, unavailableReason: NOT_PUSHED };
  if (node.external) return { index, node, url: null, unavailableReason: IN_OTHER_REPOSITORY };
  return { index, node, url: node.ref.url, unavailableReason: null };
}

/** The direct parent, which this PR is stacked on. */
export function previousInStack(parse: StackParse): StackStep {
  const parentIndex = parse.nodes[parse.selfIndex].parentIndex;
  if (parentIndex === null) return { kind: "unavailable", reason: AT_STACK_BASE };
  const parent = destination(parse, parentIndex);
  if (!parent.node.ref) return { kind: "unavailable", reason: PARENT_NOT_PUSHED };
  if (!parent.url) return { kind: "unavailable", reason: IN_OTHER_REPOSITORY };
  return { kind: "go", destination: parent };
}

/** The PRs stacked directly on this one, in the description's order. */
export function stackChildren(parse: StackParse): StackDestination[] {
  return parse.nodes.flatMap((node, index) =>
    node.parentIndex === parse.selfIndex ? [destination(parse, index)] : [],
  );
}

/** The only child, or a choice among several; a sibling is never picked for the person. */
export function nextInStack(parse: StackParse): StackStep {
  const children = stackChildren(parse);
  if (children.length === 0) return { kind: "unavailable", reason: NOTHING_STACKED };
  if (children.length > 1) return { kind: "choose", destinations: children };
  const [child] = children;
  if (child.url) return { kind: "go", destination: child };
  return {
    kind: "unavailable",
    reason: child.node.ref ? IN_OTHER_REPOSITORY : CHILD_NOT_PUSHED,
  };
}

/** The first entry listed, the stack's base, unless this PR is it. */
export function firstInStack(parse: StackParse): StackStep {
  return endOfStack(parse, 0, AT_STACK_BASE);
}

/** The last entry listed, the top of a straight stack, unless this PR is it. */
export function lastInStack(parse: StackParse): StackStep {
  return endOfStack(parse, parse.nodes.length - 1, AT_STACK_TOP);
}

function endOfStack(parse: StackParse, index: number, atEnd: string): StackStep {
  if (index === parse.selfIndex) return { kind: "unavailable", reason: atEnd };
  const end = destination(parse, index);
  if (!end.url) return { kind: "unavailable", reason: end.unavailableReason ?? NOT_PUSHED };
  return { kind: "go", destination: end };
}

/** How one tree row is drawn: a guide per ancestor level, then its own connector. */
export interface StackTreeRow {
  index: number;
  node: StackNode;
  /** For each indented ancestor level, whether a line carries on past this row. */
  guides: boolean[];
  /** `tee` when a sibling follows, `elbow` for the last child, null for a root. */
  connector: "tee" | "elbow" | null;
  /** Set past {@link MAX_STACK_INDENT}, where the row stops indenting and names its level. */
  deepLevel: number | null;
}

/** How a tree row is drawn, for any tree listed parent first. */
export type TreeGuides = Pick<StackTreeRow, "guides" | "connector" | "deepLevel">;

/**
 * Tree guides for `nodes`, listed so that each parent precedes its children and siblings
 * keep their order.
 */
export function treeGuides(
  nodes: readonly { parentIndex: number | null; depth: number }[],
): TreeGuides[] {
  // From the end, a node is its parent's last child unless a later one shares its parent.
  const isLastChild = new Array<boolean>(nodes.length);
  const laterParents = new Set<number | null>();
  for (let index = nodes.length - 1; index >= 0; index -= 1) {
    const parentIndex = nodes[index]!.parentIndex;
    isLastChild[index] = !laterParents.has(parentIndex);
    laterParents.add(parentIndex);
  }
  // A node's guides are its parent's, then the parent's own line, from the root down and
  // leaving out the root. Each is worked out once; a parent listed later is reached first.
  const guidesOf = new Array<boolean[] | undefined>(nodes.length);
  const guidesFor = (index: number): boolean[] => {
    const known = guidesOf[index];
    if (known) return known;
    const chain: number[] = [];
    for (let at: number | null = index; at !== null && !guidesOf[at];) {
      chain.push(at);
      at = nodes[at]!.parentIndex;
    }
    for (const at of chain.reverse()) {
      const parentIndex = nodes[at]!.parentIndex;
      let guides: boolean[] = [];
      if (parentIndex !== null && nodes[parentIndex]!.parentIndex !== null) {
        const above = guidesOf[parentIndex]!;
        guides =
          above.length >= MAX_STACK_INDENT - 1 ? above : [...above, !isLastChild[parentIndex]];
      }
      guidesOf[at] = guides;
    }
    return guidesOf[index]!;
  };
  return nodes.map((node, index) => {
    const parentIndex = node.parentIndex;
    const guides = guidesFor(index);
    const connector = parentIndex === null ? null : isLastChild[index] ? "elbow" : "tee";
    return {
      guides,
      connector,
      deepLevel: node.depth > MAX_STACK_INDENT ? node.depth + 1 : null,
    };
  });
}

/** Whether every entry has at most one entry stacked on it: a straight line, with no forks. */
export function isStraightStack(nodes: readonly { parentIndex: number | null }[]): boolean {
  const children = new Map<number | null, number>();
  for (const node of nodes) {
    const count = (children.get(node.parentIndex) ?? 0) + 1;
    if (count > 1) return false;
    children.set(node.parentIndex, count);
  }
  return true;
}

/** The stack as rows with tree guides, base first, as the description lists it. */
export function stackTreeRows(parse: StackParse): StackTreeRow[] {
  const guides = treeGuides(parse.nodes);
  return parse.nodes.map((node, index) => ({ index, node, ...guides[index]! }));
}

/** A stack longer than this folds, around the PR on screen, to the rows below. */
export const FOLD_ABOVE_ROWS = 12;
/** The rows shown on each side of the PR on screen while its stack is folded. */
export const FOLD_CONTEXT = 3;

/** What the person has opened of a folded stack: all of it, or the rows of the gaps they opened. */
export interface StackFoldState {
  expandedAll: boolean;
  /** Each opened row by {@link stackRowKey}, so a later read can't open other rows instead. */
  revealed: ReadonlySet<string>;
}

export const STACK_FOLDED: StackFoldState = { expandedAll: false, revealed: new Set() };

/** A row's identity across reads: its PR, or its branch where it has none yet. */
export function stackRowKey(node: Pick<StackNode, "ref" | "branch">): string {
  return node.ref ? node.ref.url : `branch:${node.branch}`;
}

export type StackFoldSegment<Row> =
  | { kind: "row"; row: Row }
  /** Rows folded away between two shown ones; never fewer than two. */
  | { kind: "gap"; rows: Row[]; keys: string[]; before: Row; after: Row };

/**
 * The rows a stack card shows: all of them up to {@link FOLD_ABOVE_ROWS}; past that, the first,
 * the PR at `selfAt` with {@link FOLD_CONTEXT} rows each side, the last and any opened rows,
 * with the rest as gaps. Near an end the context moves inward, so as many rows show.
 */
export function foldStackRows<Row>(
  rows: readonly Row[],
  selfAt: number,
  keyOf: (row: Row) => string,
  fold: StackFoldState,
): StackFoldSegment<Row>[] {
  const all = rows.map((row): StackFoldSegment<Row> => ({ kind: "row", row }));
  if (fold.expandedAll || rows.length <= FOLD_ABOVE_ROWS) return all;
  const last = rows.length - 1;
  const span = FOLD_CONTEXT * 2 + 1;
  const at = selfAt >= 0 && selfAt <= last ? selfAt : 0;
  const start = Math.min(Math.max(at - FOLD_CONTEXT, 1), last - span);
  const shown = rows.map(
    (row, index) =>
      index === 0 ||
      index === last ||
      (index >= start && index < start + span) ||
      fold.revealed.has(keyOf(row)),
  );
  const segments: StackFoldSegment<Row>[] = [];
  let index = 0;
  while (index <= last) {
    if (shown[index]) {
      segments.push(all[index]!);
      index += 1;
      continue;
    }
    let end = index;
    while (!shown[end]) end += 1;
    const hidden = rows.slice(index, end);
    // One row takes as much room as the gap that would stand for it.
    if (hidden.length === 1) segments.push(all[index]!);
    else {
      segments.push({
        kind: "gap",
        rows: hidden,
        keys: hidden.map(keyOf),
        before: rows[index - 1]!,
        after: rows[end]!,
      });
    }
    index = end;
  }
  return segments;
}

/** The fold with a gap's rows opened. */
export function revealStackRows(fold: StackFoldState, keys: readonly string[]): StackFoldState {
  return { ...fold, revealed: new Set([...fold.revealed, ...keys]) };
}
