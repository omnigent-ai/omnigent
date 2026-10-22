import { USER_SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitles";

export type SplitEdge = "left" | "right" | "top" | "bottom";
type Axis = "columns" | "rows";
export type GridLayout = string | { axis: Axis; children: [GridLayout, GridLayout] };
export interface SessionGrid {
  id: string;
  name?: string;
  layout: GridLayout;
}

export function gridPanes(
  layout: GridLayout,
  column = 1,
  row = 1,
  columns = 2,
  rows = 2,
): { sessionId: string; area: string }[] {
  if (typeof layout === "string")
    return [{ sessionId: layout, area: `${row} / ${column} / span ${rows} / span ${columns}` }];
  return layout.children.flatMap((child, index) =>
    layout.axis === "columns"
      ? gridPanes(child, column + (index * columns) / 2, row, columns / 2, rows)
      : gridPanes(child, column, row + (index * rows) / 2, columns, rows / 2),
  );
}

export function gridSessionIds(layout: GridLayout): string[] {
  return typeof layout === "string" ? [layout] : layout.children.flatMap(gridSessionIds);
}

/** Each axis can be split once along a path: at most two rows and two columns. */
export function splitGrid(
  layout: GridLayout,
  targetId: string,
  sessionId: string,
  edge: SplitEdge,
  usedAxes: Axis[] = [],
): GridLayout | null {
  const ids = gridSessionIds(layout);
  if (!ids.includes(targetId) || ids.includes(sessionId) || ids.length >= 4) return null;
  const axis = edge === "left" || edge === "right" ? "columns" : "rows";
  if (typeof layout === "string") {
    if (usedAxes.includes(axis)) return null;
    return {
      axis,
      children: edge === "left" || edge === "top" ? [sessionId, layout] : [layout, sessionId],
    };
  }
  const index = gridSessionIds(layout.children[0]).includes(targetId) ? 0 : 1;
  const next = splitGrid(layout.children[index], targetId, sessionId, edge, [
    ...usedAxes,
    layout.axis,
  ]);
  if (next === null) return null;
  return {
    ...layout,
    children: index === 0 ? [next, layout.children[1]] : [layout.children[0], next],
  };
}

export function removeGridSession(layout: GridLayout, sessionId: string): GridLayout | null {
  if (typeof layout === "string") return layout === sessionId ? null : layout;
  const first = removeGridSession(layout.children[0], sessionId);
  const second = removeGridSession(layout.children[1], sessionId);
  if (first === null) return second;
  if (second === null) return first;
  return { ...layout, children: [first, second] };
}

export function swapGridSessions(
  layout: GridLayout,
  firstId: string,
  secondId: string,
): GridLayout {
  const ids = gridSessionIds(layout);
  if (firstId === secondId || !ids.includes(firstId) || !ids.includes(secondId)) return layout;
  const swap = (node: GridLayout): GridLayout => {
    if (typeof node === "string")
      return node === firstId ? secondId : node === secondId ? firstId : node;
    return { ...node, children: [swap(node.children[0]), swap(node.children[1])] };
  };
  return swap(layout);
}

export function parseSessionGrids(raw: string | null): SessionGrid[] {
  const validLayout = (value: unknown, axes: Axis[] = []): value is GridLayout => {
    if (typeof value === "string") return value.length > 0 && !value.startsWith("temp:");
    if (!value || typeof value !== "object" || axes.length >= 2) return false;
    const { axis, children } = value as Record<string, unknown>;
    if (axis !== "columns" && axis !== "rows") return false;
    return (
      !axes.includes(axis) &&
      Array.isArray(children) &&
      children.length === 2 &&
      children.every((child) => validLayout(child, [...axes, axis]))
    );
  };
  try {
    const values: unknown = JSON.parse(raw ?? "[]");
    if (!Array.isArray(values)) return [];
    const seen = new Set<string>();
    return values.filter((value): value is SessionGrid => {
      if (
        !value ||
        typeof value.id !== "string" ||
        !value.id ||
        (value.name !== undefined &&
          (typeof value.name !== "string" || value.name.length > USER_SESSION_TITLE_MAX_CHARS)) ||
        seen.has(value.id) ||
        !validLayout(value.layout)
      )
        return false;
      const ids = gridSessionIds(value.layout);
      if (ids.length < 2 || new Set(ids).size !== ids.length) return false;
      seen.add(value.id);
      return true;
    });
  } catch {
    return [];
  }
}
