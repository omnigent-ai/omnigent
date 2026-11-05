import { useQuery } from "@tanstack/react-query";
import { MessagesSquareIcon } from "lucide-react";
import { useState } from "react";
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

/** Discover saved side chats without changing this device's open tabs. */
export function SideChatPicker({
  conversationId,
  onOpen,
}: {
  conversationId: string;
  onOpen: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [after, setAfter] = useState<string | null>(null);
  const { data, isError, isFetching } = useQuery({
    queryKey: ["conversation", conversationId, "side_chats", after],
    queryFn: async (): Promise<SideChatPage> => {
      const params = new URLSearchParams({ limit: "20", ...(after ? { after } : {}) });
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(conversationId)}/side_chats?${params}`,
      );
      // Older servers keep using local tabs.
      if (response.status === 404) return { data: [], has_more: false, last_id: null };
      if (!response.ok) throw new Error("Could not load side chats");
      return response.json();
    },
    enabled: open,
    retry: false,
  });

  return (
    <DropdownMenu
      open={open}
      onOpenChange={(value) => {
        setOpen(value);
        if (!value) setAfter(null);
      }}
    >
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Browse side chats">
          <MessagesSquareIcon className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto">
        <SuppressBrowserView />
        {data?.data.map((chat) => (
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
        {(!data?.data.length || isError) && (
          <DropdownMenuItem disabled>
            {isFetching
              ? "Loading…"
              : isError
                ? "Could not load side chats"
                : "No saved side chats"}
          </DropdownMenuItem>
        )}
        {data?.has_more && data.last_id && (
          <DropdownMenuItem
            onSelect={(event) => {
              event.preventDefault();
              setAfter(data.last_id);
            }}
          >
            Next 20
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
