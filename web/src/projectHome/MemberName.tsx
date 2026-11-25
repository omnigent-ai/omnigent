import { InfoIcon } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { personDisplayName } from "./personaMentions";
import { PersonAvatar } from "./TicketActor";

/** What the pooled agents row covers, for people who don't know the internals. */
export const OTHER_AGENTS_EXPLANATION =
  "Agent sessions that aren't the Lead or a team member, such as workers the Lead starts to handle a ticket.";

/** A person's readable name from their sign-in, e.g. "alice.smith@example.com" → "Alice Smith". */
export function memberDisplayName(member: {
  kind: string;
  name: string;
  user_id?: string | null;
}): string {
  return member.kind === "human" && member.user_id
    ? personDisplayName(member.user_id)
    : member.name;
}

/** A Summary member: a person with their initials and email on hover, or an agent row. */
export function MemberName({
  member,
}: {
  member: { kind: string; name: string; user_id?: string | null };
}) {
  if (member.kind === "human" && member.user_id) {
    return (
      <span className="flex min-w-0 items-center gap-2" title={member.user_id}>
        <PersonAvatar userId={member.user_id} className="size-5 text-[9px]" />
        <span className="wrap-anywhere">{personDisplayName(member.user_id)}</span>
      </span>
    );
  }
  if (member.kind === "agents") {
    return (
      <span className="flex min-w-0 items-center gap-1.5">
        <span className="wrap-anywhere">{member.name}</span>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              className="rounded-sm text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              aria-label={`What "${member.name}" means`}
            >
              <InfoIcon aria-hidden className="size-3.5" />
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-64">{OTHER_AGENTS_EXPLANATION}</TooltipContent>
        </Tooltip>
      </span>
    );
  }
  return <span className="wrap-anywhere">{member.name}</span>;
}
