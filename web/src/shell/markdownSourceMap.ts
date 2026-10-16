import { markdownTokens } from "./SourceMappedMarkdown";
import { decodeHtmlEntities, type MarkdownToken } from "@tiptap/core";
import type { MarkdownManager } from "@tiptap/markdown";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { ALERT_MARKER } from "./TipTapGitHubAlert";

interface Source {
  text: string;
  start: number;
  offsets?: number[];
  offsetStart?: number;
}
interface SourceMap {
  starts: number[];
  ends: number[];
  positions: number[];
}
const cache = new WeakMap<
  ProseMirrorNode,
  {
    raw: string;
    manager: MarkdownManager;
    map: SourceMap | null;
  }
>();

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
  const starts: number[] = [];
  const ends: number[] = [];
  const pieces: string[] = [];
  let valid = true;

  function emit(value: string, view: Source, decode = false) {
    const located = locate(view, value);
    if (!located) {
      valid = false;
      return;
    }
    const append = (text: string, from: number, to?: number) => {
      pieces.push(text);
      for (let i = 0; i < text.length; i++) {
        starts.push(offset(located, from + (to === undefined ? i : 0)));
        ends.push(offset(located, to === undefined ? from + i : to - 1) + 1);
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

  function children(tokens: MarkdownToken[], view: Source, top = false) {
    let cursor = 0;
    for (const token of tokens) {
      const value =
        token.type === "taskList" ? (token.raw ?? "").replace(/\n$/, "") : (token.raw ?? "");
      if (!valid) return;
      let found: Source | null;
      // Top-level tokens consume the source in order; only whitespace may be
      // omitted. Never locate a block inside a skipped construct's contents.
      if (top) {
        const at = view.text.indexOf(value, cursor);
        found =
          at < 0 || view.text.slice(cursor, at).trim() ? null : slice(view, at, at + value.length);
      } else {
        found = locate(view, value, cursor);
      }
      if (!found || !value) {
        valid = false;
        return;
      }
      const last = offset(found, value.length - 1);
      while (cursor < view.text.length && offset(view, cursor) <= last) cursor++;
      visit(token, found);
    }
  }

  function visit(token: MarkdownToken, view: Source) {
    if (!valid) return;
    switch (token.type) {
      case "def":
      case "space":
      case "hr":
      case "image":
      case "br":
        return;
      case "html":
        // HTML blocks are opaque in our editor. Inline tags have no text of
        // their own; the final comparison rejects any different DOM rendering.
        if (!token.block && !/^<[^>]*>$/.test(view.text)) valid = false;
        return;
      case "code": {
        const body = /^[ \t]*(`{3,}|~{3,})/.test(view.text)
          ? slice(view, view.text.indexOf("\n") + 1)
          : view;
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
        if (/\\\|/.test(view.text)) {
          valid = false;
          return;
        }
        const rows = [token.header, ...(token.rows ?? [])] as {
          text: string;
          tokens: MarkdownToken[];
        }[][];
        const lines = view.text.match(/[^\n]*\n|[^\n]+$/g) ?? [];
        let lineStart = 0;
        for (let r = 0; r < rows.length; r++) {
          if (r === 1) lineStart += lines[1]?.length ?? 0;
          const line = slice(
            view,
            lineStart,
            lineStart + (lines[r === 0 ? 0 : r + 1]?.length ?? 0),
          );
          let cursor = 0;
          for (const cell of rows[r]) {
            if (!cell.text) continue;
            const at = line.text.indexOf(cell.text, cursor);
            if (at === -1) {
              valid = false;
              return;
            }
            children(cell.tokens, slice(line, at, at + cell.text.length));
            cursor = at + cell.text.length;
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
        if (siblings.length !== items.length) {
          valid = false;
          return;
        }
        for (let i = 0; i < items.length && valid; i++) {
          const item = items[i];
          const line = siblings[i];
          if (line[3] !== item.text) {
            valid = false;
            return;
          }
          const from = line.index + line[0].length - line[3].length;
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
        const begin = pieces.length;
        const charBegin = starts.length;
        const inner = locate(view, token.text ?? "");
        if (!inner) {
          valid = false;
          return;
        }
        const tokens = token.tokens ?? [];
        children(tokens, inner);
        const marker = ALERT_MARKER.exec(tokens[0]?.text ?? "");
        if (marker) {
          const text = pieces.slice(begin).join("");
          pieces.splice(begin, pieces.length - begin, text.slice(marker[0].length));
          starts.splice(charBegin, marker[0].length);
          ends.splice(charBegin, marker[0].length);
        }
        return;
      }
      case "list_item":
      case "taskItem": {
        const marker = /^ {0,3}(?:[-+*]|\d+[.)])[ \t]+/.exec(view.text);
        const body = slice(view, marker?.[0].length ?? 0);
        const inner = token.text === undefined ? body : locate(body, token.text);
        if (!inner) {
          valid = false;
          return;
        }
        children(token.tokens ?? [], inner);
        return;
      }
      case "heading": {
        const marker = /^ {0,3}#{1,6}(?:[ \t]+|$)/.exec(view.text);
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
        children(token.tokens ?? [], view);
        return;
      case "text":
        if (token.tokens) children(token.tokens, view);
        else emit(token.text ?? "", view, true);
        return;
      default:
        valid = false;
    }
  }

  const tokens = markdownTokens(manager, normalized);
  children(tokens, source, true);
  if (!valid) return null;
  const text = pieces.join("");
  const positions: number[] = [];
  let cursor = 0;
  doc.descendants((node, pos) => {
    if (!valid) return false;
    if (!node.isText || !node.text) return true;
    if (!text.startsWith(node.text, cursor)) {
      valid = false;
      return false;
    }
    for (let i = 0; i < node.text.length; i++) positions.push(pos + i);
    cursor += node.text.length;
    return true;
  });
  const monotonic = (values: number[]) =>
    values.every((value, i) => i === 0 || value >= values[i - 1]);
  return valid && cursor === text.length && monotonic(starts) && monotonic(ends)
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
    if (import.meta.env.DEV)
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
