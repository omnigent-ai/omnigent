// Tests for useDictationInsert: caret-positioned insertion plus the
// replaceable interim region that lets server dictation stream live text
// into a plain-string draft.
//
// Invariant under test throughout: dictation must never delete text it
// didn't write. The interim region is replaced only while the draft is still
// verbatim the string the hook produced.
//
// The caret is read off a real textarea, so most tests render one and drive
// it (focus + setSelectionRange) the way a user's click would.

import { act, render, renderHook } from "@testing-library/react";
import { StrictMode, useRef, useState, type ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { useDictationInsert } from "./useDictationInsert";
import type { ComposerTextTarget } from "@/components/composer/engine/composerInput";
import {
  createFakeComposerSurface,
  type FakeComposerSurface,
} from "@/components/composer/engine/testing/fakeComposerSurface";

type Api = ReturnType<typeof useDictationInsert>;

/**
 * Render a composer-shaped textarea wired to the hook. Returns the live
 * textarea plus helpers: ``click`` places the caret the way a user does
 * (focus, then collapse the selection at an offset).
 */
function renderComposer(initial = "") {
  const box: {
    api: Api | null;
    ta: HTMLTextAreaElement | null;
    set: ((next: string) => void) | null;
  } = { api: null, ta: null, set: null };

  function Composer() {
    const [value, setValue] = useState(initial);
    const ref = useRef<HTMLTextAreaElement>(null);
    box.api = useDictationInsert(value, setValue, ref);
    box.set = setValue;
    return (
      <textarea
        ref={ref}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onFocus={() => box.api?.noteFocus()}
      />
    );
  }

  const { container } = render(<Composer />);
  box.ta = container.querySelector("textarea");
  const ta = box.ta!;
  return {
    get value() {
      return ta.value;
    },
    get caret() {
      return ta.selectionStart;
    },
    ta,
    appendFinal: (text: string) => act(() => box.api!.appendFinal(text)),
    replaceInterim: (text: string) => act(() => box.api!.replaceInterim(text)),
    /** Un-acted hook methods, for driving several inserts inside one act(). */
    get rawApi() {
      return box.api!;
    },
    /** Place the caret at *at*, as a user's click into the field would. */
    click: (at: number) =>
      act(() => {
        ta.focus();
        ta.setSelectionRange(at, at);
      }),
    /** Rewrite the draft from outside dictation (send, Esc revert, typing),
     *  through React, so the DOM and the hook's view stay in step. */
    setRaw: (next: string) => act(() => box.set!(next)),
  };
}

/** Hook-only harness for the cases that don't involve a caret at all. */
function renderDictation(
  initial = "",
  wrapper?: ({ children }: { children: ReactNode }) => ReactNode,
) {
  return renderHook(
    () => {
      const [value, setValue] = useState(initial);
      const dictation = useDictationInsert(value, setValue);
      return { value, setRaw: setValue, ...dictation };
    },
    wrapper ? { wrapper } : undefined,
  );
}

describe("useDictationInsert", () => {
  it("streams interim text as a replaceable trailing region", () => {
    const { result } = renderDictation();
    act(() => result.current.replaceInterim("hello"));
    expect(result.current.value).toBe("hello");
    act(() => result.current.replaceInterim("hello world"));
    expect(result.current.value).toBe("hello world");
    // Partials are revisable — a shorter rewrite replaces, never appends.
    act(() => result.current.replaceInterim("help"));
    expect(result.current.value).toBe("help");
  });

  it("finalizing replaces the interim and pins the text", () => {
    const { result } = renderDictation();
    act(() => result.current.replaceInterim("hello wor"));
    act(() => result.current.appendFinal("Hello, world."));
    expect(result.current.value).toBe("Hello, world.");
    // The finalized text is no longer part of any interim region.
    act(() => result.current.replaceInterim("next"));
    expect(result.current.value).toBe("Hello, world. next");
  });

  it("space-separates from an existing draft without doubling spaces", () => {
    const { result } = renderDictation("draft");
    act(() => result.current.replaceInterim("spoken"));
    expect(result.current.value).toBe("draft spoken");
    act(() => result.current.appendFinal("Spoken."));
    expect(result.current.value).toBe("draft Spoken.");

    const trailing = renderDictation("draft ");
    act(() => trailing.result.current.appendFinal("Spoken."));
    expect(trailing.result.current.value).toBe("draft Spoken.");
  });

  it("clearing the interim restores the base draft", () => {
    const { result } = renderDictation("draft");
    act(() => result.current.replaceInterim("partial words"));
    act(() => result.current.replaceInterim(""));
    expect(result.current.value).toBe("draft");
  });

  it("survives the draft shrinking underneath a pending interim", () => {
    const { result } = renderDictation();
    act(() => result.current.replaceInterim("some long partial"));
    // Send clears the draft out from under the pending interim region.
    act(() => result.current.setRaw(""));
    // A late update must not slice into (or resurrect) stale text.
    act(() => result.current.replaceInterim("after"));
    expect(result.current.value).toBe("after");
    act(() => result.current.appendFinal("After."));
    expect(result.current.value).toBe("After.");
  });

  it("never deletes text the user typed after the interim", () => {
    const composer = renderComposer();
    composer.replaceInterim("hello wor");
    // The user types after the pending partial.
    composer.setRaw("hello wor, urgent");
    // The draft is no longer what dictation produced → nothing is replaced;
    // the utterance inserts instead of slicing typed text away.
    composer.appendFinal("Hello world.");
    expect(composer.value).toContain(", urgent");
    expect(composer.value).toContain("Hello world.");
  });

  it("is StrictMode-safe (no double-inserted text)", () => {
    const strict = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;
    const { result } = renderDictation("draft", strict);
    act(() => result.current.replaceInterim("one"));
    act(() => result.current.replaceInterim("one two"));
    expect(result.current.value).toBe("draft one two");
    act(() => result.current.appendFinal("One, two."));
    expect(result.current.value).toBe("draft One, two.");
  });

  // The reported bug: dictated text always landed at the very bottom of the
  // draft, even with the caret placed above a pasted block of context.
  describe("caret-positioned insertion", () => {
    it("inserts at the user's caret, not the end of the draft", () => {
      const composer = renderComposer("PASTED CONTEXT");
      composer.click(0); // click above the pasted block
      composer.appendFinal("Refactor this:");
      expect(composer.value).toBe("Refactor this: PASTED CONTEXT");
    });

    it("streams partials in place at the caret", () => {
      const composer = renderComposer("PASTED CONTEXT");
      composer.click(0);
      composer.replaceInterim("refac");
      expect(composer.value).toBe("refac PASTED CONTEXT");
      // Revising a partial must not walk it down the draft.
      composer.replaceInterim("refactor thi");
      expect(composer.value).toBe("refactor thi PASTED CONTEXT");
      composer.appendFinal("Refactor this:");
      expect(composer.value).toBe("Refactor this: PASTED CONTEXT");
    });

    it("chains consecutive utterances after the previous one", () => {
      const composer = renderComposer("PASTED CONTEXT");
      composer.click(0);
      composer.appendFinal("First.");
      composer.appendFinal("Second.");
      expect(composer.value).toBe("First. Second. PASTED CONTEXT");
    });

    it("inserts mid-draft without fusing words on either side", () => {
      const composer = renderComposer("alpha omega");
      composer.click(6);
      composer.appendFinal("beta");
      expect(composer.value).toBe("alpha beta omega");
    });

    it("adds no space before punctuation that hugs the previous word", () => {
      const composer = renderComposer("Ship it, please.");
      composer.click(7); // right before the comma
      composer.appendFinal("today");
      expect(composer.value).toBe("Ship it today, please.");
    });

    it("sits flush inside an opening delimiter", () => {
      const composer = renderComposer("call(arg)");
      composer.click(5); // just after "("
      composer.appendFinal("the");
      expect(composer.value).toBe("call(the arg)");
    });

    it("keeps a space before an opening quote", () => {
      // Quotes are ambiguous, so they are spaced like any other character
      // rather than guessed at; fusing words is the worse failure.
      const composer = renderComposer('say "quoted"');
      composer.click(4); // before the opening quote
      composer.appendFinal("please");
      expect(composer.value).toBe('say please "quoted"');
    });

    it("respects a newline boundary without adding a space", () => {
      const composer = renderComposer("intro\nPASTED");
      composer.click(6);
      composer.appendFinal("Do this:");
      expect(composer.value).toBe("intro\nDo this: PASTED");
    });

    it("appends at the end while the composer has never been focused", () => {
      // Pre-existing behavior: an untouched draft has no caret on screen, so
      // selectionStart's default 0 must not be mistaken for one.
      const composer = renderComposer("restored draft");
      composer.appendFinal("Added.");
      expect(composer.value).toBe("restored draft Added.");
    });

    it("follows the caret when the user moves it between utterances", () => {
      const composer = renderComposer("one three");
      composer.click(3);
      composer.appendFinal("two");
      expect(composer.value).toBe("one two three");
      // User clicks back to the start and dictates again.
      composer.click(0);
      composer.appendFinal("Zero");
      expect(composer.value).toBe("Zero one two three");
    });

    it("leaves the caret after the inserted text, ready to keep typing", () => {
      const composer = renderComposer("PASTED CONTEXT");
      composer.click(0);
      composer.appendFinal("Refactor this:");
      expect(composer.caret).toBe("Refactor this:".length);
    });

    it("does not steal focus from the mic button", () => {
      const composer = renderComposer("draft");
      composer.click(0);
      // The mic button holds focus for its Enter-commit / Esc-cancel keys.
      act(() => composer.ta.blur());
      composer.appendFinal("Spoken.");
      expect(document.activeElement).not.toBe(composer.ta);
    });

    it("clamps a stale offset past the end of a shrunken draft", () => {
      const composer = renderComposer("a long draft");
      composer.click(12);
      // The draft is replaced wholesale (a send, or select-all + retype),
      // leaving the remembered offset past the end of the new one.
      composer.setRaw("ab");
      composer.click(2);
      composer.appendFinal("End.");
      expect(composer.value).toBe("ab End.");
    });

    it("follows a moved caret after the mic's end-of-take interim clear", () => {
      // ComposerMicButton ends a take with onInterim(""), which lands here as
      // a no-op once the preceding final has cleared the region. A same-value
      // setDraft can bail out without committing, so requesting a caret on that
      // path would leave the request outstanding forever and pin every later
      // utterance to the tail, ignoring wherever the user clicked.
      const composer = renderComposer("PASTED");
      composer.click(0);
      composer.appendFinal("Hello");
      expect(composer.value).toBe("Hello PASTED");
      composer.replaceInterim(""); // end of take
      expect(composer.value).toBe("Hello PASTED");
      composer.click(0);
      composer.appendFinal("Second.");
      expect(composer.value).toBe("Second. Hello PASTED");
    });

    it("pins a final whose text matches the partial already on screen", () => {
      // The server often finalizes exactly what it last streamed, so the splice
      // changes nothing. The update is still a pin: if the region stayed
      // pending, the next insert would lift the finalized text back out, and an
      // end-of-take clear would delete the dictated word outright.
      const composer = renderComposer("PASTED");
      composer.click(0);
      composer.replaceInterim("hello");
      expect(composer.value).toBe("hello PASTED");
      composer.appendFinal("hello");
      expect(composer.value).toBe("hello PASTED");
      composer.replaceInterim(""); // end of take
      expect(composer.value).toBe("hello PASTED");
      // ...and the next utterance continues after it, not in front of it.
      composer.appendFinal("Next.");
      expect(composer.value).toBe("hello Next. PASTED");
    });

    it("is inert when an empty clear arrives with nothing pending", () => {
      const composer = renderComposer("USER TEXT");
      composer.click(0);
      composer.replaceInterim("");
      expect(composer.value).toBe("USER TEXT");
      composer.appendFinal("Lead.");
      expect(composer.value).toBe("Lead. USER TEXT");
    });

    it("does not resurrect a spent region when the draft returns to its text", () => {
      // Undo (or retyping the same string) restores value equality with what
      // dictation produced, but those characters now belong to the user. A
      // value-equality ownership check would slice the old interim span back
      // out of the middle of the draft.
      const composer = renderComposer("KEEP");
      composer.click(0);
      composer.replaceInterim("partial");
      const produced = composer.value;
      expect(produced).toBe("partial KEEP");
      composer.setRaw("something else"); // user edits away
      composer.setRaw(produced); // ...and undoes back
      composer.replaceInterim("next");
      // "partial" is the user's text now, so it must survive.
      expect(composer.value).toContain("partial");
      expect(composer.value).toContain("next");
    });

    it("starts clean after an Esc discard reverts the draft", () => {
      // The composer snapshots the text on voice start and restores it on Esc
      // with a bare setValue, behind the hook's back. The next take must
      // insert at the caret, not resurrect or slice the reverted text.
      const composer = renderComposer("keep this");
      composer.click(0);
      composer.replaceInterim("throw away");
      expect(composer.value).toBe("throw away keep this");
      composer.setRaw("keep this"); // onVoiceDiscard
      composer.click(0);
      composer.appendFinal("Second try:");
      expect(composer.value).toBe("Second try: keep this");
    });

    it("keeps order when a partial and its final land in one batch", () => {
      // Both arrive from a single socket read, so the caret this hook asks for
      // hasn't been applied yet when the second insert computes its offset.
      const composer = renderComposer("PASTED CONTEXT");
      composer.click(0);
      act(() => {
        composer.rawApi.replaceInterim("first");
        composer.rawApi.appendFinal("Second.");
      });
      expect(composer.value).toBe("Second. PASTED CONTEXT");
    });

    it("chains two finals that land in one batch", () => {
      const composer = renderComposer("TAIL");
      composer.click(0);
      act(() => {
        composer.rawApi.appendFinal("One.");
        composer.rawApi.appendFinal("Two.");
      });
      expect(composer.value).toBe("One. Two. TAIL");
    });
  });
});

describe("useDictationInsert with a setter that writes through its input", () => {
  it.each([
    ["adjusts", (next: string) => `${next}\n`, ["hel\n", true, "hello\n"]],
    ["refuses", () => null, ["", false, ""]],
  ] as const)("revises only its own text when the input %s a write", (_how, write, expected) => {
    const { result } = renderHook(() => {
      const [value, setValue] = useState("");
      const dictation = useDictationInsert(value, (next) => {
        const written = write(next) ?? value;
        setValue(written);
        return written;
      });
      return { value, ...dictation };
    });
    act(() => result.current.replaceInterim("hel"));
    expect([result.current.value, result.current.hasInterim]).toEqual(expected.slice(0, 2));
    act(() => result.current.replaceInterim("hello"));
    expect(result.current.value).toBe(expected[2]);
  });

  it("keeps its region when a later revision is refused", () => {
    let refuse = false;
    const { result } = renderHook(() => {
      const [value, setValue] = useState("");
      const dictation = useDictationInsert(value, (next) => {
        const written = refuse ? value : next;
        setValue(written);
        return written;
      });
      return { value, ...dictation };
    });
    act(() => result.current.replaceInterim("hel"));
    refuse = true;
    act(() => result.current.replaceInterim("hell"));
    expect([result.current.value, result.current.hasInterim]).toEqual(["hel", true]);
    refuse = false;
    act(() => result.current.replaceInterim("hello"));
    expect(result.current.value).toBe("hello");
  });
});

describe("useDictationInsert's setter arguments and offsets", () => {
  it("tells the setter each change, and whether it pins an utterance", () => {
    const calls: unknown[][] = [];
    const { result } = renderHook(() => {
      const [value, setValue] = useState("Intro");
      const dictation = useDictationInsert(value, (next, change, final) => {
        calls.push([change, final]);
        setValue(next);
      });
      return { value, ...dictation };
    });
    act(() => result.current.replaceInterim("hel"));
    act(() => result.current.appendFinal("hello"));
    expect(result.current.value).toBe("Intro hello");
    expect(calls).toEqual([
      [{ from: 5, to: 5, insert: " hel" }, false],
      [{ from: 5, to: 9, insert: " hello" }, true],
    ]);
  });

  it.each([
    [
      "keeps",
      "before",
      (next: string) => next.replace("Intro", "INTRO"),
      ["INTRO hel", true, "INTRO hello"],
    ],
    [
      "drops",
      "across",
      (next: string) => next.toUpperCase(),
      ["INTRO HEL", false, "INTRO HEL HELLO"],
    ],
  ] as const)(
    "%s a partial's region when the input rewrites the text %s it",
    (_keeps, _where, write, expected) => {
      const { result } = renderHook(() => {
        const [value, setValue] = useState("Intro");
        const dictation = useDictationInsert(value, (next) => {
          const written = write(next);
          setValue(written);
          return written;
        });
        return { value, ...dictation };
      });
      act(() => result.current.replaceInterim("hel"));
      expect([result.current.value, result.current.hasInterim]).toEqual(expected.slice(0, 2));
      act(() => result.current.replaceInterim("hello"));
      expect(result.current.value).toBe(expected[2]);
    },
  );
});

describe("useDictationInsert on a non-textarea composer surface", () => {
  const mountedSurfaces: FakeComposerSurface[] = [];
  afterEach(() => {
    for (const surface of mountedSurfaces.splice(0)) {
      surface.unregister();
      surface.element.remove();
    }
  });

  function renderSurfaceComposer(initial: string) {
    const surface = createFakeComposerSurface(initial);
    mountedSurfaces.push(surface);
    const box: { api: Api | null } = { api: null };
    function Composer() {
      const [value, setValue] = useState(initial);
      // The engine mirrors the controlled value, as a real editor would after render.
      surface.value = value;
      const ref = useRef<ComposerTextTarget>(surface);
      box.api = useDictationInsert(value, setValue, ref);
      return null;
    }
    render(<Composer />);
    return {
      surface,
      api: () => box.api!,
      click: (at: number) =>
        act(() => {
          surface.focus();
          surface.setSelectionRange(at, at);
          box.api!.noteFocus();
        }),
    };
  }

  it("inserts at the caret read across blur and places the caret after it without focusing", () => {
    const composer = renderSurfaceComposer("PASTED CONTEXT");
    composer.click(0);
    act(() => composer.surface.blur());
    composer.surface.scrollTop = 40;
    // Browsers scroll an unfocused element to reveal a new selection; the hook must undo that.
    const setSelection = composer.surface.setSelectionRange;
    composer.surface.setSelectionRange = (start, end) => {
      setSelection(start, end);
      composer.surface.scrollTop = 0;
    };

    act(() => composer.api().appendFinal("Refactor this:"));

    expect(composer.surface.value).toBe("Refactor this: PASTED CONTEXT");
    expect(composer.surface.selectionStart).toBe("Refactor this:".length);
    expect(document.activeElement).not.toBe(composer.surface.element);
    expect(composer.surface.scrollTop).toBe(40);
  });

  it("revises a streaming partial in place at the caret", () => {
    const composer = renderSurfaceComposer("PASTED CONTEXT");
    composer.click(0);

    act(() => composer.api().replaceInterim("refac"));
    act(() => composer.api().replaceInterim("refactor thi"));

    expect(composer.surface.value).toBe("refactor thi PASTED CONTEXT");
    expect(composer.surface.selectionStart).toBe("refactor thi".length);
  });
});
