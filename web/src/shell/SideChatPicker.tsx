import { useInfiniteQuery } from "@tanstack/react-query";
import { MessagesSquareIcon } from "lucide-react";
import { SuppressBrowserView } from "@/hooks/useSuppressBrowserView";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { authenticatedFetch } from "@/lib/identity";
import { setSessionParent } from "@/lib/sessionHost";

interface SideChatPage {
  data: { id: string; title: string | null; created_at: number }[];
  has_more: boolean;
  last_id: string | null;
}

interface SideChatPickerProps {
  conversationId: string;
  onOpen: (id: string) => void;
}

function SideChatResults({ conversationId, onOpen }: SideChatPickerProps) {
  const { data, isError, isFetching, hasNextPage, isFetchNextPageError, fetchNextPage, refetch } =
    useInfiniteQuery({
      queryKey: ["conversation", conversationId, "side_chats"],
      queryFn: async ({ pageParam, signal }): Promise<SideChatPage> => {
        const params = new URLSearchParams({
          limit: "20",
          ...(pageParam ? { after: pageParam } : {}),
        });
        const response = await authenticatedFetch(
          `/v1/sessions/${encodeURIComponent(conversationId)}/side_chats?${params}`,
          { signal },
        );
        // Older servers keep using local tabs.
        if (response.status === 404) return { data: [], has_more: false, last_id: null };
        if (!response.ok) throw new Error("Could not load side chats");
        return response.json();
      },
      initialPageParam: null as string | null,
      getNextPageParam: (page) => (page.has_more ? page.last_id : undefined),
      // The menu mounts on demand; reopening starts with just the first page.
      gcTime: 0,
      refetchOnWindowFocus: false,
      retry: false,
    });

  const chats = data?.pages.flatMap((page) => page.data) ?? [];
  const emptyLabel = isError ? "Could not load side chats" : "No saved side chats";
  return (
    <>
      <SuppressBrowserView />
      {chats.map((chat) => (
        <DropdownMenuItem
          key={chat.id}
          onSelect={() => {
            setSessionParent(chat.id, conversationId);
            onOpen(chat.id);
          }}
        >
          <span className="max-w-64 truncate">{chat.title || "Side chat"}</span>
          <span className="ml-auto pl-2 text-xs text-muted-foreground">
            {new Date(chat.created_at * 1000).toLocaleString()}
          </span>
        </DropdownMenuItem>
      ))}
      {(!chats.length || isError) && (
        <DropdownMenuItem disabled>{isFetching ? "Loading…" : emptyLabel}</DropdownMenuItem>
      )}
      {(hasNextPage || isError) && (
        <DropdownMenuItem
          disabled={isFetching}
          onSelect={(event) => {
            event.preventDefault();
            void (isError && !isFetchNextPageError ? refetch() : fetchNextPage());
          }}
        >
          {isError ? "Retry" : "Load more"}
        </DropdownMenuItem>
      )}
    </>
  );
}

/** Discover saved side chats without changing this device's open tabs. */
export function SideChatPicker(props: SideChatPickerProps) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Browse side chats">
          <MessagesSquareIcon className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto">
        <SideChatResults {...props} />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
