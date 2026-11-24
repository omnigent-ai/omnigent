import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { UsersIcon } from "lucide-react";
import { PersonaAvatar } from "@/components/PersonaAvatar";
import { RunningDot } from "@/components/RunningDot";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { openProjectIdentitySession, type ProjectIdentity } from "@/lib/projectsApi";
import { useNavigate } from "@/lib/routing";
import { ProjectTeamDialog } from "./ProjectTeamDialog";
import { projectIdentitiesKey, useProjectIdentities } from "./useProjectIdentities";

/** Personas shown in the header; the rest open from a "+N" menu so the bar never overflows. */
export const VISIBLE_ROSTER_LIMIT = 4;
/** Phones fit only the menu beside the other icon buttons. */
export const MOBILE_VISIBLE_ROSTER_LIMIT = 0;

/**
 * The project's owner-defined identities beside the Lead button. Each
 * opens its one active session (single-tracked) or starts it when it has none.
 */
export function ProjectIdentityRoster({
  projectId,
  projectName,
}: {
  projectId: string | null;
  projectName: string;
}) {
  const isMobile = useIsMobileViewport();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const identities = useProjectIdentities(projectId);
  const open = useMutation({
    mutationFn: (identity: ProjectIdentity) =>
      openProjectIdentitySession(projectId as string, identity.id, identity.active_session_id),
    onSuccess: ({ session_id }) => {
      void client.invalidateQueries({ queryKey: projectIdentitiesKey(projectId) });
      navigate(`/c/${encodeURIComponent(session_id)}`);
    },
    onError: () => void client.invalidateQueries({ queryKey: projectIdentitiesKey(projectId) }),
  });
  if (projectId === null) return null;
  const roles = (identities.data ?? []).filter((identity) => !identity.builtin);
  // Keep working personas in view; otherwise the team's own order.
  const ordered = [
    ...roles.filter((identity) => identity.active_session_state === "busy"),
    ...roles.filter((identity) => identity.active_session_state !== "busy"),
  ];
  const limit = isMobile ? MOBILE_VISIBLE_ROSTER_LIMIT : VISIBLE_ROSTER_LIMIT;
  const visible = ordered.slice(0, limit);
  const hidden = ordered.slice(limit);
  return (
    <div
      className="flex items-center gap-1"
      role="group"
      aria-label="Project identities"
      data-testid="project-identity-roster"
    >
      {visible.map((identity) => {
        const busy = identity.active_session_state === "busy";
        const pending = open.isPending && open.variables?.id === identity.id;
        return (
          <Tooltip key={identity.id}>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size={isMobile ? "icon" : "sm"}
                // Names show only on wide screens; the avatar and tooltip identify each persona.
                className="max-2xl:size-8 max-2xl:px-0"
                aria-label={`Talk to ${identity.name}${busy ? " (working)" : ""}`}
                data-testid="project-identity-button"
                loading={pending}
                onClick={() => open.mutate(identity)}
              >
                <PersonaAvatar name={identity.name} size="xs" />
                {!isMobile && (
                  <span className="max-w-28 truncate max-2xl:hidden">{identity.name}</span>
                )}
                {busy && <RunningDot />}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              <span className="font-medium">{identity.name}</span>
              {": "}
              {busy
                ? `${identity.name} is working; open its session`
                : identity.active_session_id
                  ? `Open ${identity.name}'s session`
                  : `Start a session with ${identity.name}`}
              {identity.router && ". It receives ticket events and escalations."}
            </TooltipContent>
          </Tooltip>
        );
      })}
      {hidden.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={`${hidden.length} more personas`}
              data-testid="project-identity-more"
              className="text-xs text-muted-foreground tabular-nums"
            >
              +{hidden.length}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {hidden.map((identity) => (
              <DropdownMenuItem
                key={identity.id}
                className="gap-2"
                onSelect={() => open.mutate(identity)}
              >
                <PersonaAvatar name={identity.name} size="xs" />
                <span className="max-w-48 truncate">{identity.name}</span>
                {identity.active_session_state === "busy" && <RunningDot />}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Edit team"
            data-testid="project-edit-team"
            className="text-muted-foreground"
            onClick={() => setEditing(true)}
          >
            <UsersIcon />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          {open.error ? `Could not open: ${open.error.message}` : "Team"}
        </TooltipContent>
      </Tooltip>
      {editing && (
        <ProjectTeamDialog
          projectId={projectId}
          projectName={projectName}
          onClose={() => setEditing(false)}
        />
      )}
    </div>
  );
}
