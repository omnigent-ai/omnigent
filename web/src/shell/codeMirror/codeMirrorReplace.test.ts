import { SearchQuery, getSearchQuery, search, setSearchQuery } from "@codemirror/search";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { undo, history } from "@codemirror/commands";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FindMatchIndex } from "./codeMirrorFind";
import { FindReplacer, replacementText } from "./codeMirrorReplace";
import { type FindScheduler, inlineMatchSource } from "./findMatchSource";

const views: EditorView[] = [];
afterEach(() => {
  for (const view of views.splice(0)) view.destroy();
  vi.restoreAllMocks();
});

/** A scheduler the test runs by hand; every clock read passes the slice budget. */
function manualScheduler() {
  const queue: (() => void)[] = [];
  let time = 0;
  const scheduler: FindScheduler = {
    schedule: (task) => {
      queue.push(task);
      return () => {
        const at = queue.indexOf(task);
        if (at >= 0) queue.splice(at, 1);
      };
    },
    now: () => (time += 100),
  };
  return {
    scheduler,
    pending: () => queue.length,
    drain: () => {
      let slices = 0;
      while (queue.length) {
        queue.shift()!();
        slices += 1;
      }
      return slices;
    },
  };
}

function editor(doc: string, findText: string, replace: string, regexp = false) {
  const view = new EditorView({
    state: EditorState.create({ doc, extensions: [history(), search()] }),
  });
  views.push(view);
  view.dispatch({
    effects: setSearchQuery.of(new SearchQuery({ search: findText, replace, regexp })),
  });
  const indexing = manualScheduler();
  const index = new FindMatchIndex(() => {}, inlineMatchSource(indexing.scheduler));
  index.sync(view.state);
  const preparing = manualScheduler();
  const replacer = new FindReplacer(index, preparing.scheduler);
  return { view, index, indexing, preparing, replacer };
}

describe("replacementText", () => {
  const regex = (replace: string) => new SearchQuery({ search: "x", replace, regexp: true });
  const match = (...groups: (string | undefined)[]) =>
    Object.assign([...groups], { index: 0, input: "" }) as unknown as RegExpExecArray;

  it("fills groups, the whole match and dollar signs", () => {
    expect(replacementText(regex("[$2-$1] $& $$"), match("ab", "a", "b"))).toBe("[b-a] ab $");
  });

  it("leaves a group that didn't take part empty", () => {
    expect(replacementText(regex("<$1>"), match("beta", undefined))).toBe("<>");
    expect(replacementText(regex("$1$2"), match("b", undefined, "b"))).toBe("b");
  });

  it("reads escapes but no groups in a plain-text replacement", () => {
    const plain = new SearchQuery({ search: "x", replace: "a\\tb $1" });
    expect(replacementText(plain, null)).toBe("a\tb $1");
  });
});

describe("FindReplacer", () => {
  it("waits for an unfinished index, then replaces every match as one undoable edit", () => {
    const { view, index, indexing, replacer } = editor("a b a b a", "a", "z");
    replacer.replaceAll(view);
    expect(replacer.busy).toBe(true);
    expect(view.state.doc.toString()).toBe("a b a b a");
    indexing.drain();
    replacer.resume(view);
    expect(view.state.doc.toString()).toBe("z b z b z");
    expect(replacer.busy).toBe(false);
    index.sync(view.state);
    undo(view);
    expect(view.state.doc.toString()).toBe("a b a b a");
  });

  it("drops a waiting Replace all once the document or the query changes", () => {
    const edited = editor("a b a", "a", "z");
    edited.replacer.replaceAll(edited.view);
    edited.view.dispatch({ changes: { from: 0, insert: "a " } });
    edited.indexing.drain();
    edited.replacer.resume(edited.view);
    expect(edited.view.state.doc.toString()).toBe("a a b a");
    expect(edited.replacer.busy).toBe(false);

    const requeried = editor("a b a", "a", "z");
    requeried.replacer.replaceAll(requeried.view);
    const { search: text, regexp } = getSearchQuery(requeried.view.state);
    requeried.view.dispatch({
      effects: setSearchQuery.of(new SearchQuery({ search: text, regexp, replace: "y" })),
    });
    requeried.indexing.drain();
    requeried.replacer.resume(requeried.view);
    expect(requeried.view.state.doc.toString()).toBe("a b a");
  });

  it("reads groups in slices with one pass over a long line, as the index does", () => {
    const line = Array.from({ length: 400 }, (_unused, index) => `k${index}=v${index}`).join(";");
    const { view, preparing, replacer } = editor(line, "(k\\d+)=(v\\d+)", "$2=$1", true);
    const getCursor = vi.spyOn(SearchQuery.prototype, "getCursor");
    replacer.replaceAll(view);
    // The first slice ran; the rest wait their turn.
    expect(view.state.doc.toString()).toBe(line);
    expect(replacer.busy).toBe(true);
    expect(preparing.drain()).toBeGreaterThan(1);
    expect(view.state.doc.toString()).toBe(
      Array.from({ length: 400 }, (_unused, index) => `v${index}=k${index}`).join(";"),
    );
    expect(getCursor).toHaveBeenCalledTimes(1);
  });

  it("uses the index's positions for a plain replacement without reading the document", () => {
    const { view, indexing, replacer } = editor("needle ".repeat(500), "needle", "pin");
    indexing.drain();
    const getCursor = vi.spyOn(SearchQuery.prototype, "getCursor");
    replacer.replaceAll(view);
    expect(view.state.doc.toString()).toBe("pin ".repeat(500));
    expect(getCursor).not.toHaveBeenCalled();
  });

  /** Selects the match at `from`–`to` and replaces it, returning where Next should look from. */
  function replaceAt(setup: ReturnType<typeof editor>, from: number, to: number) {
    setup.indexing.drain();
    setup.view.dispatch({ selection: { anchor: from, head: to } });
    const resumed: (number | null)[] = [];
    setup.replacer.replaceSelected(setup.view, (resumeFrom) => resumed.push(resumeFrom));
    setup.preparing.drain();
    return resumed;
  }

  it.each([
    ["a lookahead past the match", "foobar", "(foo)(?=bar)", "$1!", [0, 3, 4], "foo!bar"],
    [
      "a lookbehind across lines",
      "before\nfoo",
      "(?<=before\\n)(foo)",
      "$1!",
      [7, 10, 11],
      "before\nfoo!",
    ],
    ["the alternative that matched", "ab", "(a)(?!b)|(a)", "[$1$2]", [0, 1, 3], "[a]b"],
  ])(
    "keeps %s when replacing one match or all",
    (_label, doc, pattern, insert, positions, result) => {
      const [from, to, resumeFrom] = positions as [number, number, number];
      const one = editor(doc, pattern, insert, true);
      expect(replaceAt(one, from, to)).toEqual([resumeFrom]);
      expect(one.view.state.doc.toString()).toBe(result);

      const all = editor(doc, pattern, insert, true);
      all.replacer.replaceAll(all.view);
      all.preparing.drain();
      expect(all.view.state.doc.toString()).toBe(result);
    },
  );

  it("leaves a match that covers part of a character alone", () => {
    const one = editor("café e", "e", "X");
    one.indexing.drain();
    expect(one.index.isPrecise(3)).toBe(false);
    expect(replaceAt(one, 3, 4)).toEqual([null]);
    expect(one.view.state.doc.toString()).toBe("café e");

    const all = editor("café e", "e", "X");
    all.indexing.drain();
    all.replacer.replaceAll(all.view);
    expect(all.view.state.doc.toString()).toBe("café X");
  });

  it("replaces only a selected match, and only in an editable file", () => {
    const setup = editor("a b a", "a", "z");
    expect(replaceAt(setup, 2, 3)).toEqual([null]);
    expect(replaceAt(setup, 4, 5)).toEqual([5]);
    expect(setup.view.state.doc.toString()).toBe("a b z");

    const readOnly = new EditorView({
      state: EditorState.create({ doc: "a", extensions: [EditorState.readOnly.of(true)] }),
    });
    views.push(readOnly);
    readOnly.dispatch({ selection: { anchor: 0, head: 1 } });
    const resumed: (number | null)[] = [];
    new FindReplacer(setup.index).replaceSelected(readOnly, (resumeFrom) =>
      resumed.push(resumeFrom),
    );
    expect(resumed).toEqual([null]);
  });

  it("drops a Replace that reads groups if the file changes before it finishes", () => {
    const doc = "x\n".repeat(200) + "foo";
    // Every "x" line is a match the read passes on its way to "foo", a slice at a time.
    const setup = editor(doc, "(x)\\n|(foo)", "<$2>", true);
    setup.indexing.drain();
    const from = doc.length - 3;
    setup.view.dispatch({ selection: { anchor: from, head: doc.length } });
    const resumed: (number | null)[] = [];
    setup.replacer.replaceSelected(setup.view, (resumeFrom) => resumed.push(resumeFrom));
    expect(setup.replacer.busy).toBe(true);
    setup.view.dispatch({ changes: { from: 0, insert: "y" } });
    setup.preparing.drain();
    expect(resumed).toEqual([]);
    expect(setup.view.state.doc.toString()).toBe(`y${doc}`);
  });
});
