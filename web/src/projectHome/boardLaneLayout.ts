import { useEffect, useLayoutEffect, useState, type RefObject } from "react";
import type { TicketStage } from "@/lib/projectTicketsApi";

/** A lane's narrowest open width (min-w-52), a strip (w-12) and the gap between lanes (gap-4), in px. */
export const LANE_MIN_PX = 208;
export const LANE_STRIP_PX = 48;
export const LANE_GAP_PX = 16;
/** Below this the board doesn't fit lanes to its width; two open lanes need at least this. */
export const MIN_FIT_WIDTH_PX = 2 * LANE_MIN_PX + LANE_GAP_PX;

/** A person's choice per lane: true collapsed, false open; a lane they never toggled is absent. */
export type LanePreferences = Readonly<Record<string, boolean>>;

/**
 * Which lanes show as narrow strips. A person's own choice wins. Otherwise the
 * completed stage starts collapsed, and when the open lanes don't fit the
 * board's width, lanes collapse from the right (where finished work sits) until
 * they do. The first stage stays open, so there's always somewhere to add work.
 */
export function collapsedLanes(
  stages: readonly TicketStage[],
  preferences: LanePreferences,
  initialStageId: string | undefined,
  width: number | null,
): Set<string> {
  const collapsed = new Set(
    stages.filter((stage) => preferences[stage.id] ?? stage.is_complete).map((stage) => stage.id),
  );
  // Too narrow for two open lanes (a phone): lanes stay full width and the board scrolls sideways.
  if (width === null || width < MIN_FIT_WIDTH_PX) return collapsed;
  const fits = () => {
    const strips = collapsed.size;
    const open = stages.length - strips;
    return open * LANE_MIN_PX + strips * LANE_STRIP_PX + (stages.length - 1) * LANE_GAP_PX <= width;
  };
  for (const stage of [...stages].reverse()) {
    if (fits()) break;
    if (collapsed.has(stage.id) || preferences[stage.id] === false) continue;
    if (stage.id === initialStageId) continue;
    collapsed.add(stage.id);
  }
  return collapsed;
}

function preferenceKeyPrefix(projectId: string) {
  return `omnigent.ticketBoard.collapsed.${projectId}.`;
}

function readPreferences(projectId: string | null): Record<string, boolean> {
  if (!projectId) return {};
  const prefix = preferenceKeyPrefix(projectId);
  const preferences: Record<string, boolean> = {};
  try {
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (!key?.startsWith(prefix)) continue;
      const value = window.localStorage.getItem(key);
      if (value === "1" || value === "0") preferences[key.slice(prefix.length)] = value === "1";
    }
  } catch {
    // Storage may be unavailable; every lane falls back to the automatic layout.
  }
  return preferences;
}

/** The lanes a person opened or collapsed by hand, remembered per project. */
export function useLanePreferences(projectId: string | null) {
  const [preferences, setPreferences] = useState(() => readPreferences(projectId));
  useEffect(() => setPreferences(readPreferences(projectId)), [projectId]);
  const setCollapsed = (stageId: string, collapsed: boolean) => {
    setPreferences((current) => ({ ...current, [stageId]: collapsed }));
    if (!projectId) return;
    try {
      window.localStorage.setItem(preferenceKeyPrefix(projectId) + stageId, collapsed ? "1" : "0");
    } catch {
      // Storage may be unavailable; the choice still applies for this view.
    }
  };
  return [preferences as LanePreferences, setCollapsed] as const;
}

/** An element's content width (inside its horizontal padding), tracked as it resizes. */
export function useContentWidth(ref: RefObject<HTMLElement | null>): number | null {
  const [width, setWidth] = useState<number | null>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const style = getComputedStyle(element);
      const content =
        element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      // Not laid out yet (hidden, or a test DOM): keep the stored layout until there's a width.
      setWidth(content > 0 ? content : null);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}
