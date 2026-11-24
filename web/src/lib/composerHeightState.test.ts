import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  composerHeightOf,
  DEFAULT_COMPOSER_HEIGHT,
  pasteExpandsComposer,
  PASTE_EXPANDS_COMPOSER,
  resetComposerHeightsForTests,
  setComposerHeight,
  toggleComposerHeight,
  useComposerHeight,
  useComposerHeightScope,
} from "./composerHeightState";

afterEach(() => {
  resetComposerHeightsForTests();
  localStorage.clear();
});

describe("composerHeightState", () => {
  it("starts every composer compact, and keeps nothing on the device", () => {
    expect(DEFAULT_COMPOSER_HEIGHT).toBe("compact");
    expect(composerHeightOf("session:a")).toBe("compact");
    setComposerHeight("session:a", "expanded");
    expect(localStorage.length).toBe(0);
  });

  it("keeps each scope's height apart, so one session's choice never reaches another", () => {
    toggleComposerHeight("session:a");
    expect(composerHeightOf("session:a")).toBe("expanded");
    expect(composerHeightOf("session:b")).toBe("compact");
    toggleComposerHeight("session:a");
    expect(composerHeightOf("session:a")).toBe("compact");
  });

  it("updates only the readers of the scope that changed", () => {
    const first = renderHook(() => useComposerHeight("session:a"));
    const other = renderHook(() => useComposerHeight("session:b"));
    act(() => setComposerHeight("session:a", "expanded"));
    expect(first.result.current).toBe("expanded");
    expect(other.result.current).toBe("compact");
  });

  it("scopes a composer by its session, or by itself while it has none", () => {
    const named = renderHook(({ key }) => useComposerHeightScope(key), {
      initialProps: { key: "a" as string | null },
    });
    expect(named.result.current.scope).toBe("session:a");
    named.rerender({ key: null });
    const ownScope = named.result.current.scope;
    expect(ownScope).not.toBe("session:a");
    // Two composers without a session never share a height.
    const another = renderHook(() => useComposerHeightScope(null));
    expect(another.result.current.scope).not.toBe(ownScope);

    named.rerender({ key: "a" });
    act(() => setComposerHeight("session:a", "expanded"));
    act(() => named.result.current.collapse());
    expect(composerHeightOf("session:a")).toBe("compact");
  });
});

describe("pasteExpandsComposer", () => {
  const lines = (count: number) => Array.from({ length: count }, (_, index) => `line ${index}`);

  it("expands from the line threshold, and not one line short of it", () => {
    expect(pasteExpandsComposer(lines(PASTE_EXPANDS_COMPOSER.lines).join("\n"))).toBe(true);
    expect(pasteExpandsComposer(lines(PASTE_EXPANDS_COMPOSER.lines - 1).join("\n"))).toBe(false);
  });

  it("expands from the character threshold on one line", () => {
    expect(pasteExpandsComposer("x".repeat(PASTE_EXPANDS_COMPOSER.characters))).toBe(true);
    expect(pasteExpandsComposer("x".repeat(PASTE_EXPANDS_COMPOSER.characters - 1))).toBe(false);
    expect(pasteExpandsComposer("")).toBe(false);
  });
});
