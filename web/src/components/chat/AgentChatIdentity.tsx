import { useId, useState } from "react";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import { PersonaAvatar } from "@/components/PersonaAvatar";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import type { Agent } from "@/hooks/useAgents";
import { sessionAgentIdentity } from "@/lib/sessionAgentIdentity";
import { cn } from "@/lib/utils";

export function AgentChatIdentity({
  agent,
  persona = null,
  compact = false,
}: {
  agent: Pick<Agent, "name" | "harness" | "description">;
  /** The team member this session works as, e.g. "Merger"; shown instead of the harness. */
  persona?: string | null;
  compact?: boolean;
}) {
  const identity = sessionAgentIdentity({ agentName: agent.name, harness: agent.harness });
  const description = agent.description?.trim();
  const [open, setOpen] = useState(false);
  const cardId = useId();
  if (!identity) return null;
  const mark = persona ? (
    <span aria-hidden className="relative inline-flex">
      <PersonaAvatar
        name={persona}
        className={cn(
          "shadow-sm ring-muted/20",
          compact ? "size-12 text-base ring-4" : "size-20 text-2xl ring-8",
        )}
      />
      <span
        className={cn(
          "absolute -right-0.5 -bottom-0.5 grid place-items-center rounded-full border border-border/70 bg-background shadow-xs",
          compact ? "size-5" : "size-7",
        )}
      >
        <ComposerAgentIcon agent={identity} className={compact ? "size-3" : "size-4"} />
      </span>
    </span>
  ) : (
    <span
      aria-hidden
      className={cn(
        "flex items-center justify-center rounded-full border border-border/70 bg-background bg-radial from-background via-muted/30 to-muted shadow-sm ring-muted/20 transition-shadow group-hover:ring-muted/35",
        compact ? "size-12 ring-4" : "size-20 ring-8",
      )}
    >
      <ComposerAgentIcon agent={identity} className={compact ? "size-7" : "size-12"} />
    </span>
  );
  if (!description)
    return (
      <span
        role="img"
        aria-label={persona ? `${persona} (${identity.displayName})` : identity.displayName}
        className={compact ? "p-1" : "p-2"}
      >
        {mark}
      </span>
    );
  const label = `About ${persona ?? identity.displayName}`;
  return (
    <HoverCard open={open} onOpenChange={setOpen} openDelay={150}>
      <HoverCardTrigger asChild>
        <button
          type="button"
          aria-label={label}
          aria-expanded={open}
          aria-controls={open ? cardId : undefined}
          aria-describedby={open ? cardId : undefined}
          onClick={() => setOpen(true)}
          onKeyDown={(event) => {
            if (event.key === "Escape") setOpen(false);
          }}
          className={cn(
            "group inline-flex items-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring",
            compact ? "p-1" : "p-2",
          )}
        >
          {mark}
        </button>
      </HoverCardTrigger>
      <HoverCardContent id={cardId} role="region" aria-label={label} className="w-72">
        <p className="text-sm leading-relaxed whitespace-pre-wrap">{description}</p>
      </HoverCardContent>
    </HoverCard>
  );
}
