import { useCallback, useEffect, useMemo, useRef } from "react";
import { ArrowLeftIcon, Maximize2Icon } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import { useSessionAgent } from "@/hooks/useAgents";
import { sessionAgentIdentity } from "@/lib/sessionAgentIdentity";
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
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Link, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { CHAT_COLUMN_WIDTH } from "@/pages/chatLayout";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";
import { ProjectAgentIdentity } from "./ProjectAgentIdentity";
import { ticketPanelSearch } from "./projectHomeNavigation";
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
  const [search] = useSearchParams();
  const agent = useSessionAgent(sessionId);
  const identity = useMemo(
    () =>
      !agent.isError && agent.data
        ? sessionAgentIdentity({ agentName: agent.data.name, harness: agent.data.harness })
        : null,
    [agent.data, agent.isError],
  );
  const ticketContext = useRef<string | null>(null);
  useEffect(() => {
    const key = ticket ? `${sessionId}:${ticket.id}` : null;
    if (ticket && ticketContext.current !== key) {
      ticketContext.current = key;
      addTicketToSessionDraft(sessionId, { ...ticket, project_id: projectId });
    }
  }, [sessionId, projectId, ticket]);
  const renderHeader = useCallback(
    (empty: boolean) => (
      <div
        className={cn("flex shrink-0 justify-center pb-8", empty ? "pt-[min(12vh,6rem)]" : "pt-5")}
      >
        {agent.data && !agent.isError && <ProjectAgentIdentity agent={agent.data} />}
      </div>
    ),
    [agent.data, agent.isError],
  );
  const presentation = useMemo<ChatPresentation>(
    () => ({
      header: renderHeader,
      assistant: identity
        ? {
            name: identity.displayName,
            icon: <ComposerAgentIcon agent={identity} />,
          }
        : undefined,
      responseExtras: (bubbles) => ticketResponseExtras(bubbles, projectId),
    }),
    [renderHeader, identity, projectId],
  );
  const backSearch = new URLSearchParams(search);
  backSearch.set("view", "board");
  return (
    <section
      data-project-chat
      aria-label={identity ? `${identity.displayName} chat` : "Project chat"}
      className={cn(
        "relative mx-auto flex min-h-0 min-w-0 flex-1 flex-col px-1 md:px-4",
        CHAT_COLUMN_WIDTH,
      )}
    >
      {ticket && (
        <Link
          to={ticketPanelSearch(ticket.id, backSearch)}
          aria-label="Back to ticket"
          className="mr-10 mb-1 flex min-w-0 items-center gap-1.5 px-3 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeftIcon className="size-3.5 shrink-0" />
          <span>Back to ticket</span>
          <span aria-hidden>·</span>
          <span className="truncate">{ticket.title}</span>
        </Link>
      )}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            asChild
            variant="ghost"
            size="icon-sm"
            className="absolute top-0 right-3 z-10 text-muted-foreground md:right-7"
          >
            <Link to={`/c/${sessionId}`} aria-label="Open full session">
              <Maximize2Icon />
            </Link>
          </Button>
        </TooltipTrigger>
        <TooltipContent>Open full session</TooltipContent>
      </Tooltip>
      <RenderErrorBoundary name="chat" resetKey={sessionId}>
        <FullSessionChat sessionId={sessionId} sessionComposer presentation={presentation} />
      </RenderErrorBoundary>
    </section>
  );
}
