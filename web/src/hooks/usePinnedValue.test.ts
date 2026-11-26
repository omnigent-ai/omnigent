import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { usePinnedValue } from "./usePinnedValue";

describe("usePinnedValue", () => {
  it("waits for a value, then keeps the first one as the input changes", () => {
    const { result, rerender } = renderHook(
      ({ value, key }: { value: string | null; key: string }) => usePinnedValue(value, key),
      { initialProps: { value: null as string | null, key: "project-a" } },
    );
    expect(result.current).toBeNull();
    rerender({ value: "open", key: "project-a" });
    expect(result.current).toBe("open");
    // The lane emptied, so the first lane with tickets is now another one.
    rerender({ value: "review", key: "project-a" });
    expect(result.current).toBe("open");
    rerender({ value: null, key: "project-a" });
    expect(result.current).toBe("open");
  });

  it("starts over when the reset key changes", () => {
    const { result, rerender } = renderHook(
      ({ value, key }: { value: string | null; key: string }) => usePinnedValue(value, key),
      { initialProps: { value: "open" as string | null, key: "project-a" } },
    );
    rerender({ value: "working", key: "project-b" });
    expect(result.current).toBe("working");
  });
});
