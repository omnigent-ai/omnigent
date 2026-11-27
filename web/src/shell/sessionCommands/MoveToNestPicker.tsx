// The keyboard way to move a session into a nest, or out of its nest: a picker of
// the viewer's nests, searching sessions the sidebar hasn't loaded.

import { GitBranchIcon, MinusIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { QuickPickDialog, type QuickPickItem } from "@/components/ui/quick-pick";
import { type Conversation, useSessionSearch } from "@/hooks/useConversations";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useOptionalLoadedConversations } from "@/hooks/useSidebarData";
import { useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";

import { nestDestinations, nestLabel } from "../nestDestinations";

/** Moving the session under `destination`, or out of its nest when it's `null`. */
interface NestChoice {
  destination: Conversation | null;
}

/** A failed search shows its own status instead, so an empty list never reads as "no nests". */
function emptyMessageFor(searchFailed: boolean, fetching: boolean): string | null {
  if (searchFailed) return null;
  return fetching ? "Searching…" : "No other nests yet.";
}

export function MoveToNestPicker({
  conversation,
  onPick,
  onClose,
}: {
  conversation: Conversation;
  /** Called with the chosen nest's representative, or `null` to take the session out of its nest. */
  onPick: (destination: Conversation | null) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const trimmed = query.trim();
  const debouncedQuery = useDebouncedValue(trimmed, 300);
  const serverInfo = useServerInfo();
  const canJoin = serverInfo !== "loading" && serverInfo.sibling_sessions === true;
  const viewerId = useViewerId();
  const loadedPages = useOptionalLoadedConversations()?.pages ?? [];
  const search = useSessionSearch(debouncedQuery, "title", {
    enabled: canJoin,
    includeArchived: false,
    limit: 20,
  });
  // Search hits only count once they belong to the query on screen.
  const searchHits =
    debouncedQuery === trimmed ? (search.data?.pages.flatMap((page) => page.data) ?? []) : [];
  const destinations = canJoin
    ? nestDestinations({
        conversation,
        loaded: loadedPages.flatMap((page) => page.data),
        searchHits,
        search: query,
        viewerId,
      })
    : [];
  const removal: QuickPickItem<NestChoice>[] = conversation.family
    ? [
        {
          id: "remove",
          label: "Remove from nest",
          icon: <MinusIcon aria-hidden className="size-4 text-muted-foreground" />,
          rememberInHistory: false,
          value: { destination: null },
        },
      ]
    : [];
  const nests: QuickPickItem<NestChoice>[] = destinations.map((row) => ({
    id: `nest:${row.family!.id}`,
    label: nestLabel(row),
    icon: <GitBranchIcon aria-hidden className="size-4 text-muted-foreground" />,
    rememberInHistory: false,
    value: { destination: row },
  }));
  // As the row menu's nest search does: later pages on request, and a failure with Retry.
  const searching = canJoin && trimmed !== "";
  const searchFailed = searching && search.isError;
  const status = searchFailed ? (
    <div className="flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground">
      <p role="status">Couldn't search for nests.</p>
      <Button variant="ghost" size="xs" onClick={() => void search.refetch()}>
        Retry
      </Button>
    </div>
  ) : undefined;
  const showMore = searching && !searchFailed && search.hasNextPage && (
    <Button
      variant="ghost"
      className="w-full"
      disabled={search.isFetching}
      onClick={() => void search.fetchNextPage()}
    >
      Show more
    </Button>
  );
  return (
    <QuickPickDialog<NestChoice>
      // Steps with Ctrl-N/Ctrl-P, like ⌘K.
      stepsWithCtrlNP
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      label={conversation.family ? "Move session to another nest" : "Move session to a nest"}
      placeholder="Find a nest…"
      emptyMessage={emptyMessageFor(searchFailed, searching && search.isFetching)}
      status={status}
      listEnd={showMore}
      acceptLabel="Move"
      query={query}
      onQueryChange={setQuery}
      segments={[
        { sourceId: "remove", ranking: "client", items: removal },
        // Already matched to the query, including search hits on a member's title.
        { sourceId: "nests", ranking: "provider", items: nests },
      ]}
      onAccept={({ value }) => {
        onPick(value.destination);
        return undefined;
      }}
    />
  );
}
