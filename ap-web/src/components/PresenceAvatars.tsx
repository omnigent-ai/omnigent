import { Avatar, AvatarFallback, AvatarGroupCount } from "@/components/ui/avatar";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { getCurrentAuthorId } from "@/lib/identity";
import { userColor, userInitials } from "@/lib/userBadge";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";

/** Circles shown before the overflow `+N` chip collapses the rest. */
const MAX_VISIBLE_VIEWERS = 3;

/**
 * PresenceAvatars — Google-Docs-style circles for OTHER users currently
 * viewing this session, rendered in the chat header's action row.
 *
 * Self-contained: subscribes to `useChatStore.viewers` (replaced
 * wholesale by each `session.presence` SSE event) and filters out the
 * current user, so ChatHeader's prop surface stays untouched. Renders
 * nothing when the user is alone — which is also the single-user-mode
 * behavior, since the server only tracks distinct human identities.
 *
 * Idle viewers (every tab backgrounded) render dimmed/desaturated with
 * an "(idle)" tooltip suffix, mirroring Google Docs' grey-out.
 */
export function PresenceAvatars() {
  const viewers = useChatStore((s) => s.viewers);
  const self = getCurrentAuthorId();
  const others = viewers.filter((viewer) => viewer.userId !== self);
  if (others.length === 0) return null;
  const visible = others.slice(0, MAX_VISIBLE_VIEWERS);
  const overflow = others.slice(MAX_VISIBLE_VIEWERS);
  return (
    // Spaced row, not an overlapping stack: with at most 3 circles +
    // an overflow chip, full visibility of each face beats the space
    // savings of AvatarGroup's negative-margin pile-up.
    <div className="mr-1 flex items-center gap-1" data-testid="presence-avatars">
      {visible.map((viewer) => (
        <Tooltip key={viewer.userId}>
          <TooltipTrigger asChild>
            <Avatar
              size="sm"
              data-testid={`presence-avatar-${viewer.userId}`}
              className={cn(viewer.idle && "opacity-40 saturate-50")}
            >
              <AvatarFallback
                className="font-medium text-white"
                style={{ backgroundColor: userColor(viewer.userId) }}
              >
                {userInitials(viewer.userId)}
              </AvatarFallback>
            </Avatar>
          </TooltipTrigger>
          <TooltipContent>{viewer.idle ? `${viewer.userId} (idle)` : viewer.userId}</TooltipContent>
        </Tooltip>
      ))}
      {overflow.length > 0 && (
        <Tooltip>
          <TooltipTrigger asChild>
            <AvatarGroupCount data-testid="presence-overflow" className="size-6 text-xs">
              +{overflow.length}
            </AvatarGroupCount>
          </TooltipTrigger>
          <TooltipContent>{overflow.map((viewer) => viewer.userId).join(", ")}</TooltipContent>
        </Tooltip>
      )}
    </div>
  );
}
