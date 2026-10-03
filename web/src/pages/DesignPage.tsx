/**
 * Design page (`/design`): every slide deck agents produced across recent
 * sessions, grouped by workspace, with the selected deck rendered in the same
 * viewer and design kit as the session file viewer.
 *
 * - Sessions come from `useCanvasSessions` (the sidebar and Canvas list).
 * - Each workspace is searched once, through its most recent session, using
 *   the Files panel's `/search` request; its kit indicator reads `kit.json` only.
 * - The selection lives in `?session=&file=`, so a deck is linkable and back
 *   and forward work. On a phone the list and the viewer take turns.
 */

import { useMemo } from "react";
import { useQueries, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import {
  AlertTriangleIcon,
  ArrowRightIcon,
  ChevronLeftIcon,
  PaletteIcon,
  PresentationIcon,
  RefreshCwIcon,
} from "lucide-react";
import { useCanvasSessions } from "@/canvas/canvasSessions";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useProjects, type ProjectSummary } from "@/hooks/useConversations";
import { fetchFileContent } from "@/hooks/useFileContent";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useViewerId } from "@/hooks/useViewerId";
import { fetchDeckSearch, fetchKitIndicator, type DeckSearchResult } from "@/lib/designDeckApi";
import {
  buildDesignGroups,
  deckName,
  isDeckPath,
  isDesignListEmpty,
  selectDesignWorkspaces,
  type DeckSearchState,
  type DesignDeck,
  type DesignGroup,
  type KitIndicatorState,
} from "@/lib/designDecks";
import { Link, useLocation, useNavigate, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { SlidesViewer } from "@/shell/SlidesViewer";

export const DESIGN_SESSION_PARAM = "session";
export const DESIGN_FILE_PARAM = "file";
const KIT_INSTRUCTIONS_URL =
  "https://github.com/omnigent-ai/omnigent/blob/main/examples/design-kits/sample/README.md";
const EMPTY_PROJECTS: ProjectSummary[] = [];
// The app defaults to a 30 s stale time without focus refetch; decks change
// while agents work, so these reads refresh on every mount and focus.
const LIVE_QUERY = { staleTime: 0, refetchOnMount: true, refetchOnWindowFocus: true } as const;
const QUERY_KEYS = ["design-deck-search", "design-kit", "design-deck"] as const;

/** Router state on list links, so the phone Back control can pop history. */
interface DesignLocationState {
  fromDeckList?: boolean;
}

function searchState(query: UseQueryResult<DeckSearchResult>): DeckSearchState {
  if (query.status === "error") return { status: "error", message: query.error.message };
  return query.data ?? { status: "loading" };
}

function deckHref(sessionId: string, path: string): string {
  const params = new URLSearchParams({
    [DESIGN_SESSION_PARAM]: sessionId,
    [DESIGN_FILE_PARAM]: path,
  });
  return `/design?${params}`;
}

function sessionFileHref(sessionId: string, path: string): string {
  return `/c/${encodeURIComponent(sessionId)}?file=${encodeURIComponent(path)}`;
}

export function DesignPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const isMobile = useIsMobileViewport();
  const { sessions, loaded, loadingMore, error, refresh } = useCanvasSessions();
  const projects = useProjects().data ?? EMPTY_PROJECTS;
  const viewerId = useViewerId();

  const workspaces = useMemo(
    () => selectDesignWorkspaces(sessions, projects, viewerId),
    [sessions, projects, viewerId],
  );
  const searches = useQueries({
    queries: workspaces.map((workspace) => ({
      queryKey: ["design-deck-search", workspace.session.id],
      queryFn: () => fetchDeckSearch(workspace.session.id),
      retry: false,
      ...LIVE_QUERY,
    })),
  });
  const searchStates = searches.map(searchState);
  const kits = useQueries({
    queries: workspaces.map((workspace, i) => {
      const search = searchStates[i];
      return {
        queryKey: ["design-kit", workspace.session.id],
        queryFn: () => fetchKitIndicator(workspace.session.id),
        enabled: search?.status === "ok" && search.paths.some(isDeckPath),
        retry: false,
        ...LIVE_QUERY,
      };
    }),
  });
  const groups = buildDesignGroups(
    workspaces,
    searchStates,
    kits.map((kit) => kit.data),
  );
  const retrySearch = (sessionId: string) => {
    const index = workspaces.findIndex((workspace) => workspace.session.id === sessionId);
    void searches[index]?.refetch();
  };

  const selectedSession = searchParams.get(DESIGN_SESSION_PARAM);
  const selectedFile = searchParams.get(DESIGN_FILE_PARAM);
  const selection =
    selectedSession && selectedFile ? { sessionId: selectedSession, path: selectedFile } : null;
  const showList = !isMobile || selection === null;
  const showViewer = !isMobile || selection !== null;

  const refreshAll = () => {
    void refresh();
    for (const key of QUERY_KEYS) void queryClient.invalidateQueries({ queryKey: [key] });
  };
  const backToList = () => {
    if ((location.state as DesignLocationState | null)?.fromDeckList) void navigate(-1);
    else setSearchParams({});
  };

  return (
    <div
      className="flex min-h-0 flex-1 flex-col"
      data-testid="design-page"
      style={{
        paddingTop: "calc(var(--omnigent-header-height) + var(--omnigent-inset-top))",
        paddingBottom: "var(--omnigent-inset-bottom)",
      }}
    >
      {showList && (
        <header className="flex items-center justify-between gap-4 px-6 pt-5 pb-3">
          <h1 className="text-2xl font-semibold">Design</h1>
          <Button variant="outline" size="sm" onClick={refreshAll} componentId="design.refresh">
            <RefreshCwIcon className="size-3.5" />
            Refresh
          </Button>
        </header>
      )}
      <div className="flex min-h-0 flex-1 border-t border-border">
        {showList && (
          <nav
            aria-label="Slide decks"
            className="min-h-0 w-full overflow-y-auto md:w-80 md:shrink-0 md:border-r md:border-border"
          >
            <DeckList
              groups={groups}
              selection={selection}
              sessionsLoading={!loaded}
              sessionsError={groups.length === 0 ? error : null}
              empty={isDesignListEmpty(groups, loaded && !loadingMore && !error)}
              onRetrySessions={() => void refresh()}
              onRetrySearch={retrySearch}
            />
          </nav>
        )}
        {showViewer && (
          <section aria-label="Deck viewer" className="flex min-h-0 min-w-0 flex-1 flex-col">
            {selection ? (
              <DeckPane
                key={`${selection.sessionId}\0${selection.path}`}
                sessionId={selection.sessionId}
                path={selection.path}
                onBack={isMobile ? backToList : undefined}
              />
            ) : (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center text-ui text-muted-foreground">
                <PresentationIcon className="size-6" />
                <p>Select a deck to view it here.</p>
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  );
}

function DeckList({
  groups,
  selection,
  sessionsLoading,
  sessionsError,
  empty,
  onRetrySessions,
  onRetrySearch,
}: {
  groups: DesignGroup[];
  selection: { sessionId: string; path: string } | null;
  sessionsLoading: boolean;
  sessionsError: string | null;
  empty: boolean;
  onRetrySessions: () => void;
  onRetrySearch: (sessionId: string) => void;
}) {
  if (sessionsLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="size-5 text-muted-foreground" aria-label="Loading sessions" />
      </div>
    );
  }
  if (sessionsError) {
    return (
      <ErrorRow message={`Couldn't load sessions: ${sessionsError}`} onRetry={onRetrySessions} />
    );
  }
  if (empty) {
    return (
      <div className="flex flex-col items-center gap-2 px-6 py-16 text-center text-ui text-muted-foreground">
        <PresentationIcon className="size-8 text-muted-foreground/50" />
        <p>
          No decks yet. Ask an agent for a slide deck; files ending in{" "}
          <code className="font-mono text-sm">.slides.html</code> appear here.
        </p>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-4 py-3">
      {groups.map((group) => {
        const headingId = `design-group-${group.workspace.session.id}`;
        return (
          <section key={group.workspace.path} aria-labelledby={headingId}>
            <div className="flex items-center gap-2 px-4 pb-1">
              <h2 id={headingId} className="min-w-0 truncate text-ui font-semibold">
                {group.workspace.label}
              </h2>
              {group.status === "ready" && <KitBadge kit={group.kit} />}
            </div>
            <GroupBody group={group} selection={selection} onRetry={onRetrySearch} />
          </section>
        );
      })}
    </div>
  );
}

function GroupBody({
  group,
  selection,
  onRetry,
}: {
  group: DesignGroup;
  selection: { sessionId: string; path: string } | null;
  onRetry: (sessionId: string) => void;
}) {
  const sessionId = group.workspace.session.id;
  if (group.status === "loading") {
    return (
      <div
        role="status"
        aria-label={`Searching ${group.workspace.label}`}
        className="flex animate-pulse flex-col gap-2 px-4 py-1"
      >
        <div className="h-4 w-2/3 rounded bg-muted" />
        <div className="h-3 w-1/2 rounded bg-muted" />
      </div>
    );
  }
  if (group.status === "unavailable") {
    return (
      <div className="flex flex-wrap items-center gap-2 px-4 text-sm text-muted-foreground">
        <span>Unavailable: open the session to start its runner</span>
        <Link
          to={`/c/${encodeURIComponent(sessionId)}`}
          className="text-foreground underline underline-offset-2"
          componentId="design.group.open_session"
        >
          Open session
        </Link>
      </div>
    );
  }
  if (group.status === "error") {
    return (
      <ErrorRow
        message={`Search failed: ${group.error ?? ""}`}
        onRetry={() => onRetry(sessionId)}
      />
    );
  }
  return (
    <ul className="flex flex-col">
      {group.decks.map((deck) => (
        <li key={deck.path}>
          <DeckRow
            deck={deck}
            selected={selection?.sessionId === deck.sessionId && selection.path === deck.path}
          />
        </li>
      ))}
    </ul>
  );
}

function DeckRow({ deck, selected }: { deck: DesignDeck; selected: boolean }) {
  const state: DesignLocationState = { fromDeckList: true };
  return (
    <Link
      to={deckHref(deck.sessionId, deck.path)}
      state={state}
      aria-current={selected ? "true" : undefined}
      componentId="design.deck.select"
      className={cn(
        "flex flex-col gap-0.5 px-4 py-2 outline-none hover:bg-muted focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring",
        selected && "bg-muted",
      )}
    >
      <span className="truncate text-ui font-medium">{deck.name}</span>
      <span className="truncate font-mono text-sm text-muted-foreground">{deck.path}</span>
      <span className="truncate text-sm text-muted-foreground">{deck.sessionTitle}</span>
    </Link>
  );
}

function KitBadge({ kit }: { kit: KitIndicatorState }) {
  const badge = "ml-auto flex shrink-0 items-center gap-1 text-sm text-muted-foreground";
  if (kit.status === "ok") {
    return (
      <span className={badge} title={`Design kit: ${kit.name}`}>
        <PaletteIcon className="size-3.5" aria-hidden />
        <span className="max-w-32 truncate">{kit.name}</span>
      </span>
    );
  }
  if (kit.status === "none") {
    return (
      <a
        href={KIT_INSTRUCTIONS_URL}
        target="_blank"
        rel="noreferrer"
        className={cn(badge, "underline underline-offset-2 hover:text-foreground")}
      >
        No kit
      </a>
    );
  }
  if (kit.status === "invalid") {
    return (
      <span className={cn(badge, "text-destructive")} title={kit.reason}>
        Kit invalid
      </span>
    );
  }
  return null;
}

function ErrorRow({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="flex items-center gap-2 px-4 text-sm">
      <AlertTriangleIcon className="size-4 shrink-0 text-destructive" />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      <Button variant="outline" size="sm" onClick={onRetry} componentId="design.retry">
        Retry
      </Button>
    </div>
  );
}

function DeckPane({
  sessionId,
  path,
  onBack,
}: {
  sessionId: string;
  path: string;
  onBack?: () => void;
}) {
  const deck = useQuery({
    queryKey: ["design-deck", sessionId, path],
    queryFn: () => fetchFileContent(sessionId, path),
    retry: 1,
    ...LIVE_QUERY,
  });
  const binary = deck.data?.encoding === "base64";
  return (
    <>
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        {onBack && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onBack}
            aria-label="Back to decks"
            componentId="design.back"
          >
            <ChevronLeftIcon className="size-4" />
            Back
          </Button>
        )}
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-ui font-medium">{deckName(path)}</h2>
          <p className="truncate font-mono text-sm text-muted-foreground">{path}</p>
        </div>
        <Button asChild variant="ghost" size="sm" className="shrink-0 text-sm">
          <Link to={sessionFileHref(sessionId, path)} componentId="design.open_in_session">
            Open in session
            <ArrowRightIcon className="ml-1 size-3.5" />
          </Link>
        </Button>
      </div>
      <div className="min-h-0 flex-1">
        {deck.isPending ? (
          <div className="flex h-full items-center justify-center">
            <Spinner className="size-5 text-muted-foreground" aria-label="Loading deck" />
          </div>
        ) : deck.isError || binary ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center text-ui">
            <AlertTriangleIcon className="size-6 text-destructive" />
            <p>
              {`Couldn't open this deck: ${deck.isError ? deck.error.message : "it is not a text file"}`}
            </p>
          </div>
        ) : (
          <SlidesViewer
            content={deck.data.content}
            truncated={deck.data.truncated}
            conversationId={sessionId}
          />
        )}
      </div>
    </>
  );
}
