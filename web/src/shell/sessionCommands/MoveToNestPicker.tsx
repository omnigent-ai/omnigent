// The keyboard way to move a session into a nest, or out of its nest: a picker of
// the viewer's nests, searching sessions the sidebar hasn't loaded.

import { GitBranchIcon, MinusIcon } from "lucide-react";
import { useState } from "react";

import { QuickPickDialog, type QuickPickItem } from "@/components/ui/quick-pick";
import { type Conversation, useSessionSearch } from "@/hooks/useConversations";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useOptionalLoadedConversations } from "@/hooks/useSidebarData";
import { useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";

import { conversationDisplayLabel, isOwnedByViewer } from "../sidebarNav";

/** Moving the session under `destination`, or out of its nest when it's `null`. */
interface NestChoice {
  destination: Conversation | null;
}

function nestLabel(row: Conversation): string {
  return row.family?.representative_title ?? conversationDisplayLabel(row);
}

/** One row per nest the viewer owns, other than the session's own. */
function nestDestinations(
  candidates: readonly Conversation[],
  conversation: Conversation,
  viewerId: string | null,
): Conversation[] {
  const byFamily = new Map<string, Conversation>();
  for (const row of candidates) {
    if (!row.family || row.archived || row.id === conversation.id) continue;
    if (row.family.id === conversation.family?.id || !isOwnedByViewer(row, viewerId)) continue;
    if (!byFamily.has(row.family.id)) byFamily.set(row.family.id, row);
  }
  return [...byFamily.values()];
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
    ? nestDestinations(
        [...loadedPages.flatMap((page) => page.data), ...searchHits],
        conversation,
        viewerId,
      )
    : [];
  const items: QuickPickItem<NestChoice>[] = [
    ...(conversation.family
      ? [
          {
            id: "remove",
            label: "Remove from nest",
            icon: <MinusIcon aria-hidden className="size-4 text-muted-foreground" />,
            rememberInHistory: false,
            value: { destination: null },
          },
        ]
      : []),
    ...destinations.map((row) => ({
      id: `nest:${row.family!.id}`,
      label: nestLabel(row),
      icon: <GitBranchIcon aria-hidden className="size-4 text-muted-foreground" />,
      rememberInHistory: false,
      value: { destination: row },
    })),
  ];
  return (
    <QuickPickDialog<NestChoice>
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      label={conversation.family ? "Move session to another nest" : "Move session to a nest"}
      placeholder="Find a nest…"
      emptyMessage="No other nests yet."
      acceptLabel="Move"
      query={query}
      onQueryChange={setQuery}
      segments={[{ sourceId: "nests", ranking: "client", items }]}
      onAccept={({ value }) => {
        onPick(value.destination);
        return undefined;
      }}
    />
  );
}
