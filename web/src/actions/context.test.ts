import { describe, expect, it } from "vitest";
import {
  and,
  contextSpecificity,
  contextsMayOverlap,
  EMPTY_ACTION_CONTEXT,
  equals,
  evaluateContext,
  not,
  or,
  when,
} from "./context";
import type { ContextPatch, ContextSnapshot } from "./types";

function context(patch: ContextPatch): ContextSnapshot {
  return { ...EMPTY_ACTION_CONTEXT, ...patch };
}

describe("action context expressions", () => {
  it("evaluates boolean, equality, and composite predicates", () => {
    const expression = and(
      equals("isNativeShell", true),
      when("composerStreaming"),
      not(when("composerSuggestionsOpen")),
    );
    expect(
      evaluateContext(
        expression,
        context({
          isNativeShell: true,
          composerStreaming: true,
          composerSuggestionsOpen: false,
        }),
      ),
    ).toBe(true);
    expect(
      evaluateContext(expression, context({ isNativeShell: false, composerStreaming: true })),
    ).toBe(false);
    expect(or(when("isMac"), when("isElectron"))).toBeTruthy();
    expect(
      evaluateContext(or(when("isMac"), when("isElectron")), context({ isElectron: true })),
    ).toBe(true);
  });

  it("ranks conjunctions above their atoms and disjunctions by their narrowest branch", () => {
    expect(contextSpecificity(and(equals("isNativeShell", true), when("composerStreaming")))).toBe(
      2,
    );
    expect(contextSpecificity(or(when("isMac"), when("isElectron")))).toBe(1);
    expect(contextSpecificity(undefined)).toBe(0);
  });

  it("detects mutually exclusive and overlapping contexts", () => {
    expect(contextsMayOverlap(equals("isNativeShell", true), equals("isNativeShell", false))).toBe(
      false,
    );
    expect(contextsMayOverlap(when("isNativeShell"), not(when("isNativeShell")))).toBe(false);
    expect(
      contextsMayOverlap(
        or(when("isNativeShell"), when("isElectron")),
        and(when("isElectron"), when("composerStreaming")),
      ),
    ).toBe(true);
    expect(contextsMayOverlap(undefined, when("isNativeShell"))).toBe(true);
  });

  it("handles negated composite expressions", () => {
    const neitherNativeNorElectron = not(or(when("isNativeShell"), when("isElectron")));
    expect(contextsMayOverlap(neitherNativeNorElectron, when("isNativeShell"))).toBe(false);
    expect(contextsMayOverlap(neitherNativeNorElectron, not(when("isMac")))).toBe(true);
  });

  it("conservatively reports overlap when DNF analysis exceeds its bound", () => {
    const branches = [
      "isMac",
      "isNativeShell",
      "isElectron",
      "isEmbedded",
      "isCoarsePointer",
      "inputFocus",
      "terminalFocus",
      "monacoFocus",
      "eventMeta",
    ] as const;
    const exponential = and(...branches.map((key) => or(when(key), not(when(key)))));
    expect(contextsMayOverlap(exponential, when("composerStreaming"))).toBe(true);
  });
});
