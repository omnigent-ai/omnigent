import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BotIcon, ChevronRightIcon } from "lucide-react";
import { NeedsResponseBadge } from "@/components/NeedsResponseBadge";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { fetchConversationById } from "@/hooks/useOpenSessionRow";
import { useSessionBadgeStates } from "@/hooks/useSessionBadgeStates";
import { Link } from "@/lib/routing";
import { findCachedListRow } from "@/lib/sessionListCache";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { projectSessionPanelSearch, ticketSessionSearch } from "./projectHomeNavigation";

function useTicketSessionMetadata(sessionId: string, refresh: boolean) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["conversation-backfill", sessionId],
    queryFn: () => fetchConversationById(sessionId, client),
    initialData: () => findCachedListRow(client, sessionId),
    staleTime: 30_000,
    refetchInterval: refresh ? 15_000 : false,
    retry: false,
  });
  const session = query.data;
  const title = session ? conversationDisplayLabel(session) : `Session ${sessionId.slice(0, 8)}`;
  return { query, title };
}

export function TicketSessionLink({
  sessionId,
  showStatus = false,
  ticketId,
  involvement,
  responseRequested = false,
}: {
  sessionId: string;
  showStatus?: boolean;
  ticketId?: string;
  involvement?: string;
  responseRequested?: boolean;
}) {
  const mobile = useIsMobileViewport();
  const { query, title } = useTicketSessionMetadata(sessionId, showStatus);
  const session = query.data;
  const [state] = useSessionBadgeStates(session ? [session] : []);
  const unavailable = query.isError || session === null;
  const awaiting = !unavailable && !session?.archived && state?.kind === "awaiting";
  const needsResponse = awaiting || responseRequested;
  const status = unavailable
    ? "Unavailable"
    : !session
      ? "Loading…"
      : session.archived
        ? "Archived"
        : state?.kind === "running"
          ? "Running"
          : state?.kind === "starting"
            ? "Starting…"
            : state?.kind === "error"
              ? "Error"
              : state?.kind === "disconnected"
                ? "Disconnected"
                : "Idle";
  return (
    <Link
      className={cn(
        "rounded-sm text-foreground underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        showStatus
          ? "flex min-w-0 flex-1 items-start gap-3 rounded-lg p-3 transition-colors hover:bg-muted/40 focus-visible:ring-inset"
          : "inline-block max-w-full align-bottom wrap-anywhere hover:underline",
        showStatus && responseRequested && "bg-tag-pink hover:bg-brand-accent/10",
      )}
      title={`${title} (${sessionId})`}
      aria-label={
        showStatus ? `${needsResponse ? "Respond to" : "Open chat with"}: ${title}` : undefined
      }
      to={
        ticketId
          ? ticketSessionSearch(ticketId, sessionId)
          : mobile
            ? `/c/${encodeURIComponent(sessionId)}`
            : projectSessionPanelSearch(sessionId)
      }
    >
      {showStatus && (
        <BotIcon aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      )}
      <span className={showStatus ? "min-w-0 flex-1 space-y-1 text-sm" : undefined}>
        <span className={showStatus ? "block font-medium wrap-anywhere" : undefined}>{title}</span>
        {showStatus && (
          <span className="block text-muted-foreground wrap-anywhere">
            {involvement ||
              (responseRequested ? "Your contact for this response" : "Linked to this ticket")}
          </span>
        )}
        {showStatus && (
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-1">
            <span className="flex items-center gap-1.5 text-muted-foreground">
              {!unavailable && !session?.archived && state && <SessionStateBadge state={state} />}
              {!awaiting && <span>{status}</span>}
            </span>
            {responseRequested && !awaiting && <NeedsResponseBadge />}
            <span
              className={cn(
                "inline-flex items-center gap-1 font-medium",
                needsResponse && "text-brand-accent",
              )}
            >
              {needsResponse ? "Respond" : "Open chat"}
              <ChevronRightIcon aria-hidden className="size-3.5" />
            </span>
          </span>
        )}
      </span>
    </Link>
  );
}
