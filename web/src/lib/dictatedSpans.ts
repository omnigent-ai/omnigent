// The text a dictation take wrote, as spans of the composer's document. Spans follow the edits
// made around them, and a span an edit touches is no longer dictation's alone, so it is dropped.

import type { ComposerTextTarget } from "@/components/composer/engine/composerInput";
import { insertComposerText } from "@/lib/composerText";
import { minimalTextChange, type TextEdit } from "@/lib/replyDraftDocument";

export interface DictatedSpan {
  from: number;
  to: number;
}

/** The spans after `edit`. Text before or after an edit keeps its span; an overlapped span goes. */
export function mapDictatedSpans(spans: readonly DictatedSpan[], edit: TextEdit | null) {
  if (edit === null) return [...spans];
  const delta = edit.insert.length - (edit.to - edit.from);
  return spans.flatMap((span): DictatedSpan[] => {
    if (span.to <= edit.from) return [span];
    if (span.from >= edit.to) return [{ from: span.from + delta, to: span.to + delta }];
    return [];
  });
}

/** The spans after the document changed from `before` to `after` by an edit the spans didn't make. */
export function mapDictatedSpansAcrossDocuments(
  spans: readonly DictatedSpan[],
  before: string,
  after: string,
): DictatedSpan[] {
  return mapDictatedSpans(spans, minimalTextChange(before, after));
}

/**
 * Apply a dictation write: `change` replaces the text it owns, and the text it inserts becomes a
 * span. The editor may normalize the write (a pinned seed moves text below it), so the spans follow
 * that normalization too. Returns the spans, which are never empty.
 */
export function writeDictatedEdit(
  target: ComposerTextTarget,
  spans: readonly DictatedSpan[],
  next: string,
  change: TextEdit,
): DictatedSpan[] {
  const mapped = mapDictatedSpans(spans, change);
  insertComposerText(target, change.insert, change.from, change.to);
  const written: DictatedSpan = { from: change.from, to: change.from + change.insert.length };
  const kept = mapDictatedSpans([...mapped, written], minimalTextChange(next, target.value));
  return kept.filter((span) => span.to > span.from);
}

/** Removes the dictated text from the editor, last span first. Nothing is written when none is owned. */
export function discardDictatedSpans(target: ComposerTextTarget, spans: readonly DictatedSpan[]) {
  for (const span of [...spans].sort((a, b) => b.from - a.from)) {
    insertComposerText(target, "", span.from, span.to);
  }
}
