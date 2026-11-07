import { useMemo } from "react";

import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { type Conversation, useSessionSearch } from "@/hooks/useConversations";
import { useOptionalLoadedConversations } from "@/hooks/useSidebarData";
import { isTempConvId } from "@/lib/tempConversationId";
import {
  type MentionState,
  rankSessionMentions,
  type SessionMention,
} from "@/lib/composerMentions";
import { childPrimaryLabel } from "@/shell/subagentLabel";

/** Matches the command palette's search debounce. */
export const MENTION_SEARCH_DEBOUNCE_MS = 300;

const NO_SUBAGENTS: readonly ChildSessionInfo[] = [];

function topLevelSessions(
  rows: Conversation[] | undefined,
  currentSessionId: string | null,
): SessionMention[] {
  return (rows ?? [])
    .filter(
      (row) =>
        row.id !== currentSessionId &&
        !row.provisional &&
        !row.archived &&
        !row.parent_session_id &&
        !isTempConvId(row.id),
    )
    .map((row) => ({ id: row.id, title: row.title || "Untitled session", isSubagent: false }));
}

/**
 * Session rows for the composer ``@`` menu: the current session's subagents,
 * then the sessions already loaded for the sidebar, then server title-search
 * hits for the typed text. Only the search issues a request, and only once
 * ``debouncedQuery`` is non-empty; the other sources are cache reads.
 *
 * :param mention: Active ``@`` token, or ``null`` when the menu is closed.
 * :param debouncedQuery: ``mention.query`` after the search debounce.
 * :param currentSessionId: Session the composer sends to; excluded from rows.
 * :param subagents: Children of the current session, listed first.
 */
export function useSessionMentionEntries({
  mention,
  debouncedQuery,
  currentSessionId,
  subagents = NO_SUBAGENTS,
}: {
  mention: MentionState | null;
  debouncedQuery: string;
  currentSessionId: string | null;
  subagents?: readonly ChildSessionInfo[];
}): SessionMention[] {
  // Optional: surfaces rendered outside the app shell (stories, embeds) have no
  // sidebar data, and then the menu lists subagents and search hits only.
  const loadedSessions = useOptionalLoadedConversations();
  const search = useSessionSearch(debouncedQuery, "title", {
    enabled: mention != null && !debouncedQuery.includes("/"),
    includeArchived: false,
  });
  return useMemo(() => {
    if (!mention) return [];
    const candidates: SessionMention[] = [
      ...subagents.map((child) => ({
        id: child.id,
        title: childPrimaryLabel(child),
        isSubagent: true,
      })),
      ...topLevelSessions(
        loadedSessions?.pages.flatMap((page) => page.data),
        currentSessionId,
      ),
      ...topLevelSessions(
        search.data?.pages.flatMap((page) => page.data),
        currentSessionId,
      ),
    ];
    return rankSessionMentions(candidates, mention.query);
  }, [mention, subagents, loadedSessions, search.data, currentSessionId]);
}
