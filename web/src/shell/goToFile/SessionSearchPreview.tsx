// The finder's Sessions tab preview: the session's name and folder, then the messages around
// the newest one that mentions the query, read from the session's latest items.

import { useQuery } from "@tanstack/react-query";

import { HighlightedText, occurrenceRanges } from "@/components/ui/quick-pick";
import type { Conversation } from "@/hooks/useConversations";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { relativeTime } from "@/lib/relativeTime";
import { fetchSessionItemsPage } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";

import { conversationDisplayLabel } from "../sidebarNav";
import { type PreviewMessage, sessionPreviewState } from "./sessionSearchExcerpt";

/** The latest items read for a preview; an older match shows as the search's excerpt. */
export const PREVIEW_ITEM_LIMIT = 50;
/** Moving through rows quickly reads only the row the selection settles on. */
const PREVIEW_SETTLE_MS = 120;

export function sessionPreviewQueryKey(sessionId: string): readonly unknown[] {
  return ["session", sessionId, "search-preview"];
}

const ROLE_LABEL: Record<PreviewMessage["role"], string> = { user: "You", assistant: "Agent" };

function PreviewNote({ children }: { children: React.ReactNode }) {
  return <p className="px-3 pt-3 text-xs text-muted-foreground">{children}</p>;
}

function MessageExcerpt({ message }: { message: PreviewMessage }) {
  return (
    <div
      data-matched={message.matched || undefined}
      className={cn("rounded-md px-3 py-2", message.matched && "bg-muted/60 ring-1 ring-border")}
    >
      <div className="text-xs font-medium text-muted-foreground">{ROLE_LABEL[message.role]}</div>
      <p className="whitespace-pre-wrap break-words text-sm text-foreground">
        {message.clippedStart && "…"}
        <HighlightedText text={message.text} ranges={message.ranges} highlight="strong" />
        {message.clippedEnd && "…"}
      </p>
    </div>
  );
}

/** The search's own excerpt, for a match older than the messages the preview read. */
function EarlierMatch({ snippet, query }: { snippet: string; query: string }) {
  return (
    <div className="px-3 pt-3">
      <div className="text-xs text-muted-foreground">Earlier in this conversation</div>
      <p className="mt-1 rounded-md bg-muted/60 px-3 py-2 text-sm ring-1 ring-border">
        <HighlightedText
          text={snippet}
          ranges={occurrenceRanges(query, snippet)}
          highlight="strong"
        />
      </p>
    </div>
  );
}

/**
 * The Sessions finder's preview of a session: its name and folder, then the messages around
 * the newest one that mentions the query, read from the session's latest items.
 */
export function SessionSearchPreview({ session, query }: { session: Conversation; query: string }) {
  const sessionId = useDebouncedValue(session.id, PREVIEW_SETTLE_MS);
  const settled = sessionId === session.id;
  const items = useQuery({
    queryKey: sessionPreviewQueryKey(sessionId),
    queryFn: ({ signal }) =>
      fetchSessionItemsPage(sessionId, { limit: PREVIEW_ITEM_LIMIT, signal }),
    enabled: settled,
    staleTime: 30_000,
    retry: false,
  });
  const label = conversationDisplayLabel(session);
  const subtitle = session.workspace ?? "";
  const trimmed = query.trim();
  const state =
    settled && items.data ? sessionPreviewState(items.data, trimmed, session.search_snippet) : null;

  let body: React.ReactNode;
  if (!settled || items.isPending) {
    body = <PreviewNote>Loading conversation…</PreviewNote>;
  } else if (items.isError || !state) {
    body = <PreviewNote>Couldn't load this conversation. Open it to read it.</PreviewNote>;
  } else if (state.kind === "empty") {
    body = <PreviewNote>This session has no messages yet.</PreviewNote>;
  } else {
    body = (
      <>
        {state.snippet ? <EarlierMatch snippet={state.snippet} query={trimmed} /> : null}
        {state.kind === "older-only" ? (
          <PreviewNote>
            Its latest activity has no messages, such as tool calls. Open the session to read its
            earlier messages.
          </PreviewNote>
        ) : (
          <>
            {!state.excerpt.found && <PreviewNote>Latest messages</PreviewNote>}
            <div className="flex flex-col gap-1 p-2">
              {state.excerpt.messages.map((message) => (
                <MessageExcerpt key={message.position} message={message} />
              ))}
            </div>
          </>
        )}
      </>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="session-search-preview">
      <div className="flex shrink-0 items-baseline gap-2 border-b border-border px-3 py-1.5 text-xs">
        <span className="min-w-0 truncate font-medium text-foreground" title={label}>
          {label}
        </span>
        {subtitle && (
          <span className="min-w-0 shrink-[1000] truncate text-muted-foreground" title={subtitle}>
            {subtitle}
          </span>
        )}
        <span className="ml-auto shrink-0 text-muted-foreground">
          {relativeTime(session.updated_at * 1000, Date.now())}
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{body}</div>
    </div>
  );
}
