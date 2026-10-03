/**
 * Design page (`/design`): a landing of every slide deck agents produced
 * across recent sessions, and a studio for one deck.
 *
 * - Landing: deck cards grouped by workspace (phase 1 data flow), a search,
 *   suggestion chips, and New design, which creates a session in place.
 * - Studio (`?session=&file=`, plus `view=full` or the phone's `view=chat`):
 *   the session's compact chat beside the live deck preview.
 */

import { useMemo, useState } from "react";
import { useQueries, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import {
  AlertTriangleIcon,
  PaletteIcon,
  PresentationIcon,
  RefreshCwIcon,
  SearchIcon,
} from "lucide-react";
import { useCanvasSessions } from "@/canvas/canvasSessions";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { useProjects, type ProjectSummary } from "@/hooks/useConversations";
import { useViewerId } from "@/hooks/useViewerId";
import { fetchDeckSearch, fetchKitIndicator, type DeckSearchResult } from "@/lib/designDeckApi";
import {
  buildDesignGroups,
  filterDesignGroups,
  isDeckPath,
  isDesignListEmpty,
  selectDesignWorkspaces,
  type DeckSearchState,
  type DesignDeck,
  type DesignGroup,
  type KitIndicatorState,
} from "@/lib/designDecks";
import {
  DESIGN_SUGGESTIONS,
  readStudioParams,
  studioHref,
  type StudioView,
} from "@/lib/designStudio";
import { Link, useLocation, useNavigate, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { DesignStudio, LIVE_QUERY } from "./design/DesignStudio";
import { NewDesignDialog } from "./design/NewDesignDialog";

const KIT_INSTRUCTIONS_URL =
  "https://github.com/omnigent-ai/omnigent/blob/main/examples/design-kits/sample/README.md";
const EMPTY_PROJECTS: ProjectSummary[] = [];
const QUERY_KEYS = ["design-deck-search", "design-kit", "design-deck"] as const;

/** Router state on landing links, so Back can pop history instead of pushing. */
interface DesignLocationState {
  fromDeckList?: boolean;
  /** The session was just created from New design. */
  fresh?: boolean;
}

function searchState(query: UseQueryResult<DeckSearchResult>): DeckSearchState {
  if (query.status === "error") return { status: "error", message: query.error.message };
  return query.data ?? { status: "loading" };
}

export function DesignPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const studio = readStudioParams(searchParams);
  const state = (location.state as DesignLocationState | null) ?? {};

  if (!studio) return <DesignLanding />;
  const setView = (view: StudioView) =>
    void navigate(studioHref(studio.sessionId, studio.path, view), {
      replace: true,
      state: location.state,
    });
  const back = () => {
    if (state.fromDeckList) void navigate(-1);
    else void navigate("/design");
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
      <DesignStudio
        key={`${studio.sessionId}\0${studio.path}`}
        sessionId={studio.sessionId}
        path={studio.path}
        view={studio.view}
        fresh={state.fresh === true}
        onView={setView}
        onBack={back}
      />
    </div>
  );
}

function DesignLanding() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { sessions, loaded, loadingMore, error, refresh } = useCanvasSessions();
  const projects = useProjects().data ?? EMPTY_PROJECTS;
  const viewerId = useViewerId();
  const [query, setQuery] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [prefill, setPrefill] = useState<string | undefined>(undefined);

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
  const visibleGroups = filterDesignGroups(groups, query);
  const empty = isDesignListEmpty(groups, loaded && !loadingMore && !error);

  const retrySearch = (sessionId: string) => {
    const index = workspaces.findIndex((workspace) => workspace.session.id === sessionId);
    void searches[index]?.refetch();
  };
  const refreshAll = () => {
    void refresh();
    for (const key of QUERY_KEYS) void queryClient.invalidateQueries({ queryKey: [key] });
  };
  const openDialog = (prompt?: string) => {
    setPrefill(prompt);
    setDialogOpen(true);
  };
  const takenDeckNames = (folder: string) => {
    const path = folder.replace(/[/\\]+$/, "");
    return groups.find((g) => g.workspace.path === path)?.decks.map((d) => d.name) ?? [];
  };
  const openCreated = (sessionId: string, path: string) => {
    void refresh();
    const state: DesignLocationState = { fromDeckList: true, fresh: true };
    void navigate(studioHref(sessionId, path), { state });
  };

  return (
    <PageScroll contentClassName="px-6" maxWidthClassName="max-w-5xl" data-testid="design-page">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold">Design</h1>
          <p className="text-ui text-muted-foreground">Slides your agents made, on brand</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="outline" onClick={refreshAll} componentId="design.refresh">
            <RefreshCwIcon className="size-3.5" />
            Refresh
          </Button>
          <Button onClick={() => openDialog()} componentId="design.new">
            New design
          </Button>
        </div>
      </div>

      <div className="relative mb-4">
        <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search designs…"
          aria-label="Search designs"
          componentId="design.search"
          className="pl-9"
        />
      </div>

      {!loaded ? (
        <div className="flex justify-center py-12">
          <Spinner className="size-5 text-muted-foreground" aria-label="Loading sessions" />
        </div>
      ) : error && groups.length === 0 ? (
        <ErrorRow message={`Couldn't load sessions: ${error}`} onRetry={() => void refresh()} />
      ) : empty ? (
        <div className="flex flex-col items-center gap-2 py-12 text-center text-ui text-muted-foreground">
          <PresentationIcon className="size-8 text-muted-foreground/50" />
          <p>
            No decks yet. Ask an agent for a slide deck; files ending in{" "}
            <code className="font-mono text-sm">.slides.html</code> appear here.
          </p>
          <Suggestions onPick={openDialog} showHeading={false} className="mt-3 border-t-0 pt-0" />
        </div>
      ) : (
        <>
          {query.trim() && visibleGroups.length === 0 ? (
            <p className="py-10 text-center text-ui text-muted-foreground">No designs match</p>
          ) : (
            <div className="flex flex-col gap-6">
              {visibleGroups.map((group) => (
                <DeckGroup key={group.workspace.path} group={group} onRetry={retrySearch} />
              ))}
            </div>
          )}
          <Suggestions onPick={openDialog} />
        </>
      )}

      <NewDesignDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        initialPrompt={prefill}
        takenDeckNames={takenDeckNames}
        onCreated={openCreated}
      />
    </PageScroll>
  );
}

function DeckGroup({
  group,
  onRetry,
}: {
  group: DesignGroup;
  onRetry: (sessionId: string) => void;
}) {
  const sessionId = group.workspace.session.id;
  const headingId = `design-group-${sessionId}`;
  return (
    <section aria-labelledby={headingId}>
      <div className="mb-2 flex items-center gap-2">
        <h2 id={headingId} className="min-w-0 truncate text-ui font-semibold">
          {group.workspace.label}
        </h2>
        {group.status === "ready" && <KitBadge kit={group.kit} />}
      </div>
      {group.status === "loading" ? (
        <div
          role="status"
          aria-label={`Searching ${group.workspace.label}`}
          className="grid animate-pulse gap-2 sm:grid-cols-2 lg:grid-cols-3"
        >
          <div className="h-16 rounded-lg bg-muted" />
        </div>
      ) : group.status === "unavailable" ? (
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          <span>Unavailable: open the session to start its runner</span>
          <Link
            to={`/c/${encodeURIComponent(sessionId)}`}
            className="text-foreground underline underline-offset-2"
            componentId="design.group.open_session"
          >
            Open session
          </Link>
        </div>
      ) : group.status === "error" ? (
        <ErrorRow
          message={`Search failed: ${group.error ?? ""}`}
          onRetry={() => onRetry(sessionId)}
        />
      ) : (
        <>
          {group.decks.length > 0 && (
            <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {group.decks.map((deck) => (
                <li key={deck.path}>
                  <DeckCard deck={deck} />
                </li>
              ))}
            </ul>
          )}
          {group.truncated && (
            <p className="py-2 text-sm text-muted-foreground">
              Search stopped early - more decks may exist in this workspace.
            </p>
          )}
        </>
      )}
    </section>
  );
}

function DeckCard({ deck }: { deck: DesignDeck }) {
  const state: DesignLocationState = { fromDeckList: true };
  return (
    <Link
      to={studioHref(deck.sessionId, deck.path)}
      state={state}
      componentId="design.deck.open"
      className="flex h-full flex-col gap-0.5 rounded-lg border border-border bg-card px-3 py-2 outline-none transition-colors hover:bg-muted focus-visible:ring-1 focus-visible:ring-ring"
    >
      <span className="truncate text-ui font-medium">{deck.name}</span>
      <span className="truncate font-mono text-sm text-muted-foreground">{deck.path}</span>
      <span className="truncate text-sm text-muted-foreground">{deck.sessionTitle}</span>
    </Link>
  );
}

function Suggestions({
  onPick,
  showHeading = true,
  className,
}: {
  onPick: (prompt: string) => void;
  showHeading?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("mt-6 border-t border-border/60 pt-4", className)}>
      {showHeading && <h2 className="mb-3 text-ui text-muted-foreground">Suggestions</h2>}
      <div className="flex flex-wrap justify-center gap-2 sm:justify-start">
        {DESIGN_SUGGESTIONS.map((s) => (
          <button
            key={s.id}
            type="button"
            onClick={() => onPick(s.title)}
            className="rounded-lg border border-border bg-card px-3 py-1.5 text-ui text-foreground transition-colors hover:bg-muted"
          >
            {s.title}
          </button>
        ))}
      </div>
    </div>
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
    <div className="flex items-center gap-2 text-sm">
      <AlertTriangleIcon className="size-4 shrink-0 text-destructive" />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      <Button variant="outline" size="sm" onClick={onRetry} componentId="design.retry">
        Retry
      </Button>
    </div>
  );
}
