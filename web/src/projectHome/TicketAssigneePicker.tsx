import { PersonaAvatar } from "@/components/PersonaAvatar";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { TicketLabel, TicketChanges } from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";
import { useProjectIdentities } from "./useProjectIdentities";

export const ASSIGNEE_LABEL_KEY = "assignee";

/** The ticket's `assignee:` value, or `null` when it has none. */
export function ticketAssignee(labels: TicketLabel[]): string | null {
  return labels.find((label) => label.key === ASSIGNEE_LABEL_KEY)?.value?.trim() || null;
}

/** The label write that assigns *name*; `null` leaves the ticket to the Lead. */
export function assigneeChanges(
  name: string | null,
): Pick<TicketChanges, "add_labels" | "remove_labels"> {
  return name
    ? { add_labels: [{ key: ASSIGNEE_LABEL_KEY, value: name }] }
    : { remove_labels: [ASSIGNEE_LABEL_KEY] };
}

/**
 * Picks who takes a ticket from the project's roles, as avatars. The Lead is the
 * default: choosing it clears the assignee, since the Lead routes unassigned work.
 */
export function TicketAssigneePicker({
  projectId,
  value,
  onChange,
  disabled = false,
}: {
  projectId: string | null;
  value: string | null;
  onChange: (name: string | null) => void;
  disabled?: boolean;
}) {
  const roles = useProjectIdentities(projectId).data ?? [];
  if (roles.length === 0) return null;
  const ordered = [...roles].sort((a, b) => Number(b.builtin) - Number(a.builtin));
  const chosen = value?.toLowerCase() ?? null;
  // An assignee that isn't a current role still shows, so it can be cleared.
  const unknown =
    chosen && !roles.some((role) => role.name.toLowerCase() === chosen) ? value : null;
  return (
    <div className="flex items-center gap-2 text-sm">
      <span className="text-muted-foreground" id={`assignee-${projectId}`}>
        Assignee
      </span>
      <div
        role="radiogroup"
        aria-labelledby={`assignee-${projectId}`}
        className="flex flex-wrap items-center gap-1"
      >
        {ordered.map((role) => {
          const checked = role.builtin
            ? chosen === null || chosen === role.name.toLowerCase()
            : chosen === role.name.toLowerCase();
          const label = role.builtin && chosen === null ? `${role.name} (default)` : role.name;
          return (
            <Tooltip key={role.id}>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  role="radio"
                  aria-checked={checked}
                  aria-label={label}
                  data-testid="ticket-assignee-option"
                  disabled={disabled}
                  onClick={() => {
                    if (!checked) onChange(role.builtin ? null : role.name);
                  }}
                  className={cn(
                    "rounded-full p-0.5 ring-offset-background transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50",
                    checked ? "ring-2 ring-primary" : "opacity-60 hover:opacity-100",
                  )}
                >
                  <PersonaAvatar name={role.name} size="sm" />
                </button>
              </TooltipTrigger>
              <TooltipContent side="bottom">{label}</TooltipContent>
            </Tooltip>
          );
        })}
        {unknown && (
          <span className="ml-1 text-muted-foreground" data-testid="ticket-assignee-unknown">
            {unknown}
          </span>
        )}
      </div>
    </div>
  );
}
