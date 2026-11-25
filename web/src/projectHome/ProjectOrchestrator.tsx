import { OutdatedHostBanner } from "./OutdatedHostBanner";
import { useEffect, useMemo, useRef, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Maximize2Icon, MessagesSquareIcon, RotateCcwIcon, XIcon } from "lucide-react";
import { FullSessionChat } from "@/components/chat/FullSessionChat";
import { useAgentChatPresentation } from "@/components/chat/useAgentChatPresentation";
import type { ChatPresentation } from "@/components/chat/assistantPresentation";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { useSessionAgent } from "@/hooks/useAgents";
import { useCanStartSideChat } from "@/hooks/usePermissions";
import { useSession } from "@/hooks/useSession";
import {
  newPendingSideChatId,
  SIDE_CHAT_COMMAND_PREFIX,
  supportsSideChat,
  usesNativeSideChatFork,
} from "@/lib/sideChat";
import { useChatStore } from "@/store/chatStore";
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
        {sessionId && !canReplace && <LeadSideChatButton sessionId={sessionId} />}
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
  charter,
  onClose,
}: {
  sessionId: string;
  name: string;
  /** One line on what this team member does, shown while the chat is empty. */
  charter?: string | null;
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
      <OrchestratorChat
        key={sessionId}
        sessionId={sessionId}
        persona={name}
        intro={<TeamMemberChatIntro sessionId={sessionId} name={name} charter={charter} />}
      />
    </section>
  );
}

/** First questions for a team member; each fills the composer to send or finish. */
export const TEAM_MEMBER_STARTERS: readonly { label: string; text: string }[] = [
  { label: "What are you working on?", text: "What are you working on right now?" },
  {
    label: "What's assigned to you?",
    text: "Which tickets are assigned to you, and where is each one?",
  },
  { label: "Review ticket…", text: "Review ticket " },
];

/** The first sentence of a team member's instructions, as its one-line charter. */
export function teamMemberCharter(instructions: string | null | undefined): string | null {
  const text = (instructions ?? "").trim().replace(/\s+/g, " ");
  if (!text) return null;
  const sentences = text.match(/[^.!?]+[.!?]+(?=\s|$)|[^.!?]+$/g) ?? [text];
  // "You are the designer." says little; carry on into the next sentence.
  const first = sentences[0].trim();
  const sentence = first.length < 40 && sentences[1] ? `${first} ${sentences[1].trim()}` : first;
  return sentence.length > 200 ? `${sentence.slice(0, 199).trimEnd()}…` : sentence;
}

/** What an empty team-member chat shows: its charter and a few ways to start. */
function TeamMemberChatIntro({
  sessionId,
  name,
  charter,
}: {
  sessionId: string;
  name: string;
  charter?: string | null;
}) {
  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-3 px-4 text-center">
      {charter && (
        <p className="text-sm text-muted-foreground" data-testid="team-member-charter">
          {charter}
        </p>
      )}
      <div
        role="group"
        aria-label={`Ways to start with ${name}`}
        className="flex flex-wrap justify-center gap-2"
      >
        {TEAM_MEMBER_STARTERS.map((starter) => (
          <Button
            key={starter.label}
            type="button"
            variant="outline"
            size="sm"
            className="rounded-full"
            onClick={() => {
              const existing = getSessionDraft(sessionId) ?? { text: "", files: [] };
              setSessionDraft(sessionId, { ...existing, text: starter.text });
              // Put the cursor at the end so the prompt can be sent or finished.
              requestAnimationFrame(() => {
                const composer = document.querySelector<HTMLTextAreaElement>(
                  "[data-project-session-panel] textarea",
                );
                if (!composer) return;
                composer.focus();
                composer.setSelectionRange(composer.value.length, composer.value.length);
              });
            }}
          >
            {starter.label}
          </Button>
        ))}
      </div>
    </div>
  );
}

/** Opens a side chat with the Lead in its full session, without interrupting the Lead. */
function LeadSideChatButton({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate();
  const { session } = useSession(sessionId);
  const canStart = useCanStartSideChat(sessionId, session?.permissionLevel ?? null);
  if (!supportsSideChat(session?.harness, canStart)) return null;
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      aria-label="Side chat with the Lead"
      title="Side chat: talk to a private copy of the Lead without interrupting it"
      data-testid="lead-side-chat"
      onClick={() => {
        if (usesNativeSideChatFork(session?.harness)) {
          // Codex forks in-process from a typed /side, so prefill the command.
          const existing = getSessionDraft(sessionId) ?? { text: "", files: [] };
          setSessionDraft(sessionId, { ...existing, text: SIDE_CHAT_COMMAND_PREFIX });
        } else {
          // The rail opens this empty tab on the Lead's page; its first send creates the fork.
          const pendingId = newPendingSideChatId();
          const store = useChatStore.getState();
          store.requestSideChatFocus(pendingId);
          store.openSideChatWithDraft(pendingId, "", sessionId);
        }
        navigate(`/c/${encodeURIComponent(sessionId)}`);
      }}
    >
      <MessagesSquareIcon />
    </Button>
  );
}

/** Shows *intro* under the chat's header while the transcript is empty. */
function withIntro(base: ChatPresentation, intro: ReactNode): ChatPresentation {
  if (!intro) return base;
  const header = base.header;
  return {
    ...base,
    header: (empty) => (
      <>
        {header?.(empty)}
        {empty && intro}
      </>
    ),
  };
}

function OrchestratorChat({
  sessionId,
  intro,
  persona = "Lead",
}: {
  sessionId: string;
  intro?: ReactNode;
  persona?: string;
}) {
  const agent = useSessionAgent(sessionId);
  const base = useAgentChatPresentation(agent.isError ? undefined : agent.data, persona);
  const presentation = useMemo(() => withIntro(base, intro), [base, intro]);
  return (
    <RenderErrorBoundary name="chat" resetKey={sessionId}>
      <OutdatedHostBanner
        sessionId={sessionId}
        name={persona === "Lead" ? "the Lead" : persona}
        className="mx-3 mt-3 w-auto"
      />
      <FullSessionChat
        sessionId={sessionId}
        sessionComposer
        evenComposerPadding
        presentation={presentation}
      />
    </RenderErrorBoundary>
  );
}
