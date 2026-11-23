import { useEffect, useMemo, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSessionAgent } from "@/hooks/useAgents";
import { useAgentChatPresentation } from "@/components/chat/useAgentChatPresentation";
import { getSessionSlim } from "@/lib/sessionsApi";
import { mergeTaggedDrafts } from "@/lib/composerMentions";
import {
  appendReplyDraft,
  restoreReplyDraft,
  serializeReplyDraft,
  snapshotReplyDraft,
} from "@/lib/replyDraft";
import { getSessionDraft, setSessionDraft } from "@/lib/sessionDrafts";
import { FullSessionChat } from "@/components/chat/FullSessionChat";
import type { ChatPresentation } from "@/components/chat/assistantPresentation";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";
import { TicketError } from "./TicketControls";
import { useProjectChatSession } from "./useProjectAgentSession";
import { ticketResponseExtras } from "./ProjectTicketUpdates";
import { addTicketToSessionDraft } from "./projectTicketDraft";

export function ProjectChat({
  projectName,
  ticket,
}: {
  projectName: string;
  ticket?: { id: string; title: string };
}) {
  const queryClient = useQueryClient();
  const { projectId, sessionId, isLoading, error, canReplace, expectedMainSessionId, refresh } =
    useProjectChatSession(projectName);
  if (sessionId && projectId)
    return (
      <ProjectAgentChat
        key={sessionId}
        sessionId={sessionId}
        projectId={projectId}
        ticket={ticket}
      />
    );
  if (canReplace && projectId)
    return (
      <NewChatLandingScreen
        projectName={projectName}
        projectMainSession={{ projectId, expectedMainSessionId }}
        onSessionStarted={refresh}
        onMainSessionConflict={async (draft) => {
          try {
            const project = await refresh();
            const mainId = project.data?.main_session_id;
            if (project.isError || project.data?.main_session_state !== "active" || !mainId)
              return false;
            const main = await queryClient.fetchQuery({
              queryKey: ["session", mainId],
              queryFn: () => getSessionSlim(mainId),
              staleTime: 15_000,
            });
            if (main.archived || main.projectId !== projectId) return false;
            const existing = getSessionDraft(mainId) ?? { text: "", files: [] };
            const merged = mergeTaggedDrafts(existing.mentions, draft);
            const next = appendReplyDraft(restoreReplyDraft(existing.text, existing.replyDraft), {
              text: merged.text,
              quotes: [],
            });
            setSessionDraft(mainId, {
              ...existing,
              text: serializeReplyDraft(next),
              replyDraft: snapshotReplyDraft(next),
              files: [...existing.files, ...draft.files],
              mentions: merged.mentions,
            });
            return true;
          } catch {
            return false;
          }
        }}
        ticketContext={ticket ? { ...ticket, project_id: projectId } : undefined}
      />
    );
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-4 p-6">
      {isLoading ? (
        <Spinner aria-label="Loading chat" />
      ) : (
        <>
          <TicketError error={error ?? new Error("Main chat is unavailable.")} />
          {projectId && (
            <Button variant="ghost" onClick={() => void refresh()}>
              Try again
            </Button>
          )}
        </>
      )}
    </div>
  );
}

export function ProjectAgentChat({
  sessionId,
  projectId,
  ticket,
}: {
  sessionId: string;
  projectId: string;
  ticket?: { id: string; title: string };
}) {
  const agent = useSessionAgent(sessionId);
  const agentPresentation = useAgentChatPresentation(agent.isError ? undefined : agent.data);
  const ticketContext = useRef<string | null>(null);
  useEffect(() => {
    const key = ticket ? `${sessionId}:${ticket.id}` : null;
    if (ticket && ticketContext.current !== key) {
      ticketContext.current = key;
      addTicketToSessionDraft(sessionId, { ...ticket, project_id: projectId });
    }
  }, [sessionId, projectId, ticket]);
  const presentation = useMemo<ChatPresentation>(
    () => ({
      ...agentPresentation,
      responseExtras: (bubbles) => ticketResponseExtras(bubbles, projectId),
    }),
    [agentPresentation, projectId],
  );
  return (
    <section
      data-project-chat
      aria-label={presentation.assistant ? `${presentation.assistant.name} chat` : "Project chat"}
      className="relative mx-auto flex min-h-0 w-full max-w-4xl min-w-0 flex-1 flex-col px-1 md:px-6"
    >
      <RenderErrorBoundary name="chat" resetKey={sessionId}>
        <FullSessionChat sessionId={sessionId} sessionComposer presentation={presentation} />
      </RenderErrorBoundary>
    </section>
  );
}
