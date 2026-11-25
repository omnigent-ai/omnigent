import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PersonaAvatar } from "@/components/PersonaAvatar";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { LEAD_IDENTITY_ID, setProjectRouting, type ProjectIdentity } from "@/lib/projectsApi";
import { TicketError } from "./TicketControls";
import { projectRecordKey, useProjectRecord } from "./useProjectAgentSession";
import { projectIdentitiesKey } from "./useProjectIdentities";

// Radix Select can't use an empty value, so the default choice gets a sentinel.
const DEFAULT_ROUTER = "__default__";

/**
 * Owner-only routing: who receives ticket events, digests and escalations, and
 * whether the project keeps its built-in Lead. Without a choice it is the Lead,
 * or the first persona once the Lead is removed.
 */
export function ProjectRoutingSection({
  projectId,
  personas,
}: {
  projectId: string;
  personas: ProjectIdentity[];
}) {
  const client = useQueryClient();
  const project = useProjectRecord(projectId).data;
  const routing = useMutation({
    mutationFn: (next: { router_identity_id: string | null; lead_removed: boolean }) =>
      setProjectRouting(projectId, next),
    onSuccess: (updated) => {
      client.setQueryData(projectRecordKey(projectId), updated);
      void client.invalidateQueries({ queryKey: ["projects"] });
      void client.invalidateQueries({ queryKey: projectIdentitiesKey(projectId) });
    },
  });
  if (project === undefined || project.shared) return null;
  const leadRemoved = project.lead_removed === true;
  const options = [
    ...(leadRemoved ? [] : [LEAD_IDENTITY_ID]),
    ...personas.map((persona) => persona.id),
  ];
  // A deleted persona's stale choice reads as the default it falls back to.
  const configured =
    project.router_identity_id && options.includes(project.router_identity_id)
      ? project.router_identity_id
      : null;
  const routerName = project.router?.name;
  const fallback = leadRemoved ? (personas[0]?.name ?? "the Lead") : "the Lead";
  const choose = (value: string) =>
    routing.mutate({
      router_identity_id: value === DEFAULT_ROUTER ? null : value,
      lead_removed: leadRemoved,
    });
  const toggleLead = () =>
    routing.mutate({
      router_identity_id: configured === LEAD_IDENTITY_ID ? null : configured,
      lead_removed: !leadRemoved,
    });
  return (
    <section
      aria-label="Routing"
      className="flex flex-col gap-2 border-t pt-3 text-sm"
      data-testid="project-routing"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground">Ticket events and escalations go to</span>
        <Select
          value={configured ?? DEFAULT_ROUTER}
          disabled={routing.isPending}
          onValueChange={choose}
        >
          <SelectTrigger aria-label="Router team member" className="w-48" size="sm">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            <SelectItem value={DEFAULT_ROUTER}>Default ({fallback})</SelectItem>
            {!leadRemoved && (
              <SelectItem value={LEAD_IDENTITY_ID}>
                <PersonaAvatar name="Lead" size="xs" />
                Lead
              </SelectItem>
            )}
            {personas.map((persona) => (
              <SelectItem key={persona.id} value={persona.id}>
                <PersonaAvatar name={persona.name} size="xs" />
                {persona.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="ml-auto"
          data-testid="project-toggle-lead"
          loading={routing.isPending}
          disabled={!leadRemoved && personas.length === 0}
          title={
            !leadRemoved && personas.length === 0
              ? "Add another team member before removing the Lead"
              : undefined
          }
          onClick={toggleLead}
        >
          {leadRemoved ? "Restore the Lead" : "Remove the Lead"}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        {routerName ? `Now: ${routerName}. ` : ""}
        {leadRemoved
          ? "The Lead is removed. Messages to it reach the router instead."
          : "The Lead coordinates the board unless you pick another team member."}
      </p>
      <TicketError error={routing.error} />
    </section>
  );
}
