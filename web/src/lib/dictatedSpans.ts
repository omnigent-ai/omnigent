// The text a dictation take wrote, as spans of the composer's document. Spans follow the edits
// made around them, and a span an edit touches is no longer dictation's alone, so it is dropped.

import type { ComposerTextTarget } from "@/components/composer/engine/composerInput";
import { insertComposerText } from "@/lib/composerText";
import { minimalTextChange, parseReplyQuoteLine, type TextEdit } from "@/lib/replyDraftDocument";

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
 * span. The spans follow what the editor actually did, which can differ from `change` when it
 * normalizes the write, so the editor's text is diffed before and after, never the expected text.
 * Returns the surviving non-empty spans (possibly none).
 */
export function writeDictatedEdit(
  target: ComposerTextTarget,
  spans: readonly DictatedSpan[],
  change: TextEdit,
): DictatedSpan[] {
  const before = target.value;
  insertComposerText(target, change.insert, change.from, change.to);
  const edit = minimalTextChange(before, target.value);
  // A span the write replaces is no longer dictation's; the diff alone can't tell, since it may
  // keep an old span that only touches the edit's start.
  const outside = spans.filter((span) =>
    change.from === change.to
      ? !(span.from < change.from && change.from < span.to)
      : span.to <= change.from || span.from >= change.to,
  );
  if (edit === null) return outside.filter((span) => span.to > span.from);
  const mapped = mapDictatedSpans(outside, edit);
  const delta = edit.insert.length - (edit.to - edit.from);
  // A pure insert lands where the editor put it, which is where the diff shows it. A replacement
  // keeps its start, shifted only by an edit the editor made before it.
  const from =
    change.from === change.to && edit.insert === change.insert
      ? edit.from
      : change.from + (edit.to <= change.from ? delta : 0);
  const written: DictatedSpan = { from, to: from + change.insert.length };
  return [...mapped, written].filter((span) => span.to > span.from);
}

/** Removes the dictated text from the editor, last span first. Nothing is written when none is owned. */
export function discardDictatedSpans(target: ComposerTextTarget, spans: readonly DictatedSpan[]) {
  for (const span of [...spans].sort((a, b) => b.from - a.from)) {
    insertComposerText(target, "", span.from, span.to);
  }
}

/**
 * Where the editor keeps dictated text written at `at`. A quote's line is a card, and text written
 * on it moves to the line after the card, as typed text does; dictation writes there directly, so
 * the draft it keeps is the document the editor shows.
 */
export function dictationLanding(doc: string, at: number): number {
  const lineStart = doc.lastIndexOf("\n", at - 1) + 1;
  const lineEnd = doc.indexOf("\n", at);
  const end = lineEnd === -1 ? doc.length : lineEnd;
  if (parseReplyQuoteLine(doc.slice(lineStart, end)) === null) return at;
  // A card with no line after it has nowhere to land; the editor keeps the write where it is.
  return end < doc.length ? end + 1 : at;
}
