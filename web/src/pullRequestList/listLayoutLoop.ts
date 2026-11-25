// The list measures where its groups start after each layout change and feeds that to the
// virtualizer. If a layout input came back new on every render while the measurement kept moving,
// each measurement would render the list again until React stopped it with #185. The guard caps
// the measurements one task may make, and logs once per page which inputs kept changing: input
// names, counts and section names only, never a title, URL or repository.

/** The layout's inputs by name, compared by identity between measurements. */
export type ListLayoutInputs = Readonly<Record<string, unknown>>;

/** What the list looked like when the loop tripped. */
export interface ListLayoutShape {
  /** "session" for a session's own list, "page" for the Pull Requests page's. */
  where: "session" | "page";
  grouping: string;
  rows: number;
  groups: number;
  items: number;
  selected: number;
  collapsed: number;
  windowing: boolean;
  /** Group ids where they name a lifecycle section ("open", "merged"…), else none. */
  sections: readonly string[];
}

/** Measurements one task may make; React gives up on nested updates at 50. */
export const LIST_LAYOUT_MEASURES_PER_TASK = 20;

/** The input names whose values differ between `previous` and `next`. */
export function changedListLayoutInputs(
  previous: ListLayoutInputs,
  next: ListLayoutInputs,
): string[] {
  const names = new Set([...Object.keys(previous), ...Object.keys(next)]);
  return [...names].filter((name) => !Object.is(previous[name], next[name])).sort();
}

/** The one line the console capture keeps for a tripped loop. */
export function formatListLayoutLoop(report: {
  measures: number;
  /** How many measurements in the task each input changed for. */
  changed: ReadonlyMap<string, number>;
  shape: ListLayoutShape;
  /** The last measurements, oldest first. */
  tops: readonly number[];
}): string {
  const { shape } = report;
  const changed =
    [...report.changed]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([name, count]) => `${name}:${count}`)
      .join(",") || "none";
  return (
    `[omnigent] pr list layout loop where=${shape.where} measures=${report.measures} ` +
    `changed=${changed} tops=${report.tops.join(">")} grouping=${shape.grouping} ` +
    `rows=${shape.rows} groups=${shape.groups} items=${shape.items} ` +
    `selected=${shape.selected} collapsed=${shape.collapsed} windowing=${shape.windowing} ` +
    `sections=${shape.sections.join(",") || "-"}`
  );
}

let reported = false;

export interface ListLayoutLoopGuard {
  /**
   * Whether a measurement of `top` may be set now. Past the task's cap it may not, until the
   * task ends; the first time on this page that happens, the loop is logged.
   */
  allows: (top: number, inputs: ListLayoutInputs, shape: () => ListLayoutShape) => boolean;
}

/** `afterTask` runs its callback once the current task has ended; a timer by default. */
export function createListLayoutLoopGuard(
  afterTask: (run: () => void) => void = (run) => {
    setTimeout(run, 0);
  },
): ListLayoutLoopGuard {
  let measures = 0;
  let previous: ListLayoutInputs | null = null;
  let changed = new Map<string, number>();
  let tops: number[] = [];
  let resetPending = false;
  return {
    allows(top, inputs, shape) {
      if (!resetPending) {
        resetPending = true;
        afterTask(() => {
          resetPending = false;
          measures = 0;
          changed = new Map();
          tops = [];
        });
      }
      measures += 1;
      if (previous) {
        for (const name of changedListLayoutInputs(previous, inputs)) {
          changed.set(name, (changed.get(name) ?? 0) + 1);
        }
      }
      previous = inputs;
      tops = [...tops.slice(-5), top];
      if (measures <= LIST_LAYOUT_MEASURES_PER_TASK) return true;
      if (!reported) {
        reported = true;
        try {
          console.warn(formatListLayoutLoop({ measures, changed, shape: shape(), tops }));
        } catch {
          // Diagnostics are best-effort; the list renders the same without them.
        }
      }
      return false;
    },
  };
}

export function resetListLayoutLoopDiagnosticsForTests(): void {
  reported = false;
}
