// The keyboard way to move a session into a nest, or out of its nest: a picker of
// the viewer's nests, searching sessions the sidebar hasn't loaded.

import { GitBranchIcon, MinusIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { QuickPickDialog, type QuickPickItem } from "@/components/ui/quick-pick";
import type { Conversation } from "@/hooks/useConversations";
import { useOptionalLoadedConversations } from "@/hooks/useSidebarData";
import { useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";

import { nestLabel } from "../nestDestinations";
import { useNestDestinationSearch } from "../useNestDestinationSearch";

/** Moving the session under `destination`, or out of its nest when it's `null`. */
interface NestChoice {
  destination: Conversation | null;
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
  const serverInfo = useServerInfo();
  const canJoin = serverInfo !== "loading" && serverInfo.sibling_sessions === true;
  const viewerId = useViewerId();
  const loadedPages = useOptionalLoadedConversations()?.pages ?? [];
  const search = useNestDestinationSearch({
    conversation,
    loaded: loadedPages.flatMap((page) => page.data),
    search: query,
    enabled: canJoin,
    viewerId,
  });
  const { destinations } = search;
  // A failed search says so and offers Retry, in place of an empty list's message.
  const status = search.failed ? (
    <p className="px-3 py-2 text-xs text-muted-foreground">
      <span role="status">Couldn't search your nests.</span>
      <button
        type="button"
        className="ml-2 underline"
        disabled={search.searching}
        onClick={search.retry}
      >
        Retry
      </button>
    </p>
  ) : undefined;
  // A later page can hold nests the first one's sessions didn't.
  const showMore = search.hasMore && (
    <Button
      variant="ghost"
      className="w-full"
      disabled={search.searching}
      onClick={search.showMore}
    >
      Show more nests
    </Button>
  );
  const emptyMessage = search.failed
    ? null
    : search.searching || !search.settled
      ? "Searching…"
      : "No other nests yet.";
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
  return (
    <QuickPickDialog<NestChoice>
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      label={conversation.family ? "Move session to another nest" : "Move session to a nest"}
      placeholder="Find a nest…"
      emptyMessage={emptyMessage}
      status={status}
      listEnd={showMore || undefined}
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
