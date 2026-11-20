// The single edit that turns one snapshot into the next, so a refresh keeps
// scroll, selection, and decorations anchored outside the changed region.

import { Text, type TransactionSpec } from "@codemirror/state";
import type { EditorView } from "@codemirror/view";
import { documentTextOf, rawDocumentOf, setRawDocument } from "./rawOffsets";

export interface TextChange {
  from: number;
  to: number;
  insert: string;
}

/** Replaces only the span between the snapshots' common prefix and suffix. */
export function textChange(previous: string, next: string): TextChange | null {
  if (previous === next) return null;
  const limit = Math.min(previous.length, next.length);
  let prefix = 0;
  while (prefix < limit && previous.charCodeAt(prefix) === next.charCodeAt(prefix)) prefix += 1;
  let suffix = 0;
  while (
    suffix < limit - prefix &&
    previous.charCodeAt(previous.length - 1 - suffix) === next.charCodeAt(next.length - 1 - suffix)
  ) {
    suffix += 1;
  }
  return {
    from: prefix,
    to: previous.length - suffix,
    insert: next.slice(prefix, next.length - suffix),
  };
}

/**
 * Loads newer raw file content into `view`, keeping the reader's place outside
 * the changed lines. The raw file changes with it, even when only its line breaks differ.
 */
export function documentRefresh(view: EditorView, raw: string): TransactionSpec {
  const change = textChange(view.state.doc.toString(), documentTextOf(raw));
  const changes = view.state.changes(
    change ? { ...change, insert: Text.of(change.insert.split("\n")) } : [],
  );
  const scrollSnapshot = view.scrollSnapshot().map(changes);
  return {
    changes,
    effects: [setRawDocument.of(rawDocumentOf(raw)), ...(scrollSnapshot ? [scrollSnapshot] : [])],
  };
}
