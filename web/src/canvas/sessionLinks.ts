import { useQueries } from "@tanstack/react-query";
import { MarkerType, type Edge } from "@xyflow/react";
import { SESSION_LINKS_KEY } from "@/hooks/useSessionLinks";
import { listSessionLinks, type SessionLink } from "@/lib/sessionLinksApi";

export function sessionLinkId(link: Pick<SessionLink, "source_id" | "target_id">): string {
  return `${link.source_id}:${link.target_id}`;
}

export function sessionLinkEdges(links: SessionLink[], visibleIds: Set<string>): Edge[] {
  return links
    .filter((link) => visibleIds.has(link.source_id) && visibleIds.has(link.target_id))
    .map((link) => ({
      id: sessionLinkId(link),
      source: link.source_id,
      target: link.target_id,
      sourceHandle: "outgoing",
      targetHandle: "incoming",
      type: "sessionLink",
      label: link.active ? "Linked" : "Inactive link",
      ariaLabel: `${link.source_title || link.source_id} to ${link.target_title || link.target_id}: ${link.active ? "linked" : "inactive link"}`,
      markerEnd: { type: MarkerType.ArrowClosed, color: "var(--brand-accent)" },
      style: {
        stroke: "var(--brand-accent)",
        strokeWidth: 2,
        ...(link.active ? {} : { strokeDasharray: "5 4", opacity: 0.6 }),
      },
      labelStyle: { fill: "var(--foreground)", fontSize: 12 },
      labelBgStyle: { fill: "var(--card)" },
      interactionWidth: 24,
      focusable: true,
      selectable: true,
      deletable: false,
    }));
}

export function useCanvasSessionLinks(sessionIds: string[], enabled: boolean) {
  const queries = useQueries({
    queries: sessionIds.map((id) => ({
      queryKey: [...SESSION_LINKS_KEY, "canvas", id],
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const links: SessionLink[] = [];
        let after: string | undefined;
        do {
          signal.throwIfAborted();
          // eslint-disable-next-line no-await-in-loop -- The next cursor comes from this page.
          const page = await listSessionLinks(id, false, after, signal);
          links.push(...page.data);
          after = page.next_cursor ?? undefined;
        } while (after);
        return links;
      },
      enabled,
      staleTime: 15_000,
      refetchInterval: enabled ? 15_000 : false,
    })),
  });
  return {
    links: enabled ? queries.flatMap((query) => query.data ?? []) : [],
    error: queries.find((query) => query.error)?.error,
    loading: enabled && queries.some((query) => query.isPending),
  };
}
