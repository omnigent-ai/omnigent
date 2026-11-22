import { ArrowLeftIcon, Maximize2Icon, XIcon } from "lucide-react";
import { FullSessionChat } from "@/components/chat/FullSessionChat";
import type { ChatPresentation } from "@/components/chat/assistantPresentation";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSession } from "@/hooks/useSession";
import { Link, useSearchParams } from "@/lib/routing";
import { sessionAgentIdentity } from "@/lib/sessionAgentIdentity";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import { ticketPanelSearch } from "./projectHomeNavigation";

const HEADER_HEIGHT = "2.75rem";

/** A board session's transcript and composer, for a quick reply beside the board. */
export function ProjectSessionPanel({
  sessionId,
  onClose,
  ticket,
  assistant,
}: {
  sessionId: string;
  onClose: () => void;
  ticket?: { id: string; title: string };
  assistant?: ChatPresentation["assistant"];
}) {
  const { session } = useSession(sessionId);
  const [search] = useSearchParams();
  const title = session?.title || "Session";
  const agent = session ? sessionAgentIdentity(session) : null;
  const identity =
    assistant ??
    (agent
      ? {
          name: agent.displayName,
          icon: <ComposerAgentIcon agent={agent} />,
        }
      : undefined);
  const headerHeight = ticket ? "4.75rem" : HEADER_HEIGHT;
  return (
    <section
      aria-label={title}
      data-testid="project-session-panel"
      data-project-session-panel=""
      className="relative flex min-h-0 flex-1 flex-col overflow-hidden rounded-[16px] border bg-background"
    >
      {/* Laid over the transcript, which scrolls under its glass. */}
      <header
        className="session-header-glass absolute inset-x-0 top-0 z-10 flex flex-col pr-1.5 pl-3"
        style={{ height: headerHeight }}
      >
        {ticket && (
          <Link
            to={ticketPanelSearch(ticket.id, search)}
            aria-label="Back to ticket"
            title={ticket.title}
            className="mt-1 flex h-7 min-w-0 shrink-0 items-center gap-1.5 rounded-sm text-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ArrowLeftIcon aria-hidden className="size-3.5 shrink-0" />
            <span className="shrink-0">Back to ticket</span>
            <span aria-hidden>·</span>
            <span className="truncate">{ticket.title}</span>
          </Link>
        )}
        <div className="flex min-h-0 flex-1 items-center gap-0.5">
          <h2 className="min-w-0 flex-1 truncate font-medium">{title}</h2>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button asChild variant="ghost" size="icon-sm" className="text-muted-foreground">
                <Link to={`/c/${sessionId}`} aria-label="Open full session">
                  <Maximize2Icon />
                </Link>
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">Open full session</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="Close panel"
                className="text-muted-foreground"
                onClick={onClose}
              >
                <XIcon />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">Close</TooltipContent>
          </Tooltip>
        </div>
      </header>
      <div className="flex min-h-0 flex-1 flex-col">
        <RenderErrorBoundary name="chat" resetKey={sessionId}>
          <FullSessionChat
            key={sessionId}
            sessionId={sessionId}
            sessionComposer
            presentation={identity ? { assistant: identity } : undefined}
            evenComposerPadding
            transcriptTopInset={headerHeight}
          />
        </RenderErrorBoundary>
      </div>
    </section>
  );
}
