import { decodeHtmlEntities, type MarkdownToken } from "@tiptap/core";
import type { MarkdownManager } from "@tiptap/markdown";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { ALERT_MARKER } from "./TipTapGitHubAlert";
import { SOFT_BREAK_NODE } from "./TipTapSoftBreak";

interface Source {
  text: string;
  start: number;
  offsets?: number[];
  offsetStart?: number;
}
export interface SourceMap {
  starts: number[];
  ends: number[];
  positions: number[];
}
// Rendered characters of one top-level node; `textblocks` numbers each character's
// paragraph, heading, code block or cell so moved text cannot keep a stale range.
interface TokenBlock {
  text: string;
  starts: number[];
  ends: number[];
  textblocks: number[];
}
interface DocumentBlock {
  text: string;
  positions: number[];
  textblocks: number[];
}

// Thrown when a token's rendering cannot be verified.
class UnmappedBlock extends Error {}
function unmapped(): never {
  throw new UnmappedBlock();
}

const tokenCache = new WeakMap<
  MarkdownManager,
  { source: string; tokens: MarkdownToken[] | null }
>();
const cache = new WeakMap<
  ProseMirrorNode,
  {
    raw: string;
    manager: MarkdownManager;
    map: SourceMap | null;
  }
>();

// Lex with the editor's configured marked instance, once per source string.
// A failed lex is cached as null and rethrown to the caller for reporting.
function markdownTokens(manager: MarkdownManager, source: string): MarkdownToken[] | null {
  const previous = tokenCache.get(manager);
  if (previous?.source === source) return previous.tokens;
  const marked = manager.instance;
  let tokens: MarkdownToken[] | null = null;
  try {
    tokens = new marked.Lexer(marked.defaults).lex(source);
  } finally {
    tokenCache.set(manager, { source, tokens });
  }
  return tokens;
}

function offset(source: Source, index: number): number {
  return source.offsets?.[(source.offsetStart ?? 0) + index] ?? source.start + index;
}
function slice(source: Source, start: number, end = source.text.length): Source {
  return {
    text: source.text.slice(start, end),
    start: offset(source, start),
    offsets: source.offsets,
    offsetStart: source.offsets ? (source.offsetStart ?? 0) + start : undefined,
  };
}
function rawText(token: MarkdownToken): string {
  if (token.type === "list_item" && token.text === undefined) {
    const nested = token.tokens?.at(-1);
    if (nested?.type === "list" && nested.ordered && nested.raw)
      return `${token.raw ?? ""}\n${nested.raw}`;
  }
  return token.type === "taskList" ? (token.raw ?? "").replace(/\n$/, "") : (token.raw ?? "");
}

// Container tokens remove list/quote prefixes. Locate each retained line inside
// its parent token, never by searching for a word across the whole document.
function locate(source: Source, text: string, from = 0): Source | null {
  let cursor = from;
  const at = source.text.indexOf(text, cursor);
  if (at !== -1) return slice(source, at, at + text.length);
  const offsets: number[] = [];
  for (const line of text.match(/[^\n]*\n|[^\n]+$/g) ?? []) {
    const found = source.text.indexOf(line, cursor);
    if (found === -1) return null;
    for (let i = 0; i < line.length; i++) offsets.push(offset(source, found + i));
    cursor = found + line.length;
  }
  return offsets.length === text.length ? { text, start: offsets[0], offsets } : null;
}

function tableCellSources(row: Source): Source[] {
  const cells: Source[] = [];
  let start = 0;
  for (const separator of row.text.matchAll(/\|/g)) {
    let escapeStart = separator.index;
    while (escapeStart > 0 && row.text[escapeStart - 1] === "\\") escapeStart--;
    if ((separator.index - escapeStart) % 2 !== 0) continue;
    cells.push(slice(row, start, separator.index));
    start = separator.index + 1;
  }
  cells.push(slice(row, start));
  if (!cells[0].text.trim()) cells.shift();
  if (cells.length && !cells[cells.length - 1].text.trim()) cells.pop();
  return cells.map((cell) => {
    const leading = cell.text.length - cell.text.trimStart().length;
    const trimmed = slice(cell, leading, cell.text.trimEnd().length);
    if (!trimmed.text.includes("\\|")) return trimmed;
    let text = "";
    const offsets: number[] = [];
    for (let index = 0; index < trimmed.text.length; index++) {
      if (trimmed.text[index] === "\\" && trimmed.text[index + 1] === "|") continue;
      text += trimmed.text[index];
      offsets.push(offset(trimmed, index));
    }
    return { text, start: offsets[0] ?? trimmed.start, offsets };
  });
}

// Returns the text-bearing top-level blocks, or null when a token cannot be
// verified or the tokens do not consume the source in order.
function mapTokenBlocks(topLevel: MarkdownToken[], source: Source): TokenBlock[] | null {
  let block: TokenBlock = { text: "", starts: [], ends: [], textblocks: [] };
  let textblock = 0;

  function emit(value: string, view: Source, decode = false) {
    const located = locate(view, value);
    if (!located) unmapped();
    const append = (text: string, from: number, to?: number) => {
      block.text += text;
      for (let i = 0; i < text.length; i++) {
        block.starts.push(offset(located, from + (to === undefined ? i : 0)));
        block.ends.push(offset(located, to === undefined ? from + i : to - 1) + 1);
        block.textblocks.push(textblock);
      }
    };
    if (!decode) {
      append(value, 0);
      return;
    }
    const pattern = /&(?:#\d+|#x[\da-f]+|[a-z][\da-z]+);/gi;
    let cursor = 0;
    for (const match of value.matchAll(pattern)) {
      append(value.slice(cursor, match.index), cursor);
      const decoded = decodeHtmlEntities(match[0]);
      if (decoded === match[0]) append(match[0], match.index);
      else append(decoded, match.index, match.index + match[0].length);
      cursor = match.index + match[0].length;
    }
    append(value.slice(cursor), cursor);
  }

  function children(tokens: MarkdownToken[], view: Source) {
    let cursor = 0;
    for (const token of tokens) {
      const value = rawText(token);
      const found = value ? locate(view, value, cursor) : null;
      if (!found) unmapped();
      const last = offset(found, value.length - 1);
      while (cursor < view.text.length && offset(view, cursor) <= last) cursor++;
      visit(token, found);
    }
  }

  function visit(token: MarkdownToken, view: Source) {
    switch (token.type) {
      case "def":
      case "space":
      case "hr":
      case "image":
      case "br":
        return;
      case "html":
        // HTML blocks are opaque in our editor. Inline tags have no text of
        // their own; the document comparison rejects any different rendering.
        if (!token.block && !/^<[^>]*>$/.test(view.text)) unmapped();
        return;
      case "code": {
        const body = /^[ \t]*(`{3,}|~{3,})/.test(view.text)
          ? slice(view, view.text.indexOf("\n") + 1)
          : view;
        textblock++;
        emit(token.text ?? "", body);
        return;
      }
      case "codespan": {
        const ticks = /^`+/.exec(view.text)?.[0].length ?? 0;
        const body = slice(view, ticks, view.text.length - ticks);
        // Newlines inside code spans render as spaces.
        emit(token.text ?? "", { ...body, text: body.text.replace(/\n/g, " ") });
        return;
      }
      case "escape":
        emit(token.text ?? "", slice(view, 1));
        return;
      case "table": {
        const rows = [token.header, ...(token.rows ?? [])] as {
          text: string;
          tokens: MarkdownToken[];
        }[][];
        const lines = view.text.match(/[^\n]*\n|[^\n]+$/g) ?? [];
        let lineStart = 0;
        for (let rowIndex = 0; rowIndex < rows.length; rowIndex++) {
          // Line 1 is the delimiter row, which has no cells.
          if (rowIndex === 1) lineStart += lines[1]?.length ?? 0;
          const lineIndex = rowIndex === 0 ? 0 : rowIndex + 1;
          const line = slice(view, lineStart, lineStart + (lines[lineIndex]?.length ?? 0));
          const sources = tableCellSources(line);
          for (const [cellIndex, cell] of rows[rowIndex].entries()) {
            textblock++;
            const cellSource = sources[cellIndex];
            if (!cell.text) {
              if (cellSource?.text) unmapped();
              continue;
            }
            if (!cellSource || cellSource.text !== cell.text) unmapped();
            children(cell.tokens, cellSource);
          }
          lineStart += line.text.length;
        }
        return;
      }
      case "taskList": {
        const lines = [...view.text.matchAll(/^( *)([-+*]) +\[[ xX]\] +(.*)$/gm)];
        const indent = lines[0]?.[1].length;
        const siblings = lines.filter((line) => line[1].length === indent);
        const items = token.items ?? [];
        if (siblings.length !== items.length) unmapped();
        for (let i = 0; i < items.length; i++) {
          const item = items[i];
          const line = siblings[i];
          if (line[3] !== item.text) unmapped();
          const from = line.index + line[0].length - line[3].length;
          textblock++;
          children(item.tokens ?? [], slice(view, from, from + line[3].length));
          const nested = slice(view, line.index + line[0].length + 1, siblings[i + 1]?.index);
          children(item.nestedTokens ?? [], nested);
        }
        return;
      }
      case "list":
        children(token.items ?? [], view);
        return;
      case "blockquote": {
        const begin = block.text.length;
        const inner = locate(view, token.text ?? "");
        if (!inner) unmapped();
        const tokens = token.tokens ?? [];
        children(tokens, inner);
        const marker = ALERT_MARKER.exec(tokens[0]?.text ?? "");
        if (marker) {
          if (!block.text.startsWith(marker[0], begin)) unmapped();
          block.text = block.text.slice(0, begin) + block.text.slice(begin + marker[0].length);
          for (const values of [block.starts, block.ends, block.textblocks])
            values.splice(begin, marker[0].length);
        }
        return;
      }
      case "list_item":
      case "taskItem": {
        const marker = /^[ \t]*(?:[-+*]|(?:\d+|[a-zA-Z]+)[.)])[ \t]+/.exec(view.text);
        const body = slice(view, marker?.[0].length ?? 0);
        const inner = token.text === undefined ? body : locate(body, token.text);
        if (!inner) unmapped();
        textblock++;
        children(token.tokens ?? [], inner);
        return;
      }
      case "heading": {
        const marker = /^ {0,3}#{1,6}(?:[ \t]+|$)/.exec(view.text);
        textblock++;
        children(token.tokens ?? [], slice(view, marker?.[0].length ?? 0));
        return;
      }
      case "strong":
      case "em":
      case "del": {
        const size =
          token.type === "strong" || (token.type === "del" && view.text.startsWith("~~")) ? 2 : 1;
        children(token.tokens ?? [], slice(view, size, view.text.length - size));
        return;
      }
      case "link":
        children(
          token.tokens ?? [],
          slice(view, view.text.startsWith("[") || view.text.startsWith("<") ? 1 : 0),
        );
        return;
      case "paragraph":
        textblock++;
        children(token.tokens ?? [], view);
        return;
      case "text":
        if (token.tokens) children(token.tokens, view);
        else emit(token.text ?? "", view, true);
        return;
      default:
        unmapped();
    }
  }

  const blocks: TokenBlock[] = [];
  let cursor = 0;
  for (const token of topLevel) {
    const value = rawText(token);
    const at = value ? source.text.indexOf(value, cursor) : -1;
    // Top-level tokens consume the source in order; only whitespace may be omitted.
    if (at < 0 || source.text.slice(cursor, at).trim()) return null;
    cursor = at + value.length;
    block = { text: "", starts: [], ends: [], textblocks: [] };
    try {
      visit(token, slice(source, at, cursor));
    } catch (error) {
      if (!(error instanceof UnmappedBlock)) throw error;
      return null;
    }
    if (block.text) blocks.push(block);
  }
  return blocks;
}

// Text-bearing top-level nodes; soft breaks render the source "\n" they replace.
function documentBlocks(doc: ProseMirrorNode): DocumentBlock[] {
  const blocks: DocumentBlock[] = [];
  doc.forEach((child, childOffset) => {
    const block: DocumentBlock = { text: "", positions: [], textblocks: [] };
    let textblock = child.isTextblock ? 1 : 0;
    const append = (text: string, pos: number) => {
      block.text += text;
      for (let i = 0; i < text.length; i++) {
        block.positions.push(pos + i);
        block.textblocks.push(textblock);
      }
    };
    child.descendants((node, relative) => {
      const pos = childOffset + 1 + relative;
      if (node.isText) append(node.text ?? "", pos);
      else if (node.type.name === SOFT_BREAK_NODE) append("\n", pos);
      else if (node.isTextblock) textblock++;
      return true;
    });
    if (block.text) blocks.push(block);
  });
  return blocks;
}

function sameTextblocks(tokenBlock: TokenBlock, documentBlock: DocumentBlock): boolean {
  const { textblocks } = tokenBlock;
  return textblocks.every(
    (value, i) =>
      i === 0 ||
      (value === textblocks[i - 1]) ===
        (documentBlock.textblocks[i] === documentBlock.textblocks[i - 1]),
  );
}

function buildMap(doc: ProseMirrorNode, raw: string, manager: MarkdownManager): SourceMap | null {
  const normalized = raw.replace(/\r\n?/g, "\n");
  const source: Source = { text: normalized, start: 0 };
  if (normalized !== raw) {
    source.offsets = [];
    for (let i = 0; i < raw.length; i++) {
      source.offsets.push(i);
      if (raw[i] === "\r" && raw[i + 1] === "\n") i++;
    }
  }
  const tokens = markdownTokens(manager, normalized);
  const tokenBlocks = tokens && mapTokenBlocks(tokens, source);
  const docBlocks = documentBlocks(doc);
  // A partial map cannot tell exact offsets from legacy ones, so any unverified
  // or edited block keeps the whole document on legacy anchors.
  if (!tokenBlocks || tokenBlocks.length !== docBlocks.length) return null;
  const starts: number[] = [];
  const ends: number[] = [];
  const positions: number[] = [];
  for (const [index, tokenBlock] of tokenBlocks.entries()) {
    const documentBlock = docBlocks[index];
    if (tokenBlock.text !== documentBlock.text || !sameTextblocks(tokenBlock, documentBlock))
      return null;
    for (let i = 0; i < tokenBlock.text.length; i++) {
      starts.push(tokenBlock.starts[i]);
      const end = tokenBlock.ends[i];
      ends.push(raw[end - 1] === "\r" && raw[end] === "\n" ? end + 1 : end);
      positions.push(documentBlock.positions[i]);
    }
  }
  const monotonic = (values: number[]) =>
    values.every((value, i) => i === 0 || value >= values[i - 1]);
  return positions.length && monotonic(starts) && monotonic(ends)
    ? { starts, ends, positions }
    : null;
}

export function markdownSourceMap(
  doc: ProseMirrorNode,
  raw: string,
  manager?: MarkdownManager,
): SourceMap | null {
  if (!manager) return null;
  const previous = cache.get(doc);
  if (previous?.raw === raw && previous.manager === manager) return previous.map;
  let map: SourceMap | null = null;
  try {
    map = buildMap(doc, raw, manager);
  } catch (error) {
    // Custom tokenizer failures must preserve the legacy comment path.
    console.warn("Markdown source map failed; using legacy anchors", error);
  }
  cache.set(doc, { raw, manager, map });
  return map;
}

export function exactIndex(values: number[], value: number): number {
  // Source maps emit each array in ascending order.
  let lo = 0;
  let hi = values.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (values[mid] < value) lo = mid + 1;
    else hi = mid;
  }
  return values[lo] === value ? lo : -1;
}
