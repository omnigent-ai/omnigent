import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { markTicketSeen, type ProjectTicket, type TicketEvent } from "@/lib/projectTicketsApi";

/**
 * Tracks what the viewer had not seen when they opened a ticket: the first new
 * activity position (frozen for the panel's lifetime so the "New" divider stays put),
 * a one-time scroll to it, and advancing the server-side seen marker as activity loads.
 */
export function useTicketSeen({
  projectId,
  ticket,
  events,
  hasNextPage,
  isFetching,
  fetchNextPage,
  scrollToNew = true,
}: {
  projectId: string;
  ticket: ProjectTicket;
  events: TicketEvent[];
  hasNextPage: boolean;
  isFetching: boolean;
  fetchNextPage: () => unknown;
  /** False when another target, like an Inbox question, owns the initial scroll. */
  scrollToNew?: boolean;
}) {
  const client = useQueryClient();
  const [firstNewPosition] = useState(() => ticket.first_new_position ?? null);
  const dividerRef = useRef<HTMLLIElement | null>(null);
  const scrolled = useRef(false);
  const sentPosition = useRef(ticket.viewer_seen_position ?? -1);
  // Agents' ticket reads omit seen state; only a viewer-aware server records it.
  const tracked = ticket.viewer_seen_position !== undefined;

  // Load the rest of the history so the first new item and everything after it is visible.
  useEffect(() => {
    if (firstNewPosition !== null && hasNextPage && !isFetching) void fetchNextPage();
  }, [firstNewPosition, hasNextPage, isFetching, fetchNextPage]);

  const dividerPosition =
    firstNewPosition === null
      ? null
      : (events.find((event) => event.position >= firstNewPosition)?.position ?? null);

  useEffect(() => {
    if (!scrollToNew || scrolled.current || dividerPosition === null || !dividerRef.current) {
      return;
    }
    scrolled.current = true;
    dividerRef.current.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [dividerPosition, scrollToNew]);

  const latestPosition = events.reduce((latest, event) => Math.max(latest, event.position), -1);
  useEffect(() => {
    if (!tracked || latestPosition <= sentPosition.current) return;
    sentPosition.current = latestPosition;
    markTicketSeen(projectId, ticket.id, latestPosition)
      .then(() => {
        void client.invalidateQueries({ queryKey: ["project-tickets", projectId, "board"] });
        void client.invalidateQueries({
          queryKey: ["project-tickets", projectId, ticket.id],
          exact: true,
        });
      })
      .catch(() => {
        // Best effort: the next load retries from the server's marker.
        sentPosition.current = Math.min(sentPosition.current, ticket.viewer_seen_position ?? -1);
      });
  }, [tracked, latestPosition, projectId, ticket.id, ticket.viewer_seen_position, client]);

  return { dividerPosition, dividerRef };
}
