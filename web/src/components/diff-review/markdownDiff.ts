// Compares two versions of a Markdown file as rendered blocks, the way a reader
// sees them: whole blocks are added, removed, or unchanged, and an edited
// paragraph, heading, or list marks the words that changed. Runs without React
// or the DOM so a worker can do the work; every step is bounded by `limits`.

import { normalizeExplicitMathDelimiters } from "@/components/ai-elements/mathMarkdown";
import {
  MARKDOWN_PREVIEW_MATH_REHYPE_PLUGIN,
  MARKDOWN_PREVIEW_REMARK_PLUGINS,
  MARKDOWN_PREVIEW_SANITIZED_REHYPE_PLUGINS,
} from "@/lib/markdownPreviewPipeline";
import { diffArrays } from "diff";
import type { Element, ElementContent, Nodes, Root, RootContent, Text } from "hast";
import { toHtml } from "hast-util-to-html";
import remarkParse from "remark-parse";
import remarkRehype from "remark-rehype";
import { unified } from "unified";

export type MarkdownDiffKind = "unchanged" | "added" | "removed";

/** One top-level rendered block, already sanitized, and its text for find. */
export interface MarkdownDiffSection {
  kind: MarkdownDiffKind;
  block: RootContent;
  text: string;
  /** Rendered nodes in `block`, so the page can render a few at a time. */
  nodes: number;
  /** The block is too big to render here; only its source diff shows it, and find skips it. */
  oversized?: boolean;
}

export type MarkdownDiffResult =
  { status: "ready"; sections: MarkdownDiffSection[] } | { status: "tooLarge"; message: string };

export interface MarkdownDiffLimits {
  /** Characters in either version. */
  maxSideLength: number;
  /** Top-level blocks in either version. */
  maxBlocks: number;
  /** Rendered nodes across both versions, after math. */
  maxNodes: number;
  /** Formulas typeset per version; beyond this, math shows as TeX. */
  maxMath: number;
  /** Mermaid diagrams drawn per diff; beyond this, they show as code. */
  maxDiagrams: number;
  /** Changed blocks before × after that the diff may compare; beyond this, replace wholesale. */
  maxAlignmentCells: number;
  /** Tokens word diffing may compare across the document. */
  wordBudget: number;
  /** Rendered nodes one block may have; a bigger block, such as a huge table, isn't rendered. */
  maxBlockNodes: number;
  /** Consecutive lines with a `|`; table parsing slows down sharply past this, so it isn't tried. */
  maxTableLines: number;
}

export const WORKER_MARKDOWN_DIFF_LIMITS: MarkdownDiffLimits = {
  maxSideLength: 1_000_000,
  maxBlocks: 5_000,
  maxNodes: 60_000,
  maxMath: 200,
  maxDiagrams: 20,
  maxAlignmentCells: 4_000_000,
  wordBudget: 20_000,
  maxBlockNodes: 4_000,
  maxTableLines: 1_500,
};

/** Smaller limits for when no worker is available and the page itself does the work. */
export const MAIN_THREAD_MARKDOWN_DIFF_LIMITS: MarkdownDiffLimits = {
  maxSideLength: 100_000,
  maxBlocks: 1_000,
  maxNodes: 10_000,
  maxMath: 40,
  maxDiagrams: 5,
  maxAlignmentCells: 250_000,
  wordBudget: 5_000,
  maxBlockNodes: 1_000,
  maxTableLines: 300,
};

export const TOO_LARGE_MESSAGE = "This file is too large for a rich diff.";

/** Marks a changed run of words inside an edited block. Sanitize strips this class from authored HTML. */
export const MARKDOWN_DIFF_WORD_CLASS = "markdown-diff-word";

/** Set on a Mermaid block past the diagram limit, so it renders as code. */
export const MARKDOWN_DIFF_STATIC_PROPERTY = "dataMarkdownDiffStatic";

/** Word diff skips an edited pair longer than this, and parts with more tokens than this. */
const MAX_WORD_PAIR_LENGTH = 16_000;
const MAX_WORD_PART_TOKENS = 1_024;

class TooLarge extends Error {}

const sanitizedProcessor = unified()
  .use(remarkParse)
  .use(MARKDOWN_PREVIEW_REMARK_PLUGINS ?? [])
  .use(remarkRehype, { allowDangerousHtml: true })
  .use(MARKDOWN_PREVIEW_SANITIZED_REHYPE_PLUGINS);
const mathProcessor = unified().use([MARKDOWN_PREVIEW_MATH_REHYPE_PLUGIN]);

/** Diff `before` and `after` (null for a side that doesn't exist) as rendered Markdown. */
export function computeMarkdownDiff(
  before: string | null,
  after: string | null,
  limits: MarkdownDiffLimits = WORKER_MARKDOWN_DIFF_LIMITS,
): MarkdownDiffResult {
  try {
    const beforeBlocks = renderBlocks(before, limits);
    const afterBlocks = renderBlocks(after, limits);
    if (countNodes([...beforeBlocks, ...afterBlocks]) > limits.maxNodes) throw new TooLarge();
    const sections = alignBlocks(beforeBlocks, afterBlocks, limits.maxAlignmentCells);
    markChangedWords(sections, limits.wordBudget);
    limitDiagrams(sections, limits.maxDiagrams);
    for (const entry of sections) {
      entry.nodes = nodeCount(entry.block);
      if (entry.nodes > limits.maxBlockNodes) {
        // Nothing of an oversized block is sent to the page or searched there.
        entry.oversized = true;
        entry.block = { type: "element", tagName: "div", properties: {}, children: [] };
        entry.nodes = 1;
      } else {
        entry.text = blockText(entry.block);
      }
    }
    return { status: "ready", sections };
  } catch (error) {
    if (error instanceof TooLarge) return { status: "tooLarge", message: TOO_LARGE_MESSAGE };
    throw error;
  }
}

/** Render one version to sanitized top-level blocks, without source positions. */
function renderBlocks(source: string | null, limits: MarkdownDiffLimits): RootContent[] {
  if (source === null || source.trim() === "") return [];
  if (source.length > limits.maxSideLength) throw new TooLarge();
  if (longestTableLikeRun(source) > limits.maxTableLines) throw new TooLarge();
  const normalized = normalizeExplicitMathDelimiters(source);
  let tree = sanitizedProcessor.runSync(sanitizedProcessor.parse(normalized)) as Root;
  const blocks = tree.children.filter((block) => !isBlankText(block));
  if (blocks.length > limits.maxBlocks) throw new TooLarge();
  if (countMath(tree) <= limits.maxMath) tree = mathProcessor.runSync(tree) as Root;
  const rendered = tree.children.filter((block) => !isBlankText(block));
  for (const block of rendered) stripPositions(block);
  return rendered;
}

/** The longest run of consecutive lines containing `|`, an upper bound on any table's rows. */
export function longestTableLikeRun(source: string): number {
  let longest = 0;
  let run = 0;
  let start = 0;
  let pipe = source.indexOf("|");
  while (start <= source.length) {
    let end = source.indexOf("\n", start);
    if (end === -1) end = source.length;
    // One forward scan for pipes, so a file without them stays linear.
    if (pipe !== -1 && pipe < start) pipe = source.indexOf("|", start);
    run = pipe !== -1 && pipe < end ? run + 1 : 0;
    if (run > longest) longest = run;
    start = end + 1;
  }
  return longest;
}

function isBlankText(node: RootContent): boolean {
  return (node.type === "text" && node.value.trim() === "") || node.type === "comment";
}

function* walk(node: Nodes): Generator<Nodes> {
  yield node;
  if ("children" in node) for (const child of node.children) yield* walk(child);
}

function nodeCount(node: Nodes): number {
  if (!("children" in node)) return 1;
  let count = 1;
  for (const child of node.children) count += nodeCount(child);
  return count;
}

function countNodes(blocks: readonly RootContent[]): number {
  return blocks.reduce((total, block) => total + nodeCount(block), 0);
}

function classNames(node: Element): string[] {
  const value = node.properties.className;
  return Array.isArray(value) ? value.map(String) : typeof value === "string" ? [value] : [];
}

function countMath(tree: Root): number {
  let count = 0;
  for (const node of walk(tree)) {
    if (node.type !== "element") continue;
    const names = classNames(node);
    if (names.some((name) => /^(language-math|math-inline|math-display)$/.test(name))) count += 1;
  }
  return count;
}

function stripPositions(node: Nodes): void {
  for (const each of walk(node)) delete each.position;
}

/**
 * A rendered block's text as the page will hold it, so find's offsets line up
 * with the DOM: a drawn Mermaid diagram shows no source text.
 */
export function blockText(node: Nodes): string {
  if (node.type === "text") return node.value;
  if (!("children" in node)) return "";
  if (isMermaidBlock(node) && !node.properties[MARKDOWN_DIFF_STATIC_PROPERTY]) return "";
  return node.children.map(blockText).join("");
}

function section(kind: MarkdownDiffKind, block: RootContent): MarkdownDiffSection {
  return { kind, block, text: "", nodes: 0 };
}

/** Align blocks by their rendered HTML. A huge changed middle is shown replaced as a whole. */
function alignBlocks(
  before: readonly RootContent[],
  after: readonly RootContent[],
  maxAlignmentCells: number,
): MarkdownDiffSection[] {
  const ids = new Map<string, number>();
  const intern = (block: RootContent) => {
    const html = toHtml(block);
    let id = ids.get(html);
    if (id === undefined) {
      id = ids.size;
      ids.set(html, id);
    }
    return id;
  };
  const beforeIds = before.map(intern);
  const afterIds = after.map(intern);
  let prefix = 0;
  while (
    prefix < beforeIds.length &&
    prefix < afterIds.length &&
    beforeIds[prefix] === afterIds[prefix]
  ) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < beforeIds.length - prefix &&
    suffix < afterIds.length - prefix &&
    beforeIds[beforeIds.length - 1 - suffix] === afterIds[afterIds.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  const middleBefore = beforeIds.slice(prefix, beforeIds.length - suffix);
  const middleAfter = afterIds.slice(prefix, afterIds.length - suffix);
  const sections: MarkdownDiffSection[] = before
    .slice(0, prefix)
    .map((block) => section("unchanged", block));
  const changes: [number, number, number, number][] =
    middleBefore.length * middleAfter.length > maxAlignmentCells
      ? [[0, middleBefore.length, 0, middleAfter.length]]
      : changedSpans(middleBefore, middleAfter);
  let beforeIndex = 0;
  for (const [startBefore, endBefore, startAfter, endAfter] of changes) {
    for (; beforeIndex < startBefore; beforeIndex += 1) {
      sections.push(section("unchanged", before[prefix + beforeIndex]));
    }
    for (let index = startBefore; index < endBefore; index += 1) {
      sections.push(section("removed", before[prefix + index]));
    }
    for (let index = startAfter; index < endAfter; index += 1) {
      sections.push(section("added", after[prefix + index]));
    }
    beforeIndex = endBefore;
  }
  for (; beforeIndex < middleBefore.length; beforeIndex += 1) {
    sections.push(section("unchanged", before[prefix + beforeIndex]));
  }
  for (const block of after.slice(after.length - suffix))
    sections.push(section("unchanged", block));
  return sections;
}

/** The shortest edit from `before` to `after`, as `[startBefore, endBefore, startAfter, endAfter]` spans. */
export function changedSpans<T>(
  before: readonly T[],
  after: readonly T[],
): [number, number, number, number][] {
  const spans: [number, number, number, number][] = [];
  let beforeIndex = 0;
  let afterIndex = 0;
  let open: [number, number, number, number] | undefined;
  for (const change of diffArrays([...before], [...after])) {
    if (!change.added && !change.removed) {
      if (open) spans.push(open);
      open = undefined;
      beforeIndex += change.count;
      afterIndex += change.count;
      continue;
    }
    // A removal and the insertion right after it are one replaced span.
    open ??= [beforeIndex, beforeIndex, afterIndex, afterIndex];
    if (change.removed) {
      beforeIndex += change.count;
      open[1] = beforeIndex;
    } else {
      afterIndex += change.count;
      open[3] = afterIndex;
    }
  }
  if (open) spans.push(open);
  return spans;
}

/**
 * Mark changed words where a run of removed blocks is replaced by the same
 * number of added blocks of the same shape, such as one edited paragraph.
 */
function markChangedWords(sections: MarkdownDiffSection[], wordBudget: number): void {
  let budget = wordBudget;
  let index = 0;
  while (index < sections.length && budget > 0) {
    if (sections[index].kind !== "removed") {
      index += 1;
      continue;
    }
    let removedEnd = index;
    while (removedEnd < sections.length && sections[removedEnd].kind === "removed") removedEnd += 1;
    let addedEnd = removedEnd;
    while (addedEnd < sections.length && sections[addedEnd].kind === "added") addedEnd += 1;
    const count = removedEnd - index;
    if (addedEnd - removedEnd === count) {
      for (let offset = 0; offset < count && budget > 0; offset += 1) {
        const removed = sections[index + offset];
        const added = sections[removedEnd + offset];
        budget -= markBlockWords(removed.block, added.block, budget);
      }
    }
    index = addedEnd;
  }
}

const PROSE_BLOCK = /^(p|h[1-6])$/;
const LIST_BLOCK = /^(ul|ol)$/;
const INLINE_ELEMENT = /^(p|a|em|strong|code|del|s|b|i|span|sub|sup|kbd)$/;

/** The parts of a prose block whose words can be compared, or undefined if it isn't plain prose. */
function proseParts(block: RootContent): Element[] | undefined {
  if (block.type !== "element") return undefined;
  const parts = PROSE_BLOCK.test(block.tagName)
    ? [block]
    : LIST_BLOCK.test(block.tagName)
      ? block.children.filter((child): child is Element => child.type === "element")
      : undefined;
  if (!parts) return undefined;
  for (const part of parts) {
    for (const node of walk(part)) {
      if (node === part || node.type === "text") continue;
      if (node.type !== "element" || !INLINE_ELEMENT.test(node.tagName)) return undefined;
    }
  }
  return parts;
}

/** Wrap changed words in both blocks; returns the tokens it compared. */
function markBlockWords(removed: RootContent, added: RootContent, budget: number): number {
  if (removed.type !== "element" || added.type !== "element") return 0;
  if (removed.tagName !== added.tagName) return 0;
  if (blockText(removed).length + blockText(added).length > MAX_WORD_PAIR_LENGTH) return 0;
  const oldParts = proseParts(removed);
  const newParts = proseParts(added);
  if (!oldParts || !newParts || oldParts.length !== newParts.length) return 0;
  let spent = 0;
  for (let part = 0; part < oldParts.length; part += 1) {
    const oldText = blockText(oldParts[part]);
    const newText = blockText(newParts[part]);
    if (oldText === newText) continue;
    const oldTokens = tokenize(oldText);
    const newTokens = tokenize(newText);
    const cost = oldTokens.length + newTokens.length;
    if (cost > MAX_WORD_PART_TOKENS || spent + cost > budget) continue;
    spent += cost;
    const { oldRanges, newRanges } = changedRanges(oldTokens, newTokens);
    wrapRanges(oldParts[part], oldRanges);
    wrapRanges(newParts[part], newRanges);
  }
  return spent;
}

/** Words, runs of whitespace, and single punctuation marks. */
export function tokenize(text: string): string[] {
  return text.match(/[\p{L}\p{N}\p{M}_]+|\s+|[^\p{L}\p{N}\p{M}_\s]/gu) ?? [];
}

type Range = [start: number, end: number];

/** Character ranges of the tokens each side changed, skipping whitespace-only changes. */
export function changedRanges(
  oldTokens: readonly string[],
  newTokens: readonly string[],
): { oldRanges: Range[]; newRanges: Range[] } {
  const offsets = (tokens: readonly string[]) => {
    const result = [0];
    for (const token of tokens) result.push(result[result.length - 1] + token.length);
    return result;
  };
  const oldOffsets = offsets(oldTokens);
  const newOffsets = offsets(newTokens);
  const oldRanges: Range[] = [];
  const newRanges: Range[] = [];
  const oldText = oldTokens.join("");
  const newText = newTokens.join("");
  const push = (
    ranges: Range[],
    tokens: readonly string[],
    text: string,
    at: number[],
    changeStart: number,
    changeEnd: number,
  ) => {
    // Leading and trailing whitespace stays unmarked so a highlight hugs the words.
    let start = changeStart;
    let end = changeEnd;
    while (start < end && tokens[start].trim() === "") start += 1;
    while (end > start && tokens[end - 1].trim() === "") end -= 1;
    if (start >= end) return;
    // Changed words only whitespace separates, as in `b` → `x y`, read as one change.
    const last = ranges[ranges.length - 1];
    if (last && text.slice(last[1], at[start]).trim() === "") last[1] = at[end];
    else ranges.push([at[start], at[end]]);
  };
  for (const [startOld, endOld, startNew, endNew] of changedSpans(oldTokens, newTokens)) {
    push(oldRanges, oldTokens, oldText, oldOffsets, startOld, endOld);
    push(newRanges, newTokens, newText, newOffsets, startNew, endNew);
  }
  return { oldRanges, newRanges };
}

/** Wrap text slices in place, never markup: links and inline formatting survive, and text is unchanged. */
function wrapRanges(root: Element, ranges: readonly Range[]): void {
  if (ranges.length === 0) return;
  let offset = 0;
  const visit = (parent: Element) => {
    const children: ElementContent[] = [];
    for (const child of parent.children) {
      if (child.type === "element") {
        visit(child);
        children.push(child);
        continue;
      }
      if (child.type !== "text") {
        children.push(child);
        continue;
      }
      const start = offset;
      const end = offset + child.value.length;
      offset = end;
      let cursor = 0;
      for (const [from, to] of ranges) {
        const sliceStart = Math.max(from, start) - start;
        const sliceEnd = Math.min(to, end) - start;
        if (sliceStart >= sliceEnd) continue;
        if (sliceStart > cursor) children.push(textNode(child.value.slice(cursor, sliceStart)));
        children.push({
          type: "element",
          tagName: "span",
          properties: { className: [MARKDOWN_DIFF_WORD_CLASS] },
          children: [textNode(child.value.slice(sliceStart, sliceEnd))],
        });
        cursor = sliceEnd;
      }
      if (cursor < child.value.length) children.push(textNode(child.value.slice(cursor)));
    }
    parent.children = children;
  };
  visit(root);
}

function textNode(value: string): Text {
  return { type: "text", value };
}

function isMermaidBlock(node: Nodes): node is Element {
  if (node.type !== "element" || node.tagName !== "pre") return false;
  const code = node.children.find((child) => child.type === "element" && child.tagName === "code");
  return (
    !!code &&
    code.type === "element" &&
    classNames(code).some((name) => name.toLowerCase() === "language-mermaid")
  );
}

/** Past the diagram limit, Mermaid blocks render as their source. */
function limitDiagrams(sections: readonly MarkdownDiffSection[], maxDiagrams: number): void {
  let diagrams = 0;
  for (const { block } of sections) {
    for (const node of walk(block)) {
      if (!isMermaidBlock(node)) continue;
      diagrams += 1;
      if (diagrams > maxDiagrams) node.properties[MARKDOWN_DIFF_STATIC_PROPERTY] = "true";
    }
  }
}
