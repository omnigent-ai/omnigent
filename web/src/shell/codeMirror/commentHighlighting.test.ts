import { EditorSelection, EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { describe, expect, it } from "vitest";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "../codeViewerHelpers";
import {
  ACTIVE_COMMENT_CLASS,
  COMMENT_CLASS,
  commentHighlighting,
  type CommentLayerState,
  selectionAnchor,
  selectionLineRange,
  setCommentLayer,
} from "./commentHighlighting";
import { rawDocument, rawDocumentOf, setRawDocument } from "./rawOffsets";

// Mixed line breaks: CRLF, a bare LF, and a lone CR.
const RAW = "first line\r\nsecond line\nthird\rfourth line\r\nlast";

function commentOn(raw: string, anchor: string): Comment {
  const start = raw.indexOf(anchor);
  return {
    id: anchor,
    conversation_id: "conv_1",
    path: "notes.md",
    start_index: start,
    end_index: start + anchor.length,
    body: "",
    status: "draft",
    created_at: 0,
    anchor_content: anchor,
  } as Comment;
}

function stateFor(raw: string, layer: CommentLayerState) {
  return EditorState.create({
    doc: raw,
    extensions: [rawDocument.init(() => rawDocumentOf(raw)), commentHighlighting(() => layer)],
  });
}

/** The text and class of every comment highlight in the state. */
function highlightedText(state: EditorState): [string, string][] {
  const highlighted: [string, string][] = [];
  for (const source of state.facet(EditorView.decorations)) {
    const set = typeof source === "function" ? source(new EditorView({ state })) : source;
    set.between(0, state.doc.length, (from, to, decoration) => {
      highlighted.push([state.sliceDoc(from, to), decoration.spec.class as string]);
    });
  }
  return highlighted;
}

describe("commentHighlighting", () => {
  it("highlights each comment's raw span in a file with mixed line breaks", () => {
    const spanning = commentOn(RAW, "line\nthird\rfourth");
    const state = stateFor(RAW, {
      comments: [commentOn(RAW, "second"), spanning],
      activeSelection: {
        start_index: spanning.start_index,
        end_index: spanning.end_index,
        anchor_content: "",
      },
    });
    expect(highlightedText(state)).toEqual([
      ["second", COMMENT_CLASS],
      ["line\nthird\nfourth", ACTIVE_COMMENT_CLASS],
    ]);
  });

  it("follows new comments and a refreshed raw file", () => {
    let state = stateFor(RAW, { comments: [], activeSelection: null });
    expect(highlightedText(state)).toEqual([]);
    state = state.update({
      effects: setCommentLayer.of({ comments: [commentOn(RAW, "last")], activeSelection: null }),
    }).state;
    expect(highlightedText(state)).toEqual([["last", COMMENT_CLASS]]);

    // The same lines saved with LF breaks: the offset now lands three earlier.
    const lfRaw = RAW.replace(/\r\n?/g, "\n");
    state = state.update({
      effects: [
        setRawDocument.of(rawDocumentOf(lfRaw)),
        setCommentLayer.of({ comments: [commentOn(lfRaw, "fourth")], activeSelection: null }),
      ],
    }).state;
    expect(highlightedText(state)).toEqual([["fourth", COMMENT_CLASS]]);
  });

  it("drops a comment past the end of the file", () => {
    const state = stateFor("short", {
      comments: [{ ...commentOn(RAW, "last"), start_index: 40, end_index: 44 }],
      activeSelection: null,
    });
    expect(highlightedText(state)).toEqual([]);
  });
});

describe("selectionAnchor", () => {
  it("anchors to raw offsets whose slice of the raw file is the selected text", () => {
    const state = stateFor(RAW, { comments: [], activeSelection: null });
    const from = state.doc.line(2).from + "second ".length;
    const to = state.doc.line(4).from + "fourth".length;
    const anchor = selectionAnchor(
      state.update({ selection: EditorSelection.range(from, to) }).state,
    ) as ActiveSelection;
    expect(anchor.anchor_content).toBe("line\nthird\rfourth");
    expect(RAW.slice(anchor.start_index, anchor.end_index)).toBe(anchor.anchor_content);
  });

  it("keeps CRLF breaks in the anchor", () => {
    const state = stateFor(RAW, { comments: [], activeSelection: null });
    const anchor = selectionAnchor(
      state.update({ selection: EditorSelection.range(6, state.doc.line(2).from + 6) }).state,
    ) as ActiveSelection;
    expect(anchor).toEqual({ start_index: 6, end_index: 18, anchor_content: "line\r\nsecond" });
  });

  it("ignores an empty or blank selection", () => {
    const state = stateFor("a   b", { comments: [], activeSelection: null });
    expect(selectionAnchor(state)).toBeNull();
    expect(
      selectionAnchor(state.update({ selection: EditorSelection.range(1, 4) }).state),
    ).toBeNull();
  });
});

describe("selectionLineRange", () => {
  const state = stateFor(RAW, { comments: [], activeSelection: null });
  const select = (from: number, to: number) =>
    selectionLineRange(state.update({ selection: EditorSelection.range(from, to) }).state);

  it("covers every line the selection touches", () => {
    expect(select(state.doc.line(1).from + 2, state.doc.line(3).from + 1)).toEqual({
      start: 1,
      end: 3,
    });
  });

  it("leaves out a last line the selection only reaches the start of", () => {
    expect(select(state.doc.line(1).from, state.doc.line(3).from)).toEqual({ start: 1, end: 2 });
    expect(select(state.doc.line(2).from + 1, state.doc.line(2).from + 3)).toEqual({
      start: 2,
      end: 2,
    });
  });

  it("is null without a selection", () => {
    expect(selectionLineRange(state)).toBeNull();
  });
});
