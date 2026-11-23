import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BotIcon, ChevronRightIcon } from "lucide-react";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import { fetchConversationById } from "@/hooks/useOpenSessionRow";
import { useSessionBadgeStates } from "@/hooks/useSessionBadgeStates";
import { Link } from "@/lib/routing";
import { getSessionSharePath } from "@/lib/shareLink";
import { findCachedListRow } from "@/lib/sessionListCache";
import { sessionAgentIdentity } from "@/lib/sessionAgentIdentity";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel } from "@/shell/sidebarNav";

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
  showAgentName = false,
  needsResponse = false,
  involvement,
  label,
  icon,
}: {
  sessionId: string;
  showStatus?: boolean;
  showAgentName?: boolean;
  needsResponse?: boolean;
  involvement?: string;
  label?: string;
  icon?: ReactNode;
}) {
  const { query, title: sessionTitle } = useTicketSessionMetadata(sessionId, showStatus);
  const session = query.data;
  const agent = session
    ? sessionAgentIdentity({
        agentName: session.agent_name,
        harness: session.harness,
        labels: session.labels,
      })
    : null;
  const title =
    label ??
    (showStatus || showAgentName ? agent?.displayName : undefined) ??
    (showAgentName ? "Agent" : sessionTitle);
  const [state] = useSessionBadgeStates(session ? [session] : []);
  const unavailable = query.isError || session === null;
  const awaiting = !unavailable && !session?.archived && state?.kind === "awaiting";
  const respond = !unavailable && !!session && !session.archived && (awaiting || needsResponse);
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
        showStatus && respond && "bg-tag-pink hover:bg-tag-pink",
      )}
      title={`${sessionTitle} (${sessionId})`}
      aria-label={showStatus ? `${respond ? "Respond to" : "Open chat with"}: ${title}` : undefined}
      to={getSessionSharePath(encodeURIComponent(sessionId))}
    >
      {showStatus &&
        (icon ??
          (agent ? (
            <ComposerAgentIcon agent={agent} className="mt-0.5 text-muted-foreground" />
          ) : (
            <BotIcon aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          )))}
      <span className={showStatus ? "min-w-0 flex-1 space-y-1 text-sm" : undefined}>
        <span className={showStatus ? "block font-medium wrap-anywhere" : undefined}>{title}</span>
        {showStatus && (
          <span className="block text-muted-foreground wrap-anywhere">
            {involvement || "Linked to this ticket"}
          </span>
        )}
        {showStatus && (
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-1">
            {!awaiting && (
              <span className="flex items-center gap-1.5 text-muted-foreground">
                {!unavailable && !session?.archived && state && <SessionStateBadge state={state} />}
                <span>{status}</span>
              </span>
            )}
            <span
              className={cn(
                "inline-flex items-center gap-1 font-medium",
                respond && "text-brand-accent",
              )}
            >
              {respond ? "Respond" : "Open chat"}
              <ChevronRightIcon aria-hidden className="size-3.5" />
            </span>
          </span>
        )}
      </span>
    </Link>
  );
}
