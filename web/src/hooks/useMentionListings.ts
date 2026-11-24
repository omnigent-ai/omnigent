import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import {
  MENTION_SEARCH_DEBOUNCE_MS,
  useSessionMentionEntries,
} from "@/hooks/useSessionMentionEntries";
import {
  useWorkspaceAllFiles,
  useWorkspaceDirectory,
  useWorkspaceFileSearch,
  type WorkspaceFile,
} from "@/hooks/useWorkspaceChangedFiles";
import {
  MENTION_MATCH_CAP,
  parseMentionToken,
  rankMentionEntries,
  type MentionState,
  type SessionMention,
} from "@/lib/composerMentions";
import { isTempConvId } from "@/lib/tempConversationId";

/** Inputs for the rows of a composer's "@" menu. */
export interface MentionListingsParams {
  /** The session the composer sends to: its workspace is listed and it is not a row. */
  conversationId: string | null;
  /** Whether the agent opens "@"-tagged workspace files (see harnessSupportsFileMentions). */
  enabled: boolean;
  /** The active "@" token, or null while the menu is closed. */
  mention: MentionState | null;
  /** The session's children, listed first among the session rows. */
  subagents: readonly ChildSessionInfo[];
  /** Whether the workspace environment is still resolving, so the root listing is not settled yet. */
  environmentLoading?: boolean;
}

/** The rows of a composer's "@" menu for its active token. */
export interface MentionListings {
  /** Directory the token browses ("" is the workspace root). */
  mentionDir: string;
  /** Typed segment that filters that directory's entries. */
  mentionFilter: string;
  /** The directory's files and folders, folders first, capped. */
  mentionEntries: WorkspaceFile[];
  /** Session rows matching the typed text. */
  sessionEntries: SessionMention[];
  /**
   * True while the token is active but its listing hasn't resolved yet. A stray Enter
   * must not send the half-typed token then; a settled zero-match is deliberately not pending.
   */
  mentionListingPending: boolean;
}

/**
 * The file and session rows of the "@" menu, shared by the in-session composer and a
 * grid cell's composer so both list the same things. The token after "@" doubles as a
 * path: text up to the last "/" browses that directory, and the rest filters it.
 */
export function useMentionListings({
  conversationId,
  enabled,
  mention,
  subagents,
  environmentLoading = false,
}: MentionListingsParams): MentionListings {
  const composerSessionId = isTempConvId(conversationId) ? null : conversationId;
  const { dir: mentionDir, filter: mentionFilter } = parseMentionToken(mention?.query ?? "");
  // Root lists from the cached all-files query; a sub-directory uses the lazy per-dir hook.
  const workspaceFilesQuery = useWorkspaceAllFiles(composerSessionId ?? undefined, { enabled });
  const mentionDirQuery = useWorkspaceDirectory(
    conversationId ?? undefined,
    enabled && mention && mentionDir ? mentionDir : null,
  );
  const mentionSourceEntries: WorkspaceFile[] = mentionDir
    ? (mentionDirQuery.data ?? [])
    : (workspaceFilesQuery.data?.data ?? []);
  // Typing after "@" also searches the server, debounced like the palette.
  const debouncedMentionQuery = useDebouncedValue(mention?.query ?? "", MENTION_SEARCH_DEBOUNCE_MS);
  // Root-level typing also finds files in unexpanded folders (files only).
  const mentionFileSearch = useWorkspaceFileSearch(
    composerSessionId ?? undefined,
    enabled && mention && !mentionDir ? debouncedMentionQuery : "",
  );
  // Folders first, filtered by the typed segment, capped (see rankMentionEntries),
  // then deeper search hits the cached listing doesn't already show.
  const mentionEntries = ((): WorkspaceFile[] => {
    if (!enabled || !mention) return [];
    const listed = rankMentionEntries(mentionSourceEntries, mentionFilter);
    if (mentionDir || !mentionFilter) return listed;
    const listedPaths = new Set(listed.map((entry) => entry.path));
    // The search ran on the debounced text; re-check hits against the live filter.
    const needle = mentionFilter.toLowerCase();
    const deeper = rankMentionEntries(
      (mentionFileSearch.data?.files ?? []).filter(
        (file) => !listedPaths.has(file.path) && file.path.toLowerCase().includes(needle),
      ),
      "",
    );
    return [...listed, ...deeper].slice(0, MENTION_MATCH_CAP);
  })();
  const sessionEntries = useSessionMentionEntries({
    mention,
    debouncedQuery: debouncedMentionQuery,
    currentSessionId: conversationId,
    subagents,
  });
  const mentionListingPending =
    enabled &&
    mention !== null &&
    (mentionDir ? mentionDirQuery.isLoading : environmentLoading || workspaceFilesQuery.isLoading);
  return { mentionDir, mentionFilter, mentionEntries, sessionEntries, mentionListingPending };
}
