import { useQueries } from "@tanstack/react-query";
import { MarkerType, type Edge } from "@xyflow/react";
import { SESSION_MESSAGES_KEY } from "@/hooks/useSessionCommunication";
import { listSessionMessages, type SessionMessage } from "@/lib/sessionCommunicationApi";

export function sessionMessageId(link: Pick<SessionMessage, "source_id" | "target_id">): string {
  return `${link.source_id}:${link.target_id}`;
}
export function sessionMessageEdges(links: SessionMessage[], visibleIds: Set<string>): Edge[] {
  return [...new Map(links.map((link) => [sessionMessageId(link), link])).values()]
    .filter((link) => visibleIds.has(link.source_id) && visibleIds.has(link.target_id))
    .map((link) => ({
      id: sessionMessageId(link),
      source: link.source_id,
      target: link.target_id,
      sourceHandle: "outgoing",
      targetHandle: "incoming",
      type: "sessionMessage",
      label: "Sent",
      ariaLabel: `${link.source_title || link.source_id} to ${link.target_title || link.target_id}: communication activity`,
      markerEnd: { type: MarkerType.ArrowClosed, color: "var(--brand-accent)" },
      style: { stroke: "var(--brand-accent)", strokeWidth: 2 },
      labelStyle: { fill: "var(--foreground)", fontSize: 12 },
      labelBgStyle: { fill: "var(--card)" },
      interactionWidth: 24,
      focusable: true,
      selectable: true,
      deletable: false,
    }));
}
export function useCanvasSessionMessages(sessionIds: string[], enabled: boolean) {
  const queries = useQueries({
    queries: sessionIds.map((id) => ({
      queryKey: [...SESSION_MESSAGES_KEY, "canvas", id],
      queryFn: ({ signal }: { signal: AbortSignal }) => listSessionMessages(id, signal),
      enabled,
      staleTime: 15_000,
      refetchInterval: enabled ? 15_000 : false,
    })),
  });
  return {
    links: enabled ? queries.flatMap((query) => query.data?.data ?? []) : [],
    error: queries.find((query) => query.error)?.error,
    loading: enabled && queries.some((query) => query.isPending),
  };
}
