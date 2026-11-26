// A session search's rows, for any list of them such as ⌘K's: one per session with
// its project or group, where the query matched, and a paging row under each server group.

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { occurrenceRanges, type QuickPickItem } from "@/components/ui/quick-pick";

import { PaletteSessionContext } from "./PaletteSessionContext";
import type { PaletteTarget } from "./paletteTarget";
import type { PaletteSession } from "./usePaletteSessionSearch";

export type SessionRowTarget = Extract<PaletteTarget, { kind: "session" }>;
export type MoreRowTarget = Extract<PaletteTarget, { kind: "more" }>;

/** Session rows carry no icon of their own; this keeps their text aligned with other rows. */
export const SESSION_ICON = <span aria-hidden className="size-4" />;

/** A server search group's paging state, as `useSessionSearch` reports it. */
export interface SessionSearchGroupPaging {
  hasNextPage: boolean;
  isFetchNextPageError: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => unknown;
}

/**
 * One session's row. `labelQuery` marks the name, `snippetQuery` the message excerpt the
 * server returned; the sidebar's own name matches bring their ranges with them.
 */
export function paletteSessionItem(
  session: PaletteSession,
  group: string,
  { labelQuery, snippetQuery }: { labelQuery: string; snippetQuery: string },
): QuickPickItem<SessionRowTarget> {
  return {
    id: session.id,
    label: session.label,
    context: <PaletteSessionContext project={session.project} parent={session.parent} />,
    description: session.snippet ?? undefined,
    group,
    icon: SESSION_ICON,
    highlights: {
      label: session.labelRanges ?? occurrenceRanges(labelQuery, session.label),
      description: session.snippet ? occurrenceRanges(snippetQuery, session.snippet) : [],
    },
    accessory: (
      <>
        {session.archived && (
          <Badge variant="outline" className="text-muted-foreground">
            Archived
          </Badge>
        )}
        <span className="text-sm text-muted-foreground">{session.agent}</span>
      </>
    ),
    // Sessions list by their own activity; the history holds commands only.
    rememberInHistory: false,
    value: { kind: "session", id: session.id },
  };
}

/**
 * A group's "Show more" row, under that group's rows while the server has another page. A group
 * with no rows, its page's matches all listed above, offers none unless `keepWithoutRows`.
 */
export function paletteShowMoreItems(
  scope: "title" | "content",
  group: SessionSearchGroupPaging,
  rows: readonly PaletteSession[],
  groupLabel: string,
  { keepWithoutRows = false }: { keepWithoutRows?: boolean } = {},
): QuickPickItem<MoreRowTarget>[] {
  if (!group.hasNextPage || group.isFetchNextPageError) return [];
  if (rows.length === 0 && !keepWithoutRows) return [];
  return [
    {
      id: `more:${scope}`,
      rememberInHistory: false,
      label: group.isFetchingNextPage ? "Loading…" : "Show more",
      group: groupLabel,
      icon: SESSION_ICON,
      disabledReason: group.isFetchingNextPage ? "Loading more sessions" : undefined,
      value: {
        kind: "more",
        load: () => void group.fetchNextPage(),
        lastRowId: rows.at(-1)?.id,
      },
    },
  ];
}

/** The parts of a session search its rows are built from, as `usePaletteSessionSearch` returns. */
export interface SessionSearchRows {
  visibleSessions: readonly PaletteSession[];
  contentSessions: readonly PaletteSession[];
  /** Whether the server's matches answer the query typed now; paging waits for that. */
  showingServerMatches: boolean;
  titleSearch: SessionSearchGroupPaging;
  contentSearch: SessionSearchGroupPaging;
}

/**
 * A query's rows: the name matches, then the message matches, each with its paging row. Paging
 * shows only once the server answered the query typed now, so a page of an earlier query is
 * never offered. ⌘K hides a group whose matches were all listed above, Show more included;
 * `keepPagingWithoutRows` keeps its Show more, so the pages after it stay reachable.
 */
export function sessionSearchItems(
  search: SessionSearchRows,
  groups: { names: string; messages: string },
  highlight: { labelQuery: string; snippetQuery: string },
  { keepPagingWithoutRows = false }: { keepPagingWithoutRows?: boolean } = {},
): {
  names: QuickPickItem<SessionRowTarget | MoreRowTarget>[];
  messages: QuickPickItem<SessionRowTarget | MoreRowTarget>[];
} {
  const paging = search.showingServerMatches;
  const keep = { keepWithoutRows: keepPagingWithoutRows };
  return {
    names: [
      ...search.visibleSessions.map((session) =>
        paletteSessionItem(session, groups.names, highlight),
      ),
      ...(paging
        ? paletteShowMoreItems(
            "title",
            search.titleSearch,
            search.visibleSessions,
            groups.names,
            keep,
          )
        : []),
    ],
    messages: [
      ...search.contentSessions.map((session) =>
        paletteSessionItem(session, groups.messages, highlight),
      ),
      ...(paging
        ? paletteShowMoreItems(
            "content",
            search.contentSearch,
            search.contentSessions,
            groups.messages,
            keep,
          )
        : []),
    ],
  };
}

/** Where Show more left the selection: its group's last row, and the group itself. */
export interface ShowMoreParking {
  lastRowId: string | undefined;
  group: string | undefined;
}

/**
 * The row to keep active after Show more: the group's last row, so the next ArrowDown reaches
 * the rows it loads. Once that row is gone the list picks its first enabled row, as for any
 * missing row. A group that had no rows of its own, its page's matches all listed above, parks
 * on its first row instead: Loading… while the page loads, then the first row it brings.
 */
export function parkedRowId(
  parking: ShowMoreParking,
  items: readonly QuickPickItem<unknown>[],
): string | null {
  if (parking.lastRowId !== undefined) return parking.lastRowId;
  return items.find((item) => item.group === parking.group)?.id ?? null;
}

/**
 * Show more for a list of session rows: `accept` loads the next page and parks the selection,
 * `activeId` is the parked row for the list's `activeId`, and `clear` ends it once the user moves.
 * Any change to the query ends it too, and so does closing the list, so a query typed again
 * later starts from its top row.
 */
export function useShowMoreParking(
  items: readonly QuickPickItem<unknown>[],
  { query, open }: { query: string; open: boolean },
) {
  const [parked, setParked] = useState<{ parking: ShowMoreParking; query: string } | null>(null);
  const current = parked !== null && open && parked.query === query;
  if (parked !== null && !current) setParked(null);
  return {
    activeId: current ? parkedRowId(parked.parking, items) : null,
    accept: (row: QuickPickItem<MoreRowTarget>) => {
      setParked({ parking: { lastRowId: row.value.lastRowId, group: row.group }, query });
      row.value.load();
    },
    clear: () => setParked(null),
  };
}
