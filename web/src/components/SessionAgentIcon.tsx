import { useId, useState } from "react";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import type { SessionAgentIdentity } from "@/lib/sessionAgentIdentity";
import { cn } from "@/lib/utils";

/** Compact sender identity; the session link remains a separate control. */
export function SessionAgentIcon({
  agent,
  className,
  iconClassName,
}: {
  agent: SessionAgentIdentity;
  className?: string;
  iconClassName?: string;
}) {
  const [open, setOpen] = useState(false);
  const cardId = useId();
  return (
    <HoverCard open={open} onOpenChange={setOpen} openDelay={150}>
      <HoverCardTrigger asChild>
        <button
          type="button"
          aria-label={agent.displayName}
          aria-expanded={open}
          aria-controls={open ? cardId : undefined}
          aria-describedby={open ? cardId : undefined}
          onClick={() => setOpen(true)}
          onKeyDown={(event) => {
            if (event.key === "Escape") setOpen(false);
          }}
          className={cn(
            "inline-flex shrink-0 items-center justify-center rounded-sm p-0.5 outline-none focus-visible:ring-2 focus-visible:ring-ring",
            className,
          )}
        >
          <ComposerAgentIcon agent={agent} className={iconClassName} />
        </button>
      </HoverCardTrigger>
      <HoverCardContent id={cardId} className="w-auto max-w-64 px-2.5 py-1.5 text-sm font-medium">
        {agent.displayName}
      </HoverCardContent>
    </HoverCard>
  );
}
