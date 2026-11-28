// Of several boxes in one scope, only the one focused last is active, until it unmounts.

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { createLastFocusedScope } from "./lastFocusedScope";

describe("a last-focused scope", () => {
  it("makes only the box focused last active, and none once it unmounts", () => {
    const scope = createLastFocusedScope();
    const first = renderHook(() => scope.useLastFocused());
    const second = renderHook(() => scope.useLastFocused());
    expect(first.result.current.active).toBe(false);
    act(() => first.result.current.markFocused());
    expect(first.result.current.active).toBe(true);
    act(() => second.result.current.markFocused());
    expect(first.result.current.active).toBe(false);
    expect(second.result.current.active).toBe(true);
    second.unmount();
    expect(first.result.current.active).toBe(false);
  });

  it("says when no box holds the claim, and a box that folds gives it up", () => {
    const scope = createLastFocusedScope();
    const box = renderHook(() => scope.useLastFocused());
    const other = renderHook(() => scope.useLastFocused());
    expect(box.result.current.none).toBe(true);
    act(() => box.result.current.markFocused());
    expect(other.result.current.none).toBe(false);
    // Only the box holding the claim can give it up.
    act(() => other.result.current.release());
    expect(box.result.current.active).toBe(true);
    act(() => box.result.current.release());
    expect(box.result.current.active).toBe(false);
    expect(other.result.current.none).toBe(true);
  });

  it("keeps scopes apart", () => {
    const editors = createLastFocusedScope();
    const forms = createLastFocusedScope();
    const editor = renderHook(() => editors.useLastFocused());
    const form = renderHook(() => forms.useLastFocused());
    act(() => editor.result.current.markFocused());
    act(() => form.result.current.markFocused());
    expect(editor.result.current.active).toBe(true);
    expect(form.result.current.active).toBe(true);
  });
});
