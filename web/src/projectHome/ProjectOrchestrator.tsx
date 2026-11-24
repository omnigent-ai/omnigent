import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Maximize2Icon, RotateCcwIcon, XIcon } from "lucide-react";
import { FullSessionChat } from "@/components/chat/FullSessionChat";
import { useAgentChatPresentation } from "@/components/chat/useAgentChatPresentation";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { useSessionAgent } from "@/hooks/useAgents";
import { getSessionSlim, rolloverOrchestrator } from "@/lib/sessionsApi";
import { showToast } from "@/components/ui/toast";
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
          <Spinner aria-label="Opening the Lead" />
        ) : (
          <>
            <TicketError error={error ?? new Error("The Lead is unavailable.")} />
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
      aria-label="Lead chat"
      data-project-session-panel
      className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[16px] border bg-background"
    >
      <header className="flex h-11 shrink-0 items-center gap-1 border-b border-border/60 pr-1.5 pl-4">
        <h2 className="min-w-0 flex-1 truncate text-ui font-medium">Lead</h2>
        {sessionId && !canReplace && (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Roll over the Lead"
            title="Roll over: continue in a fresh Lead session with a handoff"
            data-testid="rollover-orchestrator"
            onClick={() => {
              if (
                !window.confirm(
                  "Start a fresh Lead session? It gets a handoff of the board and recent messages. This conversation is kept as read-only history, and messages to it are forwarded.",
                )
              )
                return;
              rolloverOrchestrator(sessionId).then(
                () => showToast("The Lead will roll over when its current turn ends"),
                (err: unknown) => {
                  const detail = err instanceof Error && err.message ? `: ${err.message}` : "";
                  showToast(`Couldn't roll over${detail}`);
                },
              );
            }}
          >
            <RotateCcwIcon />
          </Button>
        )}
        {sessionId && (
          <Button variant="ghost" size="icon-sm" asChild>
            <Link
              to={`/c/${encodeURIComponent(sessionId)}`}
              aria-label="Open the Lead in full session"
              title="Open full session"
            >
              <Maximize2Icon />
            </Link>
          </Button>
        )}
        <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="Close the Lead">
          <XIcon />
        </Button>
      </header>
      {content}
    </section>
  );
}

/** A persona's session in the project's side chat, framed like the Lead's. */
export function ProjectPersonaChat({
  sessionId,
  name,
  onClose,
}: {
  sessionId: string;
  name: string;
  onClose: () => void;
}) {
  return (
    <section
      aria-label={`${name} chat`}
      data-project-session-panel
      className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[16px] border bg-background"
    >
      <header className="flex h-11 shrink-0 items-center gap-1 border-b border-border/60 pr-1.5 pl-4">
        <h2 className="min-w-0 flex-1 truncate text-ui font-medium">{name}</h2>
        <Button variant="ghost" size="icon-sm" asChild>
          <Link
            to={`/c/${encodeURIComponent(sessionId)}`}
            aria-label={`Open ${name} in full session`}
            title="Open full session"
          >
            <Maximize2Icon />
          </Link>
        </Button>
        <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label={`Close ${name}`}>
          <XIcon />
        </Button>
      </header>
      <OrchestratorChat key={sessionId} sessionId={sessionId} />
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
