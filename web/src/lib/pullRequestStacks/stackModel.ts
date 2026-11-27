// What a PR description says about the stack the PR belongs to: a tree of branches,
// each with the PR it was pushed as, if any. Formats only read syntax into an outline;
// this module decides parentage, which node is this PR, and how far the result is trusted.

import { pullRequestPageUrl } from "@/lib/pullRequestReadRoute";

/** A PR a stack entry links to, named by its full identity. */
export interface StackPullRequestRef {
  url: string;
  host: string;
  repository: string;
  number: number;
}

/** The PR whose description is read, as the page routed to it. */
export interface StackSelf {
  host: string;
  repository: string;
  number: number;
}

export interface StackNodeMarkers {
  /** The description marks this entry as the PR it belongs to. */
  current: boolean;
  /** The description says this entry was merged; a hint only, since it goes stale. */
  merged: boolean;
}

export interface StackNode {
  /** 0 for a stack root. */
  depth: number;
  /** Index of the parent in `nodes`, or null for a root. */
  parentIndex: number | null;
  branch: string;
  /** Null for a branch that hasn't been pushed as a PR. */
  ref: StackPullRequestRef | null;
  /** The PR lives in another repository or on another host, so it's only ever linked to. */
  external: boolean;
  markers: StackNodeMarkers;
}

/** Offsets of the stack metadata in the description: `[start, end)`, end past its last line break. */
export interface StackSpan {
  start: number;
  end: number;
}

/** Why a parse isn't trusted enough to strip, rewrite or compare against its metadata. */
export type StackTolerance =
  | "header"
  | "footer"
  | "unexpected-line"
  | "indentation"
  | "link"
  | "dependency"
  | "duplicate"
  | "current-marker";

interface StackParseBase {
  format: string;
  /** Members base first, in the description's order, so a parent precedes its children. */
  nodes: StackNode[];
  /** A PR outside the stack that must land before it, listed above the stack. */
  dependency: StackNode | null;
  /** Index of this PR in `nodes`. */
  selfIndex: number;
}

/** The metadata matches its format exactly, so its span can be stripped and spliced. */
export interface VerifiedStackParse extends StackParseBase {
  trust: "verified";
  span: StackSpan;
}

/** Good enough to navigate by, but nothing may rewrite or hide the description around it. */
export interface ToleratedStackParse extends StackParseBase {
  trust: "tolerated";
  tolerances: StackTolerance[];
}

/** GitHub's own record of the stack: authoritative, with no description metadata to touch. */
export interface NativeStackParse extends StackParseBase {
  trust: "native";
}

export type StackParse = VerifiedStackParse | ToleratedStackParse | NativeStackParse;

/** The tool that writes a format's stacks, for the landing advice the merge area copies. */
export interface StackLandingTool {
  /** The tool's name, as in "your <name> checkout of this stack". */
  name: string;
  /** How advice names its land command, its land-check command and its sync command. */
  landName: string;
  checkName: string;
  syncName: string;
  /** Lands every PR from the bottom of the stack up to `branch`; with `check`, only checks each could. */
  landCommand: (branch: string, options: { check: boolean }) => string;
  /** Drops a merged parent's commits from the current branch, then pushes it. */
  syncCommand: string;
}

export interface StackFormat {
  id: string;
  /** Null when the description carries no stack in this format. */
  parse: (body: string, self: StackSelf) => StackParse | null;
  /** How a verified stack in this format lands, when its tool lands stacks itself. */
  landing?: StackLandingTool;
}

/** One entry as a format read it: its indentation, before any parentage is decided. */
export interface StackOutlineEntry {
  indent: number;
  branch: string;
  /** The entry's PR link as written, or null for an unpushed branch. */
  url: string | null;
  markers: StackNodeMarkers;
}

export interface StackOutline {
  format: string;
  /** The dependency entry, already set apart from the members by the format. */
  dependency: StackOutlineEntry | null;
  members: StackOutlineEntry[];
  /** Null when the format couldn't bound its metadata exactly. */
  span: StackSpan | null;
  tolerances: StackTolerance[];
}

const PULL_PAGE = /^https:\/\/([^/]+)\/([^/]+)\/([^/]+)\/pull\/([1-9][0-9]*)$/;

/**
 * The PR a link names, or null unless it is exactly a PR's page. Another port or
 * credentials would name a different server, and a subpage, query or fragment isn't
 * what a stack tool writes.
 */
export function stackPullRequestRef(url: string): StackPullRequestRef | null {
  const page = pullRequestPageUrl(url);
  if (page === null || page.toLowerCase() !== url.replace(/\/$/, "").toLowerCase()) return null;
  const match = PULL_PAGE.exec(page);
  if (!match) return null;
  const [, host = "", owner, name, number] = match;
  return { url, host, repository: `${owner}/${name}`, number: Number(number) };
}

const sameRepository = (ref: StackPullRequestRef, self: StackSelf) =>
  ref.host === self.host.toLowerCase() &&
  ref.repository.toLowerCase() === self.repository.toLowerCase();

/** A PR's full identity as one string: host, repository and number, case-insensitive. */
export const stackRefKey = (ref: StackPullRequestRef) =>
  `${ref.host.toLowerCase()}\n${ref.repository.toLowerCase()}\n${ref.number}`;

/**
 * Decides each member's parent from indentation alone, unpushed branches included, so a PR
 * stacked on an unpushed branch never skips to the PR below it.
 */
function outlineTopology(
  entries: readonly StackOutlineEntry[],
): Pick<StackNode, "depth" | "parentIndex">[] {
  const open: { indent: number; index: number }[] = [];
  return entries.map((entry, index) => {
    while (open.length > 0 && (open.at(-1)?.indent ?? 0) >= entry.indent) open.pop();
    const parentIndex = open.at(-1)?.index ?? null;
    open.push({ indent: entry.indent, index });
    return { depth: open.length - 1, parentIndex };
  });
}

/**
 * Resolves a format's outline against the PR it was read from. A description that doesn't
 * list that PR isn't its stack, whatever it marks as current.
 */
export function resolveStackOutline(outline: StackOutline, self: StackSelf): StackParse | null {
  const tolerances = new Set(outline.tolerances);
  const toNode = (
    entry: StackOutlineEntry,
    topology: Pick<StackNode, "depth" | "parentIndex">,
  ): StackNode => {
    const ref = entry.url === null ? null : stackPullRequestRef(entry.url);
    if (entry.url !== null && ref === null) tolerances.add("link");
    return {
      ...topology,
      branch: entry.branch,
      ref,
      external: ref !== null && !sameRepository(ref, self),
      markers: entry.markers,
    };
  };

  const nodes = outlineTopology(outline.members).map((topology, index) =>
    toNode(outline.members[index] as StackOutlineEntry, topology),
  );
  const dependency =
    outline.dependency === null
      ? null
      : toNode(outline.dependency, { depth: 0, parentIndex: null });

  const isSelf = (node: StackNode) =>
    node.ref !== null && !node.external && node.ref.number === self.number;
  const selfIndex = nodes.findIndex(isSelf);
  if (selfIndex === -1) return null;

  const seen = new Set<string>();
  for (const node of dependency ? [dependency, ...nodes] : nodes) {
    if (node.ref === null) continue;
    const key = stackRefKey(node.ref);
    if (seen.has(key)) tolerances.add("duplicate");
    seen.add(key);
  }
  if (nodes.some((node, index) => node.markers.current && index !== selfIndex)) {
    tolerances.add("current-marker");
  }
  if (dependency?.markers.current) tolerances.add("current-marker");

  // Without exact bounds the metadata can't be spliced, even when every entry read cleanly.
  if (outline.span === null && tolerances.size === 0) tolerances.add("footer");

  const base = { format: outline.format, nodes, dependency, selfIndex };
  if (tolerances.size === 0 && outline.span !== null) {
    return { ...base, trust: "verified", span: outline.span };
  }
  return { ...base, trust: "tolerated", tolerances: [...tolerances] };
}
