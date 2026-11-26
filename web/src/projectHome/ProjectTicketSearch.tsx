import { useEffect, useRef, type ReactNode } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { ArrowLeftIcon, SearchIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Link, useSearchParams } from "@/lib/routing";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";
import {
  searchTickets,
  type TicketSearchMatch,
  type TicketSearchPage,
  type TicketStage,
} from "@/lib/projectTicketsApi";
import { ticketPanelSearch } from "./projectHomeNavigation";
import { TicketStageBadge } from "./TicketStageAppearance";

/** Typing settles this long before a search runs. */
export const TICKET_SEARCH_DEBOUNCE_MS = 250;
// A page that read its bounded slice of the board without enough matches continues
// on its own this many times, so a sparse search still fills the list.
const AUTO_CONTINUE_PAGES = 4;
const WANTED_RESULTS = 20;

export function useTicketSearchEnabled() {
  const info = useServerInfo();
  return info !== "loading" && info.project_ticket_search === true;
}

/** The trimmed search once typing settles; empty means the board shows. */
export function useSettledTicketSearch(query: string) {
  return useDebouncedValue(query.trim(), TICKET_SEARCH_DEBOUNCE_MS);
}

export function useTicketSearch(projectId: string | null, query: string) {
  return useInfiniteQuery({
    queryKey: ["project-tickets", projectId, "search", query],
    queryFn: ({ pageParam }) => searchTickets(projectId!, query, { after: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page: TicketSearchPage) =>
      page.has_more ? (page.last_id ?? undefined) : undefined,
    enabled: projectId !== null && query !== "",
    staleTime: 10_000,
  });
}

/** The board toolbar's ticket search box; Escape or the clear button empties it. */
export function TicketSearchBox({
  value,
  onChange,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  className?: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <div className={cn("relative w-52 shrink-0 max-sm:w-full", className)}>
      <SearchIcon
        aria-hidden
        className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
      />
      <Input
        ref={inputRef}
        type="search"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape" && value) {
            event.preventDefault();
            event.stopPropagation();
            onChange("");
          }
        }}
        placeholder="Search tickets…"
        aria-label="Search tickets"
        data-testid="ticket-search"
        className="h-7 rounded-full pr-8 pl-8 text-[13px] [&::-webkit-search-cancel-button]:appearance-none"
      />
      {value && (
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Clear search"
          className="absolute top-1/2 right-1 -translate-y-1/2 text-muted-foreground"
          onClick={() => {
            onChange("");
            inputRef.current?.focus();
          }}
        >
          <XIcon />
        </Button>
      )}
    </div>
  );
}

const FIELD_LABELS: Record<TicketSearchMatch["field"], string> = {
  title: "Title",
  description: "Description",
  comment: "Comment",
  label: "Label",
  id: "ID",
};

/** *text* with each search word marked, case-insensitively. */
export function highlightWords(text: string, query: string): ReactNode[] {
  const words = [...new Set(query.toLowerCase().split(/\s+/).filter(Boolean))];
  if (words.length === 0) return [text];
  const escaped = words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp(`(${escaped.join("|")})`, "gi");
  // Each part is keyed by where it starts in the text.
  let offset = 0;
  return text.split(pattern).map((part, index) => {
    const start = offset;
    offset += part.length;
    return index % 2 === 1 ? (
      <mark key={start} className="rounded-sm bg-brand-accent/20 px-0.5 text-foreground">
        {part}
      </mark>
    ) : (
      part
    );
  });
}

/** Search results in place of the board: ticket rows, most recently updated first. */
export function TicketSearchResults({
  projectId,
  query,
  selectedTicketId,
  onClear,
}: {
  projectId: string | null;
  query: string;
  selectedTicketId: string | null;
  onClear: () => void;
}) {
  const [searchParams] = useSearchParams();
  const search = useTicketSearch(projectId, query);
  const { data, hasNextPage, isFetching, isError, fetchNextPage } = search;
  const pages = data?.pages ?? [];
  const seen = new Set<string>();
  // A ticket updated between pages can appear twice; keep its first, newer row.
  const results = pages
    .flatMap((page) => page.data)
    .filter((ticket) => (seen.has(ticket.id) ? false : (seen.add(ticket.id), true)));
  const stages = new Map<string, TicketStage>(
    (pages[0]?.stages ?? []).map((stage) => [stage.id, stage]),
  );
  const scanned = pages.reduce((total, page) => total + page.scanned, 0);
  const sparse = results.length < WANTED_RESULTS && pages.length < AUTO_CONTINUE_PAGES;
  useEffect(() => {
    if (sparse && hasNextPage && !isFetching && !isError) void fetchNextPage();
  }, [sparse, hasNextPage, isFetching, isError, fetchNextPage]);
  const now = Date.now();
  return (
    <section
      aria-label="Ticket search results"
      data-testid="ticket-search-results"
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto px-4 pb-4"
    >
      <div
        className="flex items-center gap-2 pb-2 text-sm text-muted-foreground"
        aria-live="polite"
      >
        {search.isPending ? (
          <>
            <Spinner className="size-3.5" aria-label="Searching" />
            <span>Searching…</span>
          </>
        ) : (
          <span>
            {results.length === 0
              ? hasNextPage
                ? `No matches in the ${scanned} most recently updated tickets`
                : `No tickets match “${query}”`
              : `${results.length}${hasNextPage ? "+" : ""} ${results.length === 1 ? "ticket" : "tickets"} · newest first`}
          </span>
        )}
        <Button variant="ghost" size="sm" className="ml-auto shrink-0" onClick={onClear}>
          <ArrowLeftIcon aria-hidden />
          Back to board
        </Button>
      </div>
      {isError && (
        <p role="alert" className="pb-2 text-sm text-destructive">
          Search failed. {search.error instanceof Error ? search.error.message : ""}
        </p>
      )}
      <ul className="flex flex-col gap-2">
        {results.map((ticket) => {
          const stage = stages.get(ticket.stage_id);
          const snippet = ticket.match?.snippet;
          const selected = ticket.id === selectedTicketId;
          return (
            <li key={ticket.id}>
              <Link
                to={ticketPanelSearch(ticket.id, searchParams)}
                aria-current={selected ? "true" : undefined}
                data-testid="ticket-search-result"
                className={cn(
                  "flex flex-col gap-1.5 rounded-xl border bg-card px-3.5 py-3 transition-colors hover:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
                  selected && "border-brand-accent/50 bg-brand-accent/5",
                )}
              >
                <div className="flex min-w-0 items-start gap-2">
                  <span className="line-clamp-2 min-w-0 flex-1 text-sm font-medium wrap-anywhere text-foreground">
                    {highlightWords(ticket.title, query)}
                  </span>
                  <span
                    className="shrink-0 pt-0.5 text-xs text-muted-foreground tabular-nums"
                    title={new Date(ticket.updated_at * 1000).toLocaleString()}
                  >
                    {relativeTime(ticket.updated_at * 1000, now)}
                  </span>
                </div>
                {snippet && ticket.match.field !== "title" && (
                  <p className="line-clamp-2 text-[13px] wrap-anywhere text-muted-foreground">
                    <span className="font-medium text-foreground/70">
                      {FIELD_LABELS[ticket.match.field]}:{" "}
                    </span>
                    {highlightWords(snippet, query)}
                  </p>
                )}
                {stage && (
                  <div className="flex min-w-0">
                    <TicketStageBadge stage={stage} />
                  </div>
                )}
              </Link>
            </li>
          );
        })}
      </ul>
      {hasNextPage && !sparse && (
        <div className="flex items-center gap-2 pt-3 text-xs text-muted-foreground">
          <Button
            variant="outline"
            size="sm"
            disabled={isFetching}
            onClick={() => void fetchNextPage()}
          >
            {isFetching ? <Spinner className="size-3.5" /> : null}
            Search older tickets
          </Button>
          <span>Searched the {scanned} most recently updated tickets.</span>
        </div>
      )}
      {hasNextPage && sparse && isFetching && results.length > 0 && (
        <Spinner className="mt-3 size-3.5" aria-label="Searching older tickets" />
      )}
    </section>
  );
}
