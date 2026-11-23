// CodeMirror counts every line break as one position, while comments anchor
// to offsets in the raw file, where "\r\n" is two. This maps between them.

import { type EditorState, StateEffect, StateField, Text } from "@codemirror/state";
import { EditorView } from "@codemirror/view";

/** Converts between raw file offsets and CodeMirror document positions. */
export class RawOffsetMap {
  /** Document positions of each "\r\n" break, ascending. */
  private readonly crlfBreaks: number[];

  private constructor(crlfBreaks: number[]) {
    this.crlfBreaks = crlfBreaks;
  }

  static of(raw: string): RawOffsetMap {
    const breaks: number[] = [];
    let shift = 0;
    for (let index = raw.indexOf("\r\n"); index !== -1; index = raw.indexOf("\r\n", index + 2)) {
      breaks.push(index - shift);
      shift += 1;
    }
    return new RawOffsetMap(breaks);
  }

  /** The document position of a raw offset; an offset inside "\r\n" maps to its break. */
  toDocument(raw: number): number {
    // Each break whose "\r" lies before `raw` shortens the document by one.
    let low = 0;
    let high = this.crlfBreaks.length;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if (this.crlfBreaks[middle]! + middle < raw) low = middle + 1;
      else high = middle;
    }
    return raw - low;
  }

  /** The raw offset of a document position. */
  toRaw(position: number): number {
    let low = 0;
    let high = this.crlfBreaks.length;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if (this.crlfBreaks[middle]! < position) low = middle + 1;
      else high = middle;
    }
    return position + low;
  }
}

/** The file as the server returned it, kept beside the document it was loaded into. */
export interface RawDocument {
  text: string;
  offsets: RawOffsetMap;
}

export function rawDocumentOf(text: string): RawDocument {
  return { text, offsets: RawOffsetMap.of(text) };
}

/** Replaces the raw file in the same transaction that loads it into the document. */
export const setRawDocument = StateEffect.define<RawDocument>();

export const rawDocument = StateField.define<RawDocument>({
  create: (state) => rawDocumentOf(state.doc.toString()),
  update(value, transaction) {
    for (const effect of transaction.effects) {
      if (effect.is(setRawDocument)) return effect.value;
    }
    return value;
  },
});

/** The document text for raw file content, with every line break as "\n". */
export function documentTextOf(raw: string): string {
  return raw.replace(/\r\n?/g, "\n");
}

/**
 * The selected text with the file's own line breaks: from the raw file while
 * the buffer still matches it, otherwise from the edited buffer.
 */
export function rawSelectionText(state: EditorState): string {
  const raw = state.field(rawDocument);
  const ranges = state.selection.ranges.filter((range) => !range.empty);
  if (state.doc.eq(loadedDocumentOf(raw))) {
    return ranges
      .map((range) => raw.text.slice(raw.offsets.toRaw(range.from), raw.offsets.toRaw(range.to)))
      .join("\n");
  }
  // Unsaved edits: copy the buffer, with the line break the file will be saved with.
  const lineBreak = preferredLineBreak(raw.text);
  return ranges
    .map((range) => state.doc.sliceString(range.from, range.to, lineBreak))
    .join(lineBreak);
}

// The document each raw file was loaded as, computed once per file when first needed.
const loadedDocuments = new WeakMap<RawDocument, Text>();

/** The document lines the raw file had when it was loaded or saved. */
function loadedDocumentOf(raw: RawDocument): Text {
  let loaded = loadedDocuments.get(raw);
  if (!loaded) {
    loaded = documentOf(raw.text);
    loadedDocuments.set(raw, loaded);
  }
  return loaded;
}

/** Copies a selection with the file's own line breaks; an empty selection copies as usual. */
export const copyRawSelection = EditorView.domEventHandlers({
  copy(event, view) {
    if (!event.clipboardData || view.state.selection.ranges.every((range) => range.empty)) {
      return false;
    }
    event.clipboardData.setData("text/plain", rawSelectionText(view.state));
    event.preventDefault();
    return true;
  },
});

/** The line break a saved file uses: "\r\n" when most of the file's breaks are, as Monaco picks. */
export function preferredLineBreak(raw: string): "\n" | "\r\n" {
  let crlf = 0;
  let other = 0;
  for (let index = 0; index < raw.length; index += 1) {
    const code = raw.charCodeAt(index);
    if (code === 13 && raw.charCodeAt(index + 1) === 10) {
      crlf += 1;
      index += 1;
    } else if (code === 10 || code === 13) {
      other += 1;
    }
  }
  return crlf > other ? "\r\n" : "\n";
}

/** The file text to save for `doc`, with every line break as `lineBreak`. */
export function savedTextOf(doc: Text, lineBreak: "\n" | "\r\n"): string {
  return doc.sliceString(0, doc.length, lineBreak);
}

/** The document lines of raw file content, splitting on every kind of line break. */
export function documentOf(raw: string): Text {
  return Text.of(raw.split(/\r\n?|\n/));
}
