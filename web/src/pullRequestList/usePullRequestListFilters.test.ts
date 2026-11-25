import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PullRequestListEntry } from "@/shell/PullRequestListRows";

import { usePullRequestListFilters } from "./usePullRequestListFilters";

const NO_ENTRIES: readonly PullRequestListEntry[] = [];

describe("usePullRequestListFilters", () => {
  it("keeps one value between renders while its entries and filters stand", () => {
    const update = vi.fn();
    const view = renderHook(() =>
      usePullRequestListFilters({ entries: NO_ENTRIES, filters: undefined, update }),
    );
    const first = view.result.current;
    view.rerender();
    // A list's rows derive from it; a new value every render works them out again each time.
    expect(view.result.current).toBe(first);
  });
});
