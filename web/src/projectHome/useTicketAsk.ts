import { useEffect, useRef, type RefObject } from "react";
import type { TicketEvent } from "@/lib/projectTicketsApi";

/**
 * Scroll a ticket's activity to the question it was opened for (the Inbox's
 * `ask` link), loading older pages until it's there, and report its position so
 * the item can be highlighted. Returns `null` without a target or until it loads.
 */
export function useTicketAsk({
  askPosition,
  events,
  hasNextPage,
  isFetching,
  fetchNextPage,
  containerRef,
}: {
  askPosition: number | null;
  events: TicketEvent[];
  hasNextPage: boolean;
  isFetching: boolean;
  fetchNextPage: () => unknown;
  containerRef: RefObject<HTMLElement | null>;
}): number | null {
  const scrolled = useRef(false);
  const target =
    askPosition === null
      ? null
      : (events.find((event) => event.position >= askPosition)?.position ?? null);

  useEffect(() => {
    if (askPosition !== null && target === null && hasNextPage && !isFetching) {
      void fetchNextPage();
    }
  }, [askPosition, target, hasNextPage, isFetching, fetchNextPage]);

  useEffect(() => {
    if (scrolled.current || target === null) return;
    const item = containerRef.current?.querySelector(`[data-ticket-event-position="${target}"]`);
    if (!item) return;
    scrolled.current = true;
    item.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [target, containerRef]);

  return target;
}

/** Parse the `ask` query value: a non-negative activity position, else `null`. */
export function parseAskPosition(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value)) return null;
  return Number(value);
}
