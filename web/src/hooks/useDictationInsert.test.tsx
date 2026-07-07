// Tests for useDictationInsert — the replaceable trailing interim region
// that lets server dictation stream live text into a plain-string draft.

import { act, renderHook } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { useDictationInsert } from "./useDictationInsert";

/** Harness pairing the hook with the same useState shape the composers use. */
function renderDictation(initial = "") {
  return renderHook(() => {
    const [value, setValue] = useState(initial);
    const dictation = useDictationInsert(setValue);
    return { value, setRaw: (next: string) => setValue(() => next), ...dictation };
  });
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
});
