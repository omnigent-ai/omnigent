// Tests for useDictationInsert — the replaceable trailing interim region
// that lets server dictation stream live text into a plain-string draft.
//
// Invariant under test throughout: dictation must never delete text it
// didn't write. The interim region is stripped only when the draft still
// ends with the exact text the hook inserted.

import { act, renderHook } from "@testing-library/react";
import { StrictMode, useState, type ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { useDictationInsert } from "./useDictationInsert";

/** Harness pairing the hook with the same useState shape the composers use. */
function renderDictation(
  initial = "",
  wrapper?: ({ children }: { children: ReactNode }) => ReactNode,
) {
  return renderHook(
    () => {
      const [value, setValue] = useState(initial);
      const dictation = useDictationInsert(setValue);
      return { value, setRaw: (next: string) => setValue(() => next), ...dictation };
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
    const { result } = renderDictation();
    act(() => result.current.replaceInterim("hello wor"));
    // The user clicks into the composer and types after the interim.
    act(() => result.current.setRaw("hello wor, urgent"));
    // The draft no longer ends with the tracked interim → nothing is
    // stripped; the update appends instead of slicing typed text away.
    act(() => result.current.appendFinal("Hello world."));
    expect(result.current.value).toBe("hello wor, urgent Hello world.");
  });

  it("is StrictMode-safe (updaters are pure; double-invoke is a no-op)", () => {
    const strict = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;
    const { result } = renderDictation("draft", strict);
    act(() => result.current.replaceInterim("one"));
    act(() => result.current.replaceInterim("one two"));
    expect(result.current.value).toBe("draft one two");
    act(() => result.current.appendFinal("One, two."));
    expect(result.current.value).toBe("draft One, two.");
  });
});
