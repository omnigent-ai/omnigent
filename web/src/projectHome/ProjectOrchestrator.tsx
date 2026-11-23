import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Maximize2Icon, XIcon } from "lucide-react";
import { FullSessionChat } from "@/components/chat/FullSessionChat";
import { useAgentChatPresentation } from "@/components/chat/useAgentChatPresentation";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { useSessionAgent } from "@/hooks/useAgents";
import { getSessionSlim } from "@/lib/sessionsApi";
import { mergeTaggedDrafts } from "@/lib/composerMentions";
import {
  appendReplyDraft,
  restoreReplyDraft,
  serializeReplyDraft,
  snapshotReplyDraft,
} from "@/lib/replyDraft";
import { getSessionDraft, setSessionDraft } from "@/lib/sessionDrafts";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";
import { TicketError } from "./TicketControls";
import { Link, useNavigate } from "@/lib/routing";
import { useProjectOrchestratorSession } from "./useProjectAgentSession";
import { addTicketToSessionDraft } from "./projectTicketDraft";

/** Resolve the saved session, or offer the normal composer for explicit replacement. */
export function ProjectOrchestrator({
  projectName,
  ticket,
  onClose,
}: {
  projectName: string;
  ticket?: { id: string; title: string };
  /** Show the saved conversation in a closable panel instead of redirecting. */
  onClose?: () => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { projectId, sessionId, isLoading, error, canReplace, expectedMainSessionId, refresh } =
    useProjectOrchestratorSession(projectName);
  const ticketContext = useRef<string | null>(null);
  useEffect(() => {
    if (!sessionId || !projectId) return;
    const key = ticket ? `${sessionId}:${ticket.id}` : null;
    if (ticket && ticketContext.current !== key) {
      ticketContext.current = key;
      addTicketToSessionDraft(sessionId, { ...ticket, project_id: projectId });
    }
    if (!onClose) navigate(`/c/${encodeURIComponent(sessionId)}`, { replace: true });
  }, [sessionId, projectId, ticket, navigate, onClose]);
  const content =
    canReplace && projectId ? (
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
    ) : sessionId && onClose ? (
      <OrchestratorChat key={sessionId} sessionId={sessionId} />
    ) : (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-4 p-6">
        {isLoading || sessionId ? (
          <Spinner aria-label="Opening orchestrator" />
        ) : (
          <>
            <TicketError error={error ?? new Error("Orchestrator is unavailable.")} />
            {projectId && (
              <Button variant="ghost" onClick={() => void refresh()}>
                Try again
              </Button>
            )}
          </>
        )}
      </div>
    );
  if (!onClose) return content;
  return (
    <section
      aria-label="Orchestrator chat"
      data-project-session-panel
      className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[16px] border bg-background"
    >
      <header className="flex h-11 shrink-0 items-center gap-1 border-b border-border/60 pr-1.5 pl-4">
        <h2 className="min-w-0 flex-1 truncate text-ui font-medium">Orchestrator</h2>
        {sessionId && (
          <Button variant="ghost" size="icon-sm" asChild>
            <Link
              to={`/c/${encodeURIComponent(sessionId)}`}
              aria-label="Open orchestrator in full session"
              title="Open full session"
            >
              <Maximize2Icon />
            </Link>
          </Button>
        )}
        <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="Close orchestrator">
          <XIcon />
        </Button>
      </header>
      {content}
    </section>
  );
}

function OrchestratorChat({ sessionId }: { sessionId: string }) {
  const agent = useSessionAgent(sessionId);
  const presentation = useAgentChatPresentation(agent.isError ? undefined : agent.data);
  return (
    <RenderErrorBoundary name="chat" resetKey={sessionId}>
      <FullSessionChat
        sessionId={sessionId}
        sessionComposer
        evenComposerPadding
        presentation={presentation}
      />
    </RenderErrorBoundary>
  );
}
