import { afterEach, describe, expect, it, vi } from "vitest";

import {
  changedListLayoutInputs,
  createListLayoutLoopGuard,
  formatListLayoutLoop,
  LIST_LAYOUT_MEASURES_PER_TASK,
  type ListLayoutShape,
  resetListLayoutLoopDiagnosticsForTests,
} from "./listLayoutLoop";

const SHAPE: ListLayoutShape = {
  where: "session",
  grouping: "status",
  rows: 30,
  groups: 3,
  items: 33,
  selected: 7,
  collapsed: 1,
  windowing: true,
  sections: ["open", "merged", "closed"],
};

afterEach(() => {
  resetListLayoutLoopDiagnosticsForTests();
  vi.restoreAllMocks();
});

/** A guard whose task ends only when the test says so. */
function guardWithTasks() {
  const pending: (() => void)[] = [];
  const guard = createListLayoutLoopGuard((run) => pending.push(run));
  return { guard, endTask: () => pending.splice(0).forEach((run) => run()) };
}

describe("changedListLayoutInputs", () => {
  it("names the inputs that are a new value, comparing by identity", () => {
    const rows: string[] = [];
    expect(
      changedListLayoutInputs(
        { rows, filters: {}, grouping: "status", removed: 1 },
        { rows, filters: {}, grouping: "status", added: 2 },
      ),
    ).toEqual(["added", "filters", "removed"]);
  });
});

describe("the list layout loop guard", () => {
  it("lets a task measure up to its cap, then holds until the task ends", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const { guard, endTask } = guardWithTasks();
    const shape = vi.fn(() => SHAPE);
    const allowed = Array.from({ length: LIST_LAYOUT_MEASURES_PER_TASK + 3 }, (_, index) =>
      guard.allows(index % 2 ? 32 : 0, { layout: {} }, shape),
    );
    expect(allowed.filter(Boolean)).toHaveLength(LIST_LAYOUT_MEASURES_PER_TASK);
    expect(allowed.at(-1)).toBe(false);
    endTask();
    expect(guard.allows(0, { layout: {} }, shape)).toBe(true);
  });

  it("records once per page which inputs kept changing, with counts and sections only", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { guard, endTask } = guardWithTasks();
    const groups = [{ id: "open", title: "Fix the parser", url: "https://example.com/pull/1" }];
    for (let index = 0; index <= LIST_LAYOUT_MEASURES_PER_TASK + 1; index += 1) {
      guard.allows(index % 2 ? 32 : 0, { layout: {}, groupFacts: {}, groups }, () => SHAPE);
    }
    endTask();
    for (let index = 0; index <= LIST_LAYOUT_MEASURES_PER_TASK + 1; index += 1) {
      guard.allows(0, { layout: {} }, () => SHAPE);
    }

    expect(warn).toHaveBeenCalledOnce();
    const [line] = warn.mock.calls[0]!;
    expect(line).toBe(
      "[omnigent] pr list layout loop where=session measures=21 " +
        "changed=groupFacts:20,layout:20 tops=32>0>32>0>32>0 grouping=status " +
        "rows=30 groups=3 items=33 selected=7 collapsed=1 windowing=true " +
        "sections=open,merged,closed",
    );
    expect(line).not.toMatch(/Fix the parser|example\.com/);
  });

  it("says when nothing it compares changed, and when no section has a lifecycle name", () => {
    expect(
      formatListLayoutLoop({
        measures: 21,
        changed: new Map(),
        shape: { ...SHAPE, grouping: "repository", sections: [] },
        tops: [0, 32],
      }),
    ).toMatch(/changed=none tops=0>32 grouping=repository .* sections=-$/);
  });

  it("keeps guarding when the console throws", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {
      throw new Error("console gone");
    });
    const { guard } = guardWithTasks();
    for (let index = 0; index < LIST_LAYOUT_MEASURES_PER_TASK; index += 1) {
      guard.allows(index, {}, () => SHAPE);
    }
    expect(guard.allows(99, {}, () => SHAPE)).toBe(false);
  });
});
