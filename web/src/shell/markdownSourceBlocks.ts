// Write unedited top-level blocks back from source on save, since the serialiser
// rewrites syntax such as `*` bullets, setext headings and bare URLs.

import type { Editor, JSONContent, MarkdownToken } from "@tiptap/core";
import type { MarkdownManager } from "@tiptap/markdown";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { INLINE_TYPES } from "./TipTapGitHubAlert";

/** One top-level marked token and the number of top-level nodes it parsed to. */
interface TokenGroup {
  raw: string;
  nodes: number;
}

/** Source range of consecutive tokens and the number of document blocks they became. */
interface Segment {
  start: number;
  end: number;
  nodes: number;
}

interface ParsedSource {
  markdown: string;
  doc: JSONContent;
  source: string;
  segments: Segment[];
}

interface SourceBlocks {
  source: string;
  segments: { start: number; end: number; nodes: ProseMirrorNode[] }[];
  segmentOf: Map<ProseMirrorNode, number>;
  definitions: string;
}

/** The private parse steps of MarkdownManager that see each top-level token. */
interface ParseInternals {
  parseToken: (token: MarkdownToken, ...rest: unknown[]) => JSONContent | JSONContent[] | null;
  createImplicitEmptyParagraphsFromSpace: (token: MarkdownToken, ...rest: unknown[]) => unknown[];
}

/** The source of the first or last block in a kept run, and the nodes it parsed to. */
export interface SourceEdge {
  source: string;
  nodes: ProseMirrorNode[];
}

/** A step in writing a document: source text to keep, or a block index to serialise. */
export type SourcePlanStep = number | { source: string; head: SourceEdge; tail: SourceEdge };

// Editors parse their content before they can record it, so keep the last few parses.
const RECENT_PARSES = 4;
const recentParses: ParsedSource[] = [];
const editorBlocks = new WeakMap<Editor, SourceBlocks>();
let tracking = false;
let recording = true;

/**
 * Run a top-level parse and record how many nodes each top-level token became.
 *
 * :param manager: The editor's Markdown manager.
 * :param parse: The parse call.
 * :returns: The parsed document and the token groups, or null groups when the
 *     parse is nested or the manager's internals changed.
 */
export function trackTopLevelTokens(
  manager: MarkdownManager,
  parse: () => JSONContent,
): { doc: JSONContent; groups: TokenGroup[] | null } {
  const internals = manager as unknown as Partial<ParseInternals>;
  const { parseToken, createImplicitEmptyParagraphsFromSpace: emptyParagraphs } = internals;
  if (tracking || !parseToken || !emptyParagraphs) return { doc: parse(), groups: null };
  const groups: TokenGroup[] = [];
  let depth = 0;
  // Restore own properties too, in case a later version defines these per instance.
  const ownParseToken = Object.hasOwn(internals, "parseToken");
  const ownEmptyParagraphs = Object.hasOwn(internals, "createImplicitEmptyParagraphsFromSpace");
  internals.parseToken = function (this: ParseInternals, token, ...rest) {
    depth++;
    try {
      const result = parseToken.call(this, token, ...rest);
      const nodes = result == null ? 0 : Array.isArray(result) ? result.length : 1;
      if (depth === 1) groups.push({ raw: token.raw ?? "", nodes });
      return result;
    } finally {
      depth--;
    }
  };
  // Blank lines between top-level tokens can parse to empty paragraphs.
  internals.createImplicitEmptyParagraphsFromSpace = function (
    this: ParseInternals,
    token,
    ...rest
  ) {
    const result = emptyParagraphs.call(this, token, ...rest);
    if (depth === 0) groups.push({ raw: token.raw ?? "", nodes: result.length });
    return result;
  };
  tracking = true;
  try {
    return { doc: parse(), groups };
  } finally {
    if (ownParseToken) internals.parseToken = parseToken;
    else delete internals.parseToken;
    if (ownEmptyParagraphs) internals.createImplicitEmptyParagraphsFromSpace = emptyParagraphs;
    else delete internals.createImplicitEmptyParagraphsFromSpace;
    tracking = false;
  }
}

/**
 * Run *callback* without recording its parses for :func:`rememberSourceBlocks`.
 *
 * :param callback: Code that parses Markdown which no editor loads.
 * :returns: The callback's result.
 */
export function withoutRecording<T>(callback: () => T): T {
  const previous = recording;
  recording = false;
  try {
    return callback();
  } finally {
    recording = previous;
  }
}

/**
 * Record which source text each top-level block of a parsed document came from.
 *
 * :param markdown: The parsed Markdown.
 * :param parsed: The document as TipTap parsed it, before loose inline nodes are
 *     wrapped in paragraphs.
 * :param doc: The document the parse returned.
 * :param groups: Token groups from :func:`trackTopLevelTokens`.
 */
export function recordParse(
  markdown: string,
  parsed: JSONContent,
  doc: JSONContent,
  groups: TokenGroup[] | null,
): void {
  if (!recording) return;
  const source = markdown.replace(/\r\n|\r/g, "\n");
  const children = parsed.content ?? [];
  // Tokens must cover the source in order and account for every parsed node.
  if (!groups || groups.map((group) => group.raw).join("") !== source) return;
  const groupOf = groups.flatMap((group, index) => Array<number>(group.nodes).fill(index));
  if (groupOf.length !== children.length) return;
  // A run of loose inline nodes becomes one paragraph, spanning their tokens.
  const ranges: [number, number][] = [];
  let inlineRun: [number, number] | null = null;
  children.forEach((child, index) => {
    if (child.type != null && INLINE_TYPES.has(child.type)) {
      inlineRun = inlineRun ? [inlineRun[0], groupOf[index]] : [groupOf[index], groupOf[index]];
      return;
    }
    if (inlineRun) ranges.push(inlineRun);
    inlineRun = null;
    ranges.push([groupOf[index], groupOf[index]]);
  });
  if (inlineRun) ranges.push(inlineRun);
  if (ranges.length !== (doc.content?.length ?? 0)) return;
  const joinsNext = new Array<boolean>(groups.length).fill(false);
  for (const [first, last] of ranges)
    for (let group = first; group < last; group++) joinsNext[group] = true;
  const segmentOfGroup: number[] = [];
  const segments: Segment[] = [];
  let offset = 0;
  groups.forEach((group, index) => {
    if (index === 0 || !joinsNext[index - 1])
      segments.push({ start: offset, end: offset, nodes: 0 });
    offset += group.raw.length;
    segments[segments.length - 1].end = offset;
    segmentOfGroup.push(segments.length - 1);
  });
  for (const [first] of ranges) segments[segmentOfGroup[first]].nodes++;
  recentParses.unshift({ markdown, doc, source, segments });
  recentParses.length = Math.min(recentParses.length, RECENT_PARSES);
}

/** The source of every footnote and link definitions block, in document order. */
function definitionSources(doc: ProseMirrorNode): string {
  const sources: string[] = [];
  doc.descendants((node) => {
    if (node.type.name === "footnoteDefinitions" || node.type.name === "linkDefinitions")
      sources.push(String(node.attrs.raw ?? ""));
    return !node.isTextblock;
  });
  return sources.join("\0");
}

/**
 * Tie the editor's blocks to the source they were parsed from.
 *
 * Call after loading *markdown* into the editor. A block keeps its source only
 * while it equals the block that source parses to.
 *
 * :param editor: The editor that loaded *markdown*.
 * :param markdown: The loaded Markdown.
 */
export function rememberSourceBlocks(editor: Editor, markdown: string): void {
  editorBlocks.delete(editor);
  const found = recentParses.findIndex((entry) => entry.markdown === markdown);
  if (found === -1) return;
  const [parsed] = recentParses.splice(found, 1);
  const expected = parsed.doc.content ?? [];
  let originals: ProseMirrorNode[];
  try {
    originals = expected.map((block) => editor.schema.nodeFromJSON(block));
  } catch {
    // A parse for another schema has node types this editor lacks.
    return;
  }
  const { doc } = editor.state;
  const segmentOf = new Map<ProseMirrorNode, number>();
  let index = 0;
  const segments = parsed.segments.map(({ start, end, nodes: count }, segment) => {
    const nodes: ProseMirrorNode[] = [];
    for (let node = 0; node < count; node++, index++) {
      // Blocks changed while loading (e.g. a table given missing cells) are
      // serialised; unchanged ones match by structure and are found by identity.
      const child = index < doc.childCount ? doc.child(index) : null;
      const kept = child?.eq(originals[index]) ? child : null;
      if (kept) segmentOf.set(kept, segment);
      nodes.push(kept ?? originals[index]);
    }
    return { start, end, nodes };
  });
  editorBlocks.set(editor, {
    source: parsed.source,
    segments,
    segmentOf,
    definitions: definitionSources(doc),
  });
}

/** *text* without leading newlines or trailing blank lines. */
function trimLines(text: string): string {
  return text.replace(/^\n+/, "").replace(/(?:\n[ \t]*)+$/, "");
}

/**
 * Plan how to write the editor's document: unchanged runs of blocks as their
 * source, every other block serialised.
 *
 * :param editor: An editor passed to :func:`rememberSourceBlocks`.
 * :returns: Steps to join with blank lines, or null to serialise everything.
 */
export function sourceBlockPlan(editor: Editor): SourcePlanStep[] | null {
  const blocks = editorBlocks.get(editor);
  if (!blocks) return null;
  const { doc } = editor.state;
  // References in unchanged text resolve against the definitions; if those
  // changed, the serialiser decides how each reference is written.
  if (definitionSources(doc) !== blocks.definitions) return null;
  const { segments, source } = blocks;
  const same = (node: ProseMirrorNode, original: ProseMirrorNode) =>
    node === original || node.eq(original);
  const plan: SourcePlanStep[] = [];
  let nextSegment = 0;
  for (let index = 0; index < doc.childCount;) {
    // A block edited back to its original text is a new node, equal to the next segment's.
    let first = blocks.segmentOf.get(doc.child(index)) ?? nextSegment;
    while (first < segments.length && segments[first].nodes.length === 0) first++;
    let end = index;
    let last = -1;
    for (let segment = first; segment < segments.length; segment++) {
      const { nodes } = segments[segment];
      const matches = nodes.every(
        (node, offset) => end + offset < doc.childCount && same(doc.child(end + offset), node),
      );
      if (!matches) break;
      end += nodes.length;
      if (nodes.length) last = segment;
    }
    if (last === -1) {
      plan.push(index);
      index++;
      continue;
    }
    const text = (from: number, to: number) =>
      trimLines(source.slice(segments[from].start, segments[to].end));
    // The blocks at each end of the run meet blocks that may not have been there.
    const edges = segments
      .slice(first, last + 1)
      .map((segment, offset) => ({
        source: text(first + offset, first + offset),
        nodes: segment.nodes,
      }))
      .filter((edge) => edge.nodes.length && edge.source);
    if (!edges.length) {
      plan.push(index);
      index++;
      continue;
    }
    plan.push({ source: text(first, last), head: edges[0], tail: edges[edges.length - 1] });
    index = end;
    nextSegment = last + 1;
  }
  return plan;
}
