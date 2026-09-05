import { useQuery } from "@tanstack/react-query";
import { ArrowLeftIcon } from "lucide-react";

import { ArchiveTranscriptViewer } from "@/components/archive/ArchiveTranscriptViewer";
import { Button } from "@/components/ui/button";
import type { Conversation } from "@/hooks/useConversations";
import { getSessionSlim } from "@/lib/sessionsApi";
import { useNavigate, useParams, useSearchParams } from "@/lib/routing";

/** Read-only, addressable destination for item/response Archive Library links. */
export function ArchiveSessionPage() {
  const { sessionId = "" } = useParams<{ sessionId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const itemId = searchParams.get("item");
  const responseId = searchParams.get("response");
  const sessionQuery = useQuery({
    queryKey: ["archive-session-metadata", sessionId],
    queryFn: () => getSessionSlim(sessionId),
    enabled: sessionId.length > 0,
    staleTime: 60_000,
    retry: false,
  });

  if (sessionQuery.isLoading) {
    return <p className="p-6 pt-20 text-sm text-muted-foreground">Loading archive reference…</p>;
  }
  if (!sessionQuery.data) {
    return (
      <div className="p-6 pt-20">
        <p className="text-sm text-destructive">This archive reference is unavailable.</p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-3"
          onClick={() => navigate(-1)}
        >
          <ArrowLeftIcon /> Back
        </Button>
      </div>
    );
  }

  const session = sessionQuery.data;
  const conversation: Conversation = {
    id: session.id,
    object: "conversation",
    title: session.title,
    created_at: session.createdAt,
    updated_at: session.updatedAt ?? session.createdAt,
    archived_at: session.archivedAt ?? null,
    labels: session.labels ?? {},
    permission_level: session.permissionLevel ?? null,
    runner_id: session.runnerId,
    host_id: session.hostId,
    workspace: session.workspace,
    agent_id: session.agentId,
    agent_name: session.agentName,
    archived: session.archived ?? true,
    ...(itemId
      ? {
          search_match: {
            item_id: itemId,
            response_id: responseId ?? itemId,
            created_at: session.createdAt,
            snippet: "",
          },
        }
      : {}),
  };

  return (
    <main className="flex h-full min-h-0 min-w-0 overflow-hidden pt-[calc(var(--omnigent-header-height)+var(--omnigent-inset-top))] pb-[var(--omnigent-inset-bottom)]">
      <ArchiveTranscriptViewer conversation={conversation} onBack={() => navigate(-1)} />
    </main>
  );
}
