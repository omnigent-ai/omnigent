import { useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";
import type { ProjectTicket } from "@/lib/projectTicketsApi";

/** `?stalled=1` narrows the board's lanes to their stalled cards. */
export const STALLED_PARAM = "stalled";
export const STALLED_VALUE = "1";

/** Whether the card reads as stalled at *nowMs*: idle past its stage's threshold. */
export function ticketStalled(ticket: Pick<ProjectTicket, "stall">, nowMs: number): boolean {
  const stall = ticket.stall;
  return Boolean(stall) && nowMs / 1000 - stall!.idle_since >= stall!.after_s;
}

/** The comment Wake the Lead posts: it @mentions the Lead, which delivers it to the Lead. */
export function wakeLeadComment(idleFor: string): string {
  return `@Lead this ticket has had no one on it for ${idleFor}. Please find out why and get it moving.`;
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
