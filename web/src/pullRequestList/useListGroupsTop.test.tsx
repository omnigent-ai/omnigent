import { act, render, screen } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  LIST_LAYOUT_MEASURES_PER_TASK,
  type ListLayoutShape,
  resetListLayoutLoopDiagnosticsForTests,
} from "./listLayoutLoop";
import { useListGroupsTop } from "./useListGroupsTop";

const SHAPE: ListLayoutShape = {
  where: "page",
  grouping: "status",
  rows: 2,
  groups: 1,
  items: 3,
  selected: 2,
  collapsed: 0,
  windowing: true,
  sections: ["open"],
};

afterEach(() => {
  resetListLayoutLoopDiagnosticsForTests();
  vi.restoreAllMocks();
});

/**
 * A list whose layout is a new value on every render, as an unmemoized input makes it, and
 * whose groups move each time they're measured: every measurement renders it again.
 */
function ChurningList({ tops }: { tops: () => number }) {
  const groupsRef = useRef<HTMLDivElement>(null);
  const [, setTick] = useState(0);
  const layout = { items: [] };
  const groupsTop = useListGroupsTop(groupsRef, {
    nothingMatches: false,
    layout,
    describe: () => ({ inputs: { layout }, shape: () => SHAPE }),
  });
  return (
    <div>
      <output aria-label="Groups top">{groupsTop}</output>
      <button type="button" onClick={() => setTick((tick) => tick + 1)}>
        Render
      </button>
      <div
        ref={(element) => {
          groupsRef.current = element;
          if (element)
            Object.defineProperty(element, "offsetTop", { get: tops, configurable: true });
        }}
      />
    </div>
  );
}

const shownTop = () => Number(screen.getByRole("status", { name: "Groups top" }).textContent);

describe("useListGroupsTop", () => {
  it("stops a layout that moves its groups on every render, and says which input changed", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    let reads = 0;
    // Without the guard React throws "Maximum update depth exceeded" here.
    render(<ChurningList tops={() => (reads++ % 2 ? 0 : 32)} />);

    expect(reads).toBe(LIST_LAYOUT_MEASURES_PER_TASK + 1);
    expect(warn).toHaveBeenCalledOnce();
    expect(warn.mock.calls[0]![0]).toMatch(
      /^\[omnigent\] pr list layout loop where=page measures=21 changed=layout:20 /,
    );
    // Once the task ends, a later render measures again.
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    });
    const before = reads;
    act(() => screen.getByRole("button", { name: "Render" }).click());
    expect(reads).toBeGreaterThan(before);
  });

  it("sets nothing for a top that hasn't moved, however often the layout changes", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    render(<ChurningList tops={() => 32} />);
    expect(shownTop()).toBe(32);
    for (let index = 0; index < LIST_LAYOUT_MEASURES_PER_TASK * 2; index += 1) {
      act(() => screen.getByRole("button", { name: "Render" }).click());
    }
    expect(shownTop()).toBe(32);
    expect(warn).not.toHaveBeenCalled();
  });
});
