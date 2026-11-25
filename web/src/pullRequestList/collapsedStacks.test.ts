import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  COLLAPSED_STACK_LIMIT,
  resetCollapsedStacks,
  useCollapsedStacks,
  withCollapsed,
} from "./collapsedStacks";

const url = (number: number) => `https://github.com/acme/app/pull/${number}`;

beforeEach(() => {
  localStorage.clear();
  resetCollapsedStacks();
});
afterEach(() => localStorage.clear());

describe("useCollapsedStacks", () => {
  it("keeps a fold on this device, for the account that made it only", () => {
    const { result } = renderHook(() => useCollapsedStacks("user-1\ngithub.com\nocto"));
    act(() => result.current.setCollapsed(url(100), true));
    expect(result.current.isCollapsed(url(100))).toBe(true);
    expect(result.current.isCollapsed(url(100).toUpperCase())).toBe(true);

    // A new run reads it back; another account on the same device doesn't see it.
    resetCollapsedStacks();
    const again = renderHook(() => useCollapsedStacks("user-1\ngithub.com\nocto"));
    expect(again.result.current.isCollapsed(url(100))).toBe(true);
    const other = renderHook(() => useCollapsedStacks("user-1\ngithub.com\nhubot"));
    expect(other.result.current.isCollapsed(url(100))).toBe(false);

    act(() => again.result.current.setCollapsed(url(100), false));
    expect(again.result.current.isCollapsed(url(100))).toBe(false);
    expect(localStorage.getItem("omnigent:pull-requests-collapsed-stacks")).toBeNull();
  });

  it("folds nothing while the account isn't known", () => {
    const { result } = renderHook(() => useCollapsedStacks(null));
    act(() => result.current.setCollapsed(url(100), true));
    expect(result.current.isCollapsed(url(100))).toBe(false);
    expect(localStorage.length).toBe(0);
  });

  it("ignores a damaged saved list", () => {
    localStorage.setItem("omnigent:pull-requests-collapsed-stacks", "{not json");
    const { result } = renderHook(() => useCollapsedStacks("scope"));
    expect(result.current.isCollapsed(url(100))).toBe(false);
  });
});

describe("withCollapsed", () => {
  it("forgets the oldest fold past the limit", () => {
    let stacks = withCollapsed([], { scope: "scope", url: url(1) });
    for (let number = 2; number <= COLLAPSED_STACK_LIMIT + 1; number += 1) {
      stacks = withCollapsed(stacks, { scope: "scope", url: url(number) });
    }
    expect(stacks).toHaveLength(COLLAPSED_STACK_LIMIT);
    expect(stacks[0]!.url).toBe(url(COLLAPSED_STACK_LIMIT + 1));
    expect(stacks.some((stack) => stack.url === url(1))).toBe(false);
    // Folding one again makes it the newest, not a second entry.
    stacks = withCollapsed(stacks, { scope: "scope", url: url(5) });
    expect(stacks[0]!.url).toBe(url(5));
    expect(stacks.filter((stack) => stack.url === url(5))).toHaveLength(1);
  });
});
