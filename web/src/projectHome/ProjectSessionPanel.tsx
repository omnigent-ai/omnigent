import { Maximize2Icon, XIcon } from "lucide-react";
import { FullSessionChat } from "@/components/chat/FullSessionChat";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSession } from "@/hooks/useSession";
import { Link } from "@/lib/routing";

const HEADER_HEIGHT = "2.75rem";

/** A board session's transcript and composer, for a quick reply beside the board. */
export function ProjectSessionPanel({
  sessionId,
  onClose,
}: {
  sessionId: string;
  onClose: () => void;
}) {
  const { session } = useSession(sessionId);
  const title = session?.title || "Session";
  return (
    <section
      aria-label={title}
      data-testid="project-session-panel"
      data-project-session-panel=""
      className="relative flex min-h-0 flex-1 flex-col overflow-hidden rounded-[16px] border bg-background"
    >
      {/* Laid over the transcript, which scrolls under its glass. */}
      <header
        className="session-header-glass absolute inset-x-0 top-0 z-10 flex items-center gap-0.5 pr-1.5 pl-3"
        style={{ height: HEADER_HEIGHT }}
      >
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
      </header>
      <div className="flex min-h-0 flex-1 flex-col">
        <RenderErrorBoundary name="chat" resetKey={sessionId}>
          <FullSessionChat
            key={sessionId}
            sessionId={sessionId}
            evenComposerPadding
            transcriptTopInset={HEADER_HEIGHT}
          />
        </RenderErrorBoundary>
      </div>
    </section>
  );
}
