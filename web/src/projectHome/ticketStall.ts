import { useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";
import type { ProjectTicket } from "@/lib/projectTicketsApi";

/** `?stalled=1` narrows the board's lanes to their stalled cards. */
export const STALLED_PARAM = "stalled";
export const STALLED_VALUE = "1";

/** Whether the card's wait has run past its kind's limit at *nowMs*. */
export function waitTooLong(ticket: Pick<ProjectTicket, "wait">, nowMs: number): boolean {
  const wait = ticket.wait;
  if (!wait?.after_s || wait.since === undefined) return false;
  return nowMs / 1000 - wait.since >= wait.after_s;
}

/**
 * Whether the card counts as stuck at *nowMs*: idle past its stage's threshold, or
 * waiting past its wait's limit. The stalled chip and filter count both.
 */
export function ticketStalled(
  ticket: Pick<ProjectTicket, "stall" | "wait">,
  nowMs: number,
): boolean {
  const stall = ticket.stall;
  if (stall) return nowMs / 1000 - stall.idle_since >= stall.after_s;
  return waitTooLong(ticket, nowMs);
}

/** The router as the button names it: "the Lead", or another persona by name. */
export function routerLabel(name: string): string {
  return name === "Lead" ? "the Lead" : name;
}

/**
 * The comment Wake the Lead posts: it @mentions the router, which delivers it to them.
 * *waitingOn* names what a long wait waits on, e.g. "Waiting to land"; else it's a stall.
 */
export function wakeRouterComment(routerName: string, idleFor: string, waitingOn?: string): string {
  const why = waitingOn
    ? `has been ${waitingOn.charAt(0).toLowerCase()}${waitingOn.slice(1)} for ${idleFor}`
    : `has had no one on it for ${idleFor}`;
  return `@${routerName} this ticket ${why}. Please find out why and get it moving.`;
}

const NO_IDS: string[] = [];

/**
 * The stalled cards among the board lanes already loaded, read from their queries'
 * cache, so the chip adds no requests of its own. *mine* matches the lanes' filter.
 */
export function useStalledTicketIds(projectId: string | null, mine: boolean, nowMs: number) {
  const client = useQueryClient();
  const cache = client.getQueryCache();
  const snapshot = useSyncExternalStore(
    (onChange) => cache.subscribe(onChange),
    () => {
      if (!projectId) return "";
      const ids = new Set<string>();
      for (const query of cache.findAll({ queryKey: ["project-tickets", projectId, "board"] })) {
        if (query.queryKey.includes("mine") !== mine) continue;
        const data = query.state.data as InfiniteData<{ data: ProjectTicket[] }> | undefined;
        for (const page of data?.pages ?? [])
          for (const ticket of page.data) if (ticketStalled(ticket, nowMs)) ids.add(ticket.id);
      }
      return [...ids].sort().join(",");
    },
  );
  return snapshot ? snapshot.split(",") : NO_IDS;
}
