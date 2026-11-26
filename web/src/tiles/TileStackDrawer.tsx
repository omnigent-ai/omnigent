// The Stack drawer: parked and quiet agents, each with Unstack and Open.

import { ArrowUpFromLineIcon, ExternalLinkIcon, LayersIcon } from "lucide-react";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { Button } from "@/components/ui/button";
import type { Conversation } from "@/hooks/useConversations";
import { getSessionState } from "@/hooks/useSessionState";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { projectOf } from "./tileFilters";

interface TileStackDrawerProps {
  stacked: readonly Conversation[];
  quietIds: ReadonlySet<string>;
  onUnstack: (conversation: Conversation) => void;
  onOpenSingle: (sessionId: string) => void;
}

export function TileStackDrawer({
  stacked,
  quietIds,
  onUnstack,
  onOpenSingle,
}: TileStackDrawerProps) {
  return (
    <section
      aria-label="Stack"
      data-testid="tiles-stack"
      className="shrink-0 border-t bg-muted/30 px-4 py-2"
    >
      {stacked.length === 0 ? (
        <p className="py-1 text-xs text-muted-foreground">
          The Stack is empty. Use <LayersIcon aria-hidden className="inline size-3" /> on a tile to
          park it here; agents idle for a day are stacked automatically.
        </p>
      ) : (
        <ul className="flex max-h-48 flex-col gap-1 overflow-y-auto">
          {stacked.map((conversation) => {
            const state = getSessionState(conversation);
            const title = conversation.title || conversationDisplayLabel(conversation);
            const quiet = quietIds.has(conversation.id);
            return (
              <li
                key={conversation.id}
                data-testid="tiles-stack-row"
                data-session-id={conversation.id}
                className="flex items-center gap-2 rounded-md px-2 py-1 text-xs hover:bg-accent"
              >
                {state ? (
                  <SessionStateBadge state={state} />
                ) : (
                  <span aria-hidden className="size-2 shrink-0 rounded-full bg-muted-foreground" />
                )}
                <span className="min-w-0 truncate font-medium" title={title}>
                  {title}
                </span>
                <span className="min-w-0 flex-1 truncate text-muted-foreground">
                  {[projectOf(conversation), quiet ? "quiet" : "parked"]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
                <Button
                  variant="ghost"
                  size="xs"
                  className="gap-1"
                  onClick={() => onUnstack(conversation)}
                >
                  <ArrowUpFromLineIcon className="size-3.5" />
                  Unstack
                </Button>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Open ${title} in single agent view`}
                  onClick={() => onOpenSingle(conversation.id)}
                >
                  <ExternalLinkIcon className="size-3.5" />
                </Button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
