import { useId, useState } from "react";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import type { Agent } from "@/hooks/useAgents";
import { sessionAgentIdentity } from "@/lib/sessionAgentIdentity";
import { projectAgentDescription } from "./projectAgentDefaults";

export function ProjectAgentIdentity({
  agent,
}: {
  agent: Pick<Agent, "name" | "harness" | "description">;
}) {
  const identity = sessionAgentIdentity({ agentName: agent.name, harness: agent.harness });
  const description = projectAgentDescription(agent);
  const [open, setOpen] = useState(false);
  const cardId = useId();
  if (!identity) return null;
  const mark = (
    <span
      aria-hidden
      className="flex size-20 items-center justify-center rounded-full border border-border/70 bg-radial from-background via-muted/30 to-muted shadow-sm ring-8 ring-muted/20 transition-shadow group-hover:ring-muted/35"
    >
      <ComposerAgentIcon agent={identity} className="size-12" />
    </span>
  );
  if (!description)
    return (
      <span role="img" aria-label={identity.displayName} className="p-2">
        {mark}
      </span>
    );
  const label = `About ${identity.displayName}`;
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
          className="group inline-flex items-center rounded-full p-2 outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
