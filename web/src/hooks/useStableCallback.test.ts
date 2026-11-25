import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useStableCallback } from "./useStableCallback";

describe("useStableCallback", () => {
  it("keeps one identity across renders and runs the latest callback", () => {
    const { result, rerender } = renderHook(
      ({ suffix }: { suffix: string }) => useStableCallback((name: string) => `${name}${suffix}`),
      { initialProps: { suffix: "!" } },
    );
    const first = result.current;
    expect(first("a")).toBe("a!");

    rerender({ suffix: "?" });
    expect(result.current).toBe(first);
    expect(first("a")).toBe("a?");
  });
});
