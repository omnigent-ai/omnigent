import { act, cleanup, renderHook } from "@testing-library/react";
import { StrictMode, type ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { useCanvasSidebar } from "./useCanvasSidebar";

afterEach(cleanup);

describe("useCanvasSidebar", () => {
  it("collapses on entry and restores the previous preference on exit", () => {
    const { result, rerender } = renderHook(({ canvas }) => useCanvasSidebar(canvas, () => true), {
      initialProps: { canvas: false },
    });
    expect(result.current[0]).toBe(true);
    rerender({ canvas: true });
    expect(result.current[0]).toBe(false);
    rerender({ canvas: false });
    expect(result.current[0]).toBe(true);
  });

  it("keeps manual expansion across card selections and after leaving Canvas", () => {
    const { result, rerender } = renderHook(({ canvas }) => useCanvasSidebar(canvas, () => false), {
      initialProps: { canvas: true },
    });
    act(() => result.current[1]((open) => !open));
    rerender({ canvas: true });
    expect(result.current[0]).toBe(true);
    rerender({ canvas: false });
    expect(result.current[0]).toBe(true);
    rerender({ canvas: true });
    expect(result.current[0]).toBe(false);
  });

  it("preserves a manual collapse without changing ordinary sidebar behavior", () => {
    const { result, rerender } = renderHook(({ canvas }) => useCanvasSidebar(canvas, () => true), {
      initialProps: { canvas: true },
    });
    act(() => result.current[1](true));
    act(() => result.current[1](false));
    rerender({ canvas: false });
    expect(result.current[0]).toBe(false);
    act(() => result.current[1](true));
    expect(result.current[0]).toBe(true);
  });

  it("restores the original preference after a StrictMode deep link", () => {
    const { result, rerender } = renderHook(({ canvas }) => useCanvasSidebar(canvas, () => true), {
      initialProps: { canvas: true },
      wrapper: ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>,
    });
    expect(result.current[0]).toBe(false);
    rerender({ canvas: false });
    expect(result.current[0]).toBe(true);
  });
});
