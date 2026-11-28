// Go to File: a file by name or path, a line by its text, or a session; every tab searches the
// same typed text, and the footer says when an answer isn't the whole story.

import { useMemo, useRef, useState } from "react";
import { useNavigate } from "@/lib/routing";

import { CompactShortcutKeys } from "@/components/ui/kbd";
import { type QuickPickItem, QuickPickDialog } from "@/components/ui/quick-pick";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useWorkspaceFileSearch, useWorkspaceServeable } from "@/hooks/useWorkspaceChangedFiles";
import {
  type ContentSearchInput,
  type ContentSearchResult,
  EMPTY_CONTENT_SEARCH,
} from "@/lib/contentSearch";
import { readRecentWorkspaceFiles } from "@/lib/recentWorkspaceFiles";
import { shortcutKeys } from "@/lib/shortcuts/registry";

import type { OpenFileOptions } from "../FileViewerContext";
import { goToFileSearchState } from "../goToFileSearchStatus";
import { useShowMoreParking } from "../paletteSessionItems";
import { GO_TO_FILE_LABEL } from "./goToFileCommands";
import type { GoToFileTarget } from "./goToFileItems";
import { SessionSearchPreview } from "./SessionSearchPreview";
import { GoToFilePreview } from "./GoToFilePreview";
import { goToFileView } from "./goToFileView";
import {
  hasFileFilter,
  TextSearchFilters,
  TextSearchToggles,
  useTextSearchOptionKeys,
} from "./TextSearchControls";
import { textSearchView } from "./textSearchView";
import { useGoToFileTextSearch } from "./useGoToFileTextSearch";
import {
  SESSION_SEARCH_COPY,
  type SessionsFinderTarget,
  useSessionsFinderView,
} from "./useSessionsFinderView";

const INPUT_TEST_ID = "go-to-file-input";

/** How long typing pauses before the workspace is searched. */
const SEARCH_DEBOUNCE_MS = 150;

/** Finds a file by name or path, a line by its text, or a session by its name or messages. */
export type GoToFileMode = "files" | "text" | "sessions";

const MODES: Record<GoToFileMode, { tab: string; shortcut?: "palette.files" | "palette.text" }> = {
  files: { tab: "Files", shortcut: "palette.files" },
  text: { tab: "Text", shortcut: "palette.text" },
  sessions: { tab: SESSION_SEARCH_COPY.tab },
};

/** Each mode's dialog title, field label and placeholder. */
const MODE_COPY: Record<GoToFileMode, { title: string; label: string; placeholder: string }> = {
  files: {
    title: GO_TO_FILE_LABEL,
    label: "Go to a file",
    placeholder: "Go to a file in this session…",
  },
  text: {
    title: "Search Text in Workspace",
    label: "Search text in files",
    placeholder: "Search text in this session's files…",
  },
  sessions: SESSION_SEARCH_COPY,
};

/** What a finder row opens: a file, at a line for a text match, or a session. */
export type FinderTarget = GoToFileTarget | SessionsFinderTarget;

function isFileTarget(target: FinderTarget): target is GoToFileTarget {
  return !("kind" in target);
}

/** The tabs a finder shows: Files and Text only with an open session whose files it searches. */
export function finderModes(hasSession: boolean, textSearch: boolean): GoToFileMode[] {
  if (!hasSession) return ["sessions"];
  return textSearch ? ["files", "text", "sessions"] : ["files", "sessions"];
}

export interface GoToFileDialogProps {
  /** The session whose files are searched; null offers Sessions alone. */
  sessionId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: GoToFileMode;
  onModeChange: (mode: GoToFileMode) => void;
  /** Whether the session's files can be searched for text; without it there's no Text tab. */
  textSearch: boolean;
  /** Text to start with, such as a key typed into the Files page's field. */
  initialQuery?: string;
  /** Opens the file, at a line for a text match; the opener records it among the recent files. */
  openFile: (path: string, options?: OpenFileOptions) => void;
  /** Shows this text search, with its answer when there is one, on the Search page. */
  openInSearch: (input: ContentSearchInput, result: ContentSearchResult | undefined) => void;
}

/** Mounted while open, so each opening starts from its own query. */
export function GoToFileDialog(props: GoToFileDialogProps) {
  if (!props.open) return null;
  return <OpenGoToFileDialog {...props} />;
}

function ModeTabs({
  modes,
  mode,
  onModeChange,
}: {
  modes: readonly GoToFileMode[];
  mode: GoToFileMode;
  onModeChange: (mode: GoToFileMode) => void;
}) {
  return (
    <Tabs
      value={mode}
      onValueChange={(next) => {
        const found = modes.find((id) => id === next);
        if (found) onModeChange(found);
      }}
    >
      <TabsList aria-label="Find files, text or sessions">
        {modes.map((id) => (
          <TabsTrigger
            key={id}
            value={id}
            // The query field keeps the keyboard, so the tab switches here: preventing the
            // mouse-down also stops the tab's own handler, which would otherwise switch it.
            onMouseDown={(event) => {
              event.preventDefault();
              if (event.button === 0 && !event.ctrlKey) onModeChange(id);
            }}
            className="gap-1.5"
          >
            {MODES[id].tab}
            {MODES[id].shortcut && <CompactShortcutKeys keys={shortcutKeys(MODES[id].shortcut)} />}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}

function OpenGoToFileDialog({
  sessionId,
  open,
  onOpenChange,
  mode,
  onModeChange,
  textSearch,
  initialQuery = "",
  openFile,
  openInSearch,
}: GoToFileDialogProps) {
  const [query, setQuery] = useState(initialQuery);
  const isMobile = useIsMobileViewport();
  const navigate = useNavigate();
  const modes = finderModes(sessionId !== null, textSearch);
  const text = mode === "text";
  const sessions = mode === "sessions";
  const files = mode === "files";
  const workspaceSessionId = sessionId ?? undefined;

  // Files: the workspace's path search, and the recent files before a query.
  const searchedQuery = useDebouncedValue(files ? query : "", SEARCH_DEBOUNCE_MS);
  const serveable = useWorkspaceServeable(workspaceSessionId);
  const search = useWorkspaceFileSearch(workspaceSessionId, searchedQuery);
  // Read once per opening: what was opened while it shows is this dialog's own doing.
  const [recentPaths] = useState(() => (sessionId ? readRecentWorkspaceFiles(sessionId) : []));
  const filesView = useMemo(
    () =>
      goToFileView(
        goToFileSearchState({
          query,
          searchedQuery,
          serveable,
          data: search.data,
          isSuccess: search.isSuccess,
          isError: search.isError,
          isFetching: search.isFetching,
          isPlaceholderData: search.isPlaceholderData,
        }),
        query,
        recentPaths,
      ),
    [
      query,
      searchedQuery,
      serveable,
      search.data,
      search.isSuccess,
      search.isError,
      search.isFetching,
      search.isPlaceholderData,
      recentPaths,
    ],
  );

  // Text: the same query searched in the files' contents, with its options.
  const [options, setOptions] = useState<Omit<ContentSearchInput, "query">>(EMPTY_CONTENT_SEARCH);
  const [filtering, setFiltering] = useState(false);
  const input = useMemo(() => ({ ...options, query }), [options, query]);
  const textSearchRun = useGoToFileTextSearch(sessionId ?? "", input, text && sessionId !== null);
  const textView = useMemo(
    () => textSearchView(textSearchRun.state, input),
    [textSearchRun.state, input],
  );
  const setInput = (next: ContentSearchInput) => {
    const { query: nextQuery, ...nextOptions } = next;
    setOptions(nextOptions);
    setQuery(nextQuery);
  };
  const controls = {
    input,
    onInputChange: setInput,
    filtering,
    onFilteringChange: setFiltering,
  };
  // The options' Alt keys work while the keyboard is anywhere in this dialog, found from its
  // toggles, which show only in Text.
  const togglesRef = useRef<HTMLSpanElement>(null);
  useTextSearchOptionKeys(() => togglesRef.current?.closest("[role=dialog]") ?? null, controls);

  // Sessions: ⌘K's session search, over the sessions the user can see.
  const sessionsView = useSessionsFinderView(sessions, query);

  const views: Record<
    GoToFileMode,
    { items: QuickPickItem<FinderTarget>[]; emptyMessage: string | null; status: string | null }
  > = {
    files: filesView,
    text: textView,
    sessions: { items: sessionsView.items, emptyMessage: sessionsView.emptyMessage, status: null },
  };
  const view = views[mode];
  const copy = MODE_COPY[mode];
  // Show more keeps the keyboard's place, as in ⌘K.
  const showMore = useShowMoreParking(view.items, { query, open });
  return (
    <QuickPickDialog<FinderTarget>
      open={open}
      onOpenChange={onOpenChange}
      title={copy.title}
      label={copy.label}
      placeholder={copy.placeholder}
      contentClassName="top-[12vh]"
      heading={
        modes.length > 1
          ? {
              label: sessionId ? "Find in Workspace" : "Find",
              end: <ModeTabs modes={modes} mode={mode} onModeChange={onModeChange} />,
            }
          : undefined
      }
      query={query}
      onQueryChange={setQuery}
      activeId={showMore.activeId}
      onActiveIdChange={showMore.clear}
      // The rows come ranked: recent files first, then the best matches, or lines as found.
      segments={[{ sourceId: `go-to-file:${mode}`, ranking: "provider", items: view.items }]}
      emptyMessage={view.emptyMessage}
      status={
        sessions && sessionsView.failure ? (
          <SessionsSearchFailure {...sessionsView.failure} />
        ) : undefined
      }
      summary={() => view.status}
      inputEnd={
        text ? (
          <span ref={togglesRef} className="contents">
            <TextSearchToggles {...controls} />
          </span>
        ) : undefined
      }
      belowInput={
        text && (filtering || hasFileFilter(input)) ? (
          <TextSearchFilters {...controls} />
        ) : undefined
      }
      acceptLabel="Open"
      // ⌘↵ hands the search to the Search page, with what it found so far.
      onAlternate={
        text ? () => ({ afterClose: () => openInSearch(input, textSearchRun.result) }) : undefined
      }
      alternateLabel={text ? "Open in Search" : undefined}
      // Beside the list in a wide window; a narrow one has no room for it.
      renderPreview={
        isMobile
          ? undefined
          : ({ value }) => {
              if (isFileTarget(value)) {
                return sessionId ? (
                  <GoToFilePreview
                    sessionId={sessionId}
                    path={value.path}
                    line={value.line}
                    column={value.column}
                    endColumn={value.endColumn}
                  />
                ) : null;
              }
              const session = value.kind === "session" ? sessionsView.sessionFor(value.id) : null;
              return session ? <SessionSearchPreview session={session} query={query} /> : null;
            }
      }
      inputTestId={INPUT_TEST_ID}
      onAccept={(item) => {
        const { value } = item;
        if (!isFileTarget(value)) {
          if (value.kind === "more") {
            showMore.accept({ ...item, value });
            return "keepOpen";
          }
          sessionsView.rememberRow(value.id);
          return { afterClose: () => navigate(`/c/${value.id}`) };
        }
        return {
          afterClose: () =>
            value.line === undefined
              ? openFile(value.path)
              : openFile(value.path, { line: value.line, column: value.column }),
        };
      }}
    />
  );
}

/** A failed session search or page above the rows it left, with Retry, as in ⌘K's palette. */
function SessionsSearchFailure({
  message,
  retrying,
  retry,
}: {
  message: string;
  retrying: boolean;
  retry: () => void;
}) {
  return (
    <p className="px-3 py-2 text-xs text-muted-foreground">
      <span role="status">{message}</span>
      <button type="button" className="ml-2 underline" disabled={retrying} onClick={retry}>
        Retry
      </button>
    </p>
  );
}
