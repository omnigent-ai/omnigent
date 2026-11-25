import { personName } from "@/components/chat/TicketUpdateCards";
import type { TicketWaitingOn } from "@/lib/projectTicketsApi";

/** Whom a ticket's needs-human ask waits on, from the viewer's side. */
export type NeedsWhom =
  | { kind: "you" }
  | { kind: "other"; label: string; title: string }
  // The ask was answered: no tag.
  | null
  // Not known for this ticket (an older server): the generic tag.
  | undefined;

export function needsWhom(
  waitingOn: TicketWaitingOn | null | undefined,
  viewer: string | null,
): NeedsWhom {
  if (waitingOn === undefined) return undefined;
  if (waitingOn === null) return null;
  const responders = waitingOn.responders.filter((id) => id !== waitingOn.raised_by_user_id);
  if (viewer !== null && responders.includes(viewer)) return { kind: "you" };
  const names = responders.map((id) => personName(id));
  const first = names[0]?.split(" ")[0];
  const label = !first
    ? "Needs response"
    : names.length === 1
      ? `Needs ${first}`
      : waitingOn.reason === "product-ok"
        ? "Needs product approval"
        : `Needs ${first} +${names.length - 1}`;
  return { kind: "other", label, title: names.length ? `Waiting on ${names.join(", ")}` : label };
}
