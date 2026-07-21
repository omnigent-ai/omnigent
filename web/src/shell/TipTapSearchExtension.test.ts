// Unit tests for the find-in-file search-decoration ProseMirror plugin.
//
// Coverage:
//   - searchDecorationKey is a stable PluginKey.
//   - findMatches returns case-insensitive, per-text-node ranges.
//   - A real headless Editor wired with createSearchDecorationExtension renders
//     .md-search-match spans for every match and .md-search-match-current on the
//     active one, follows currentIndex, clears when the query is empty, and
//     re-runs the search after a "rebuild" meta dispatch.
//
// Uses a real TipTap Editor (real schema + real @tiptap/markdown parsing) so
// regressions in decoration / remap behaviour fail the test.

import { afterEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { Markdown } from "@tiptap/markdown";
import StarterKit from "@tiptap/starter-kit";
import { PluginKey } from "@tiptap/pm/state";
import type { RefObject } from "react";
import {
  createSearchDecorationExtension,
  findMatches,
  searchDecorationKey,
  type SearchDecorationState,
} from "./TipTapSearchExtension";

let editor: Editor | null = null;
afterEach(() => {
  editor?.destroy();
  editor = null;
});

// "the" appears twice (case-insensitively): "The" at the start and "the" before "lazy".
const CONTENT = "The quick brown fox jumps over the lazy dog.";

function makeStateRef(
  state: SearchDecorationState | null,
): RefObject<SearchDecorationState | null> {
  return { current: state };
}

function makeEditor(stateRef: RefObject<SearchDecorationState | null>, content = CONTENT): Editor {
  return new Editor({
    element: document.createElement("div"),
    extensions: [StarterKit, createSearchDecorationExtension(stateRef), Markdown],
    content,
    contentType: "markdown",
  });
}

/** Re-run the search plugin against the current stateRef. */
function rebuild(ed: Editor) {
  ed.view.dispatch(ed.state.tr.setMeta(searchDecorationKey, "rebuild"));
}

// ---------------------------------------------------------------------------
// searchDecorationKey
// ---------------------------------------------------------------------------

describe("searchDecorationKey", () => {
  it("is a PluginKey", () => {
    expect(searchDecorationKey).toBeInstanceOf(PluginKey);
  });
});

// ---------------------------------------------------------------------------
// findMatches
// ---------------------------------------------------------------------------

describe("findMatches", () => {
  it("finds all case-insensitive occurrences", () => {
    const stateRef = makeStateRef(null);
    editor = makeEditor(stateRef);
    // "the" matches "The" (start) and "the" (before lazy) — 2 hits.
    expect(findMatches(editor.state.doc, "the")).toHaveLength(2);
  });

  it("returns an empty array for an empty query", () => {
    const stateRef = makeStateRef(null);
    editor = makeEditor(stateRef);
    expect(findMatches(editor.state.doc, "")).toEqual([]);
  });

  it("returns an empty array when nothing matches", () => {
    const stateRef = makeStateRef(null);
    editor = makeEditor(stateRef);
    expect(findMatches(editor.state.doc, "zzz")).toEqual([]);
  });

  it("finds multiple occurrences within a single text node", () => {
    const stateRef = makeStateRef(null);
    editor = makeEditor(stateRef, "aaa");
    // "aa" at offset 0 and 2 do NOT overlap (search advances past each match),
    // so only one non-overlapping match is found in "aaa".
    expect(findMatches(editor.state.doc, "aa")).toHaveLength(1);
    expect(findMatches(editor.state.doc, "a")).toHaveLength(3);
  });

  it("matches a term spanning a formatting boundary", () => {
    const stateRef = makeStateRef(null);
    // "Hello" split across a bold boundary → two adjacent inline text nodes
    // "Hel" and "lo". The visible-text map concatenates them within the block.
    editor = makeEditor(stateRef, "Hel**lo** world");
    const matches = findMatches(editor.state.doc, "hello");
    expect(matches).toHaveLength(1);
    // The mapped range must round-trip to the original visible text.
    const { from, to } = matches[0];
    expect(editor.state.doc.textBetween(from, to)).toBe("Hello");
  });

  it("does not match across a block boundary", () => {
    const stateRef = makeStateRef(null);
    // "foobar" split across two paragraphs — a block separator sits between
    // them, so the concatenated "foo\nbar" must not yield a "foobar" match.
    editor = makeEditor(stateRef, "foo\n\nbar");
    expect(findMatches(editor.state.doc, "foobar")).toHaveLength(0);
    // Each half still matches on its own.
    expect(findMatches(editor.state.doc, "foo")).toHaveLength(1);
    expect(findMatches(editor.state.doc, "bar")).toHaveLength(1);
  });

  it("keeps positions aligned after a length-changing case-fold character", () => {
    const stateRef = makeStateRef(null);
    // "İ" (U+0130) lowercases to two UTF-16 units ("i" + combining U+0307).
    // A plain toLowerCase() haystack would desync from the original-text
    // coordinate map and shift/invalidate the match after it. The word
    // following it must still map to its exact original range.
    editor = makeEditor(stateRef, "İstanbul word");
    const matches = findMatches(editor.state.doc, "word");
    expect(matches).toHaveLength(1);
    const { from, to } = matches[0];
    expect(editor.state.doc.textBetween(from, to)).toBe("word");
  });
});

// ---------------------------------------------------------------------------
// Decoration rendering
// ---------------------------------------------------------------------------

describe("createSearchDecorationExtension decorations", () => {
  it("renders a .md-search-match span for every match", () => {
    const stateRef = makeStateRef({ query: "the", currentIndex: 0 });
    editor = makeEditor(stateRef);
    const spans = editor.view.dom.querySelectorAll(".md-search-match");
    expect(spans).toHaveLength(2);
  });

  it("marks the current match with .md-search-match-current", () => {
    const stateRef = makeStateRef({ query: "the", currentIndex: 0 });
    editor = makeEditor(stateRef);
    const current = editor.view.dom.querySelectorAll(".md-search-match-current");
    expect(current).toHaveLength(1);
    // currentIndex 0 → the first "The".
    expect(current[0].textContent?.toLowerCase()).toBe("the");
  });

  it("advances the current match with currentIndex", () => {
    const stateRef = makeStateRef({ query: "the", currentIndex: 1 });
    editor = makeEditor(stateRef);
    const all = Array.from(editor.view.dom.querySelectorAll(".md-search-match"));
    const currentIdx = all.findIndex((el) => el.classList.contains("md-search-match-current"));
    // Second match is the current one.
    expect(currentIdx).toBe(1);
  });

  it("wraps currentIndex modulo the match count", () => {
    // currentIndex 2 with 2 matches wraps back to the first.
    const stateRef = makeStateRef({ query: "the", currentIndex: 2 });
    editor = makeEditor(stateRef);
    const all = Array.from(editor.view.dom.querySelectorAll(".md-search-match"));
    const currentIdx = all.findIndex((el) => el.classList.contains("md-search-match-current"));
    expect(currentIdx).toBe(0);
  });

  it("renders nothing for an empty query", () => {
    const stateRef = makeStateRef({ query: "", currentIndex: 0 });
    editor = makeEditor(stateRef);
    expect(editor.view.dom.querySelectorAll(".md-search-match")).toHaveLength(0);
  });

  it("re-runs the search after a rebuild meta dispatch", () => {
    const stateRef = makeStateRef({ query: "", currentIndex: 0 });
    editor = makeEditor(stateRef);
    expect(editor.view.dom.querySelectorAll(".md-search-match")).toHaveLength(0);

    // Mutate the shared state, then rebuild — the new query must paint.
    stateRef.current = { query: "fox", currentIndex: 0 };
    rebuild(editor);
    expect(editor.view.dom.querySelectorAll(".md-search-match")).toHaveLength(1);
  });

  it("clears highlights when the query is reset to empty via rebuild", () => {
    const stateRef = makeStateRef({ query: "the", currentIndex: 0 });
    editor = makeEditor(stateRef);
    expect(editor.view.dom.querySelectorAll(".md-search-match")).toHaveLength(2);

    stateRef.current = { query: "", currentIndex: 0 };
    rebuild(editor);
    expect(editor.view.dom.querySelectorAll(".md-search-match")).toHaveLength(0);
  });
});
