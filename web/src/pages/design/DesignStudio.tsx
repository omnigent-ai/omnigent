// Design studio: one deck's compact chat beside its live preview. The chat is
// the session's side chat pane with full history; the preview refetches on the
// session's changed-files event (see chatStore) and when the agent's turn ends.

import { useEffect, useRef, useState, type MouseEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangleIcon,
  ArrowRightIcon,
  ChevronLeftIcon,
  MessageSquareIcon,
  XIcon,
} from "lucide-react";
import { WorkingIndicator, computeIsTurnActive } from "@/components/chat/chatBubbleParts";
import { ConversationScopeContext } from "@/components/chat/conversationScope";
import { SideChatPane } from "@/components/chat/SideChatPane";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { fetchFileContent } from "@/hooks/useFileContent";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { deckName } from "@/lib/designDecks";
import { deckPreviewState, type StudioView } from "@/lib/designStudio";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { SlidesViewer } from "@/shell/SlidesViewer";
import { ensureConversationStreamed } from "@/store/chatStore";

// The app defaults to a 30 s stale time without focus refetch; decks change
// while agents work, so the deck read refreshes on every mount and focus.
export const LIVE_QUERY = {
  staleTime: 0,
  refetchOnMount: true,
  refetchOnWindowFocus: true,
} as const;

function sessionFileHref(sessionId: string, path: string): string {
  return `/c/${encodeURIComponent(sessionId)}?file=${encodeURIComponent(path)}`;
}

const isMissing = (error: Error | null) => error?.message.startsWith("404") ?? false;

export function DesignStudio({
  sessionId,
  path,
  view,
  fresh,
  onView,
  onBack,
}: {
  sessionId: string;
  path: string;
  view: StudioView;
  /** Just created from New design: only an observed turn end means "not written". */
  fresh: boolean;
  onView: (view: StudioView) => void;
  onBack: () => void;
}) {
  const isMobile = useIsMobileViewport();
  const queryClient = useQueryClient();
  // Keep the stream bound even while the chat is hidden (Full, phone preview).
  useEffect(() => {
    void ensureConversationStreamed(sessionId);
  }, [sessionId]);

  const session = useConversationEntryState(sessionId);
  const turnActive =
    computeIsTurnActive(session.sessionStatus, session.status === "streaming") ||
    session.sessionStatus === "launching";
  const hydrated = !session.loadingConversation && session.blocks.length > 0;
  const [sawTurnEnd, setSawTurnEnd] = useState(false);
  const wasActive = useRef(turnActive);
  useEffect(() => {
    if (wasActive.current && !turnActive) {
      setSawTurnEnd(true);
      void queryClient.invalidateQueries({ queryKey: ["design-deck", sessionId, path] });
    }
    wasActive.current = turnActive;
  }, [turnActive, queryClient, sessionId, path]);
  const turnEnded = !turnActive && (sawTurnEnd || (!fresh && hydrated));

  const deck = useQuery({
    queryKey: ["design-deck", sessionId, path],
    queryFn: () => fetchFileContent(sessionId, path),
    retry: (count, error) => !isMissing(error) && count < 1,
    ...LIVE_QUERY,
  });
  const binary = deck.data?.encoding === "base64";
  const file = deck.data
    ? binary
      ? "error"
      : "ok"
    : deck.isError
      ? isMissing(deck.error)
        ? "missing"
        : "error"
      : "loading";
  const preview = deckPreviewState({ file, turnEnded });

  const showChat = isMobile ? view === "chat" : view !== "full";
  const showPreview = !isMobile || view !== "chat";
  const back = (event: MouseEvent) => {
    event.preventDefault();
    onBack();
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="design-studio">
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        <Link
          to="/design"
          onClick={back}
          componentId="design.studio.back"
          className="flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-ui text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <ChevronLeftIcon className="size-4" aria-hidden />
          <span className={cn(isMobile && "sr-only")}>Back to designs</span>
        </Link>
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-ui font-medium">{deckName(path)}</h2>
          <p className="truncate font-mono text-sm text-muted-foreground">{path}</p>
        </div>
        {!isMobile && (
          <div role="group" aria-label="Studio layout" className="flex shrink-0 gap-1">
            {(["preview", "full"] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                aria-pressed={(view === "full") === (mode === "full")}
                onClick={() => onView(mode)}
                className={cn(
                  "rounded-md px-3 py-1 text-ui font-medium transition-colors",
                  (view === "full") === (mode === "full")
                    ? "bg-muted text-foreground"
                    : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                )}
              >
                {mode === "full" ? "Full" : "Preview"}
              </button>
            ))}
          </div>
        )}
        {isMobile &&
          (view === "chat" ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onView("preview")}
              aria-label="Close chat"
              componentId="design.studio.close_chat"
            >
              <XIcon className="size-4" />
            </Button>
          ) : (
            <Button
              variant="outline"
              size="sm"
              onClick={() => onView("chat")}
              componentId="design.studio.chat"
            >
              <MessageSquareIcon className="size-4" />
              Chat
            </Button>
          ))}
        {!(isMobile && view === "chat") && (
          <Button asChild variant="ghost" size="sm" className="shrink-0 text-sm">
            <Link to={sessionFileHref(sessionId, path)} componentId="design.open_in_session">
              <span className={cn(isMobile && "sr-only")}>Open in session</span>
              <ArrowRightIcon className="ml-1 size-3.5" aria-hidden />
            </Link>
          </Button>
        )}
      </div>
      <div className="flex min-h-0 flex-1">
        {showChat && (
          <aside
            aria-label="Design chat"
            className="flex min-h-0 w-full flex-col md:w-[380px] md:shrink-0 md:border-r md:border-border"
          >
            <SideChatPane
              childId={sessionId}
              fullHistory
              placeholder="Ask for changes to this deck"
            />
          </aside>
        )}
        {showPreview && (
          <section aria-label="Deck preview" className="flex min-h-0 min-w-0 flex-1 flex-col">
            {preview === "deck" && deck.data ? (
              <SlidesViewer
                content={deck.data.content}
                truncated={deck.data.truncated}
                conversationId={sessionId}
              />
            ) : preview === "loading" ? (
              <div className="flex h-full items-center justify-center">
                <Spinner className="size-5 text-muted-foreground" aria-label="Loading deck" />
              </div>
            ) : preview === "waiting" ? (
              <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center text-ui">
                <p className="text-muted-foreground">Waiting for the first slide</p>
                {turnActive && (
                  <ConversationScopeContext.Provider value={sessionId}>
                    <WorkingIndicator />
                  </ConversationScopeContext.Provider>
                )}
              </div>
            ) : preview === "not-written" ? (
              <div className="flex h-full items-center justify-center p-8 text-center text-ui text-muted-foreground">
                <p>
                  The agent has not written <code className="font-mono text-sm">{path}</code> yet
                </p>
              </div>
            ) : (
              <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center text-ui">
                <AlertTriangleIcon className="size-6 text-destructive" />
                <p>
                  {`Couldn't open this deck: ${deck.error?.message ?? "it is not a text file"}`}
                </p>
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
