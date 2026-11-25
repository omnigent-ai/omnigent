import { requesterLabel } from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";
import { userColor, userInitials } from "@/lib/userBadge";
import { personDisplayName } from "./personaMentions";

/** A person's first name from their sign-in ID: `ann.lee@example.com` reads `Ann`. */
export function personFirstName(userId: string): string {
  return personDisplayName(userId).split(" ")[0] || userId;
}

/** How a person reads in running text: "you" for the viewer, else their first name. */
export function personLabel(userId: string, viewer: string | null): string {
  return userId === viewer ? "you" : personFirstName(userId);
}

/** The hover text that names a person in full: "Ann Lee · ann.lee@example.com". */
export function personTitle(userId: string): string {
  const name = personDisplayName(userId);
  return name === userId ? userId : `${name} · ${userId}`;
}

/** A person's initials circle, the same one the app shows for people elsewhere. */
export function PersonAvatar({ userId, className }: { userId: string; className?: string }) {
  return (
    <span
      aria-hidden
      data-testid="ticket-person-avatar"
      className={cn(
        "grid size-6 shrink-0 place-items-center rounded-full text-[10px] font-semibold text-white",
        className,
      )}
      style={{ backgroundColor: userColor(userId) }}
    >
      {userInitials(userId)}
    </span>
  );
}

/** A person by first name, with the full name and sign-in ID on hover. */
export function PersonName({
  userId,
  viewer,
  capitalize = false,
  avatar = false,
}: {
  userId: string;
  viewer: string | null;
  /** "You" rather than "you", at the start of a line. */
  capitalize?: boolean;
  /** Lead with a small initials circle, as on board cards. */
  avatar?: boolean;
}) {
  const label = personLabel(userId, viewer);
  return (
    <span
      className="inline-flex min-w-0 items-center gap-1"
      title={personTitle(userId)}
      data-testid="ticket-person-name"
    >
      {avatar && <PersonAvatar userId={userId} className="size-4 text-[8px]" />}
      <span className="truncate">
        {capitalize ? label.charAt(0).toUpperCase() + label.slice(1) : label}
      </span>
    </span>
  );
}

/** ", for Ann" after an agent's name: whose login the agent's session acts for. */
export function ActingFor({ userId, viewer }: { userId: string | null; viewer: string | null }) {
  if (!userId) return null;
  return (
    <span className="text-muted-foreground" title={personTitle(userId)} data-testid="acting-for">
      , for {personLabel(userId, viewer)}
    </span>
  );
}

/** Who asked for a ticket: a person by name, or an outside PR author or project as labelled. */
export function RequesterName({
  requestedBy,
  viewer,
  avatar = false,
}: {
  requestedBy: string;
  viewer: string | null;
  avatar?: boolean;
}) {
  if (requestedBy.startsWith("github:") || requestedBy.startsWith("project:")) {
    return <span className="truncate">{requesterLabel(requestedBy, viewer)}</span>;
  }
  return <PersonName userId={requestedBy} viewer={viewer} avatar={avatar} />;
}
